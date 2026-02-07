"""
Browser domain detection for playtimed.

This package provides browser-specific workers that detect active tabs
and resolve window titles to domains.

Architecture:
    - detection.py: KWin window title detection (shared)
    - base.py: BrowserWorker ABC, BrowserTab dataclass, SITE_SIGNATURES
    - chrome.py: ChromeWorker for Chrome/Chromium/Brave/Edge
    - firefox.py: FirefoxWorker with places.sqlite domain resolution

Public API:
    get_browser_domains_for_user(uid) -> list[BrowserWindow]
    get_active_domains(uid) -> dict[str, str]
    BrowserMonitor - class for daemon integration
    BrowserWindow - dataclass for backward compatibility
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .base import BrowserTab, BrowserWorker, SITE_SIGNATURES
from .detection import get_window_titles
from .chrome import ChromeWorker
from .firefox import FirefoxWorker

log = logging.getLogger(__name__)

# Registry of all available browser workers
_WORKERS: list[BrowserWorker] = [
    ChromeWorker(),
    FirefoxWorker(),
]


@dataclass
class BrowserWindow:
    """
    Represents a browser window with detected domain.

    Backward-compatible with legacy API.
    """
    title: str
    browser: str  # 'chrome', 'firefox', etc.
    domain: Optional[str]  # None if domain couldn't be extracted


def get_browser_domains_for_user(uid: int) -> list[BrowserWindow]:
    """
    Get all browser windows for a user.

    Queries KWin for window titles, then uses browser workers
    to resolve domains.

    Args:
        uid: User ID (e.g., 1000)

    Returns:
        List of BrowserWindow objects with detected domains.
    """
    # Get window titles from KWin
    window_titles = get_window_titles(uid)
    if not window_titles:
        return []

    results = []
    seen_domains = set()

    # Let each worker process the windows
    for worker in _WORKERS:
        try:
            tabs = worker.get_active_tabs(uid, window_titles)
            for tab in tabs:
                if tab.domain and tab.domain not in seen_domains:
                    seen_domains.add(tab.domain)
                    results.append(BrowserWindow(
                        title=tab.title,
                        browser=tab.browser,
                        domain=tab.domain,
                    ))
        except Exception as e:
            log.debug("Worker %s failed: %s", worker.name, e)

    return results


def get_active_domains(uid: int) -> dict[str, str]:
    """
    Get currently active browser domains for a user.

    This is the main entry point for the daemon's scan loop.
    Combines Chrome session file reading with window title detection
    so all browsers (Chrome, Firefox, etc.) are covered.

    Args:
        uid: User ID

    Returns:
        Dict mapping domain -> browser (e.g., {'discord.com': 'chrome'})
    """
    domains = {}

    # Session file reading (catches background tabs without D-Bus)
    chrome_worker = ChromeWorker()
    session_domains = chrome_worker.get_active_domains_from_session(uid)
    if session_domains:
        log.debug("Got %d domains from Chrome session files", len(session_domains))
        domains.update(session_domains)

    firefox_worker = FirefoxWorker()
    ff_session = firefox_worker.get_active_domains_from_session(uid)
    if ff_session:
        log.debug("Got %d domains from Firefox session files", len(ff_session))
        for domain, browser in ff_session.items():
            if domain not in domains:
                domains[domain] = browser

    # Window title detection (fallback for browsers without session file support)
    windows = get_browser_domains_for_user(uid)
    for w in windows:
        if w.domain and w.domain not in domains:
            domains[w.domain] = w.browser

    return domains


# Legacy exports for backward compatibility
BROWSER_SUFFIXES = [
    ' - Google Chrome',
    ' - Chromium',
    ' - Mozilla Firefox',
    ' - Firefox',
    ' - Brave',
    ' - Microsoft Edge',
]


def extract_domain_from_title(title: str) -> tuple[Optional[str], Optional[str]]:
    """
    Extract domain and browser from window title.

    Legacy function - tries each registered worker.

    Args:
        title: Window title

    Returns:
        Tuple of (domain, browser)
    """
    for worker in _WORKERS:
        browser_id = worker.matches_window(title)
        if browser_id is None:
            continue

        clean_title = worker.clean_title(title)
        domain = worker.match_signature(clean_title)
        if domain:
            return domain, browser_id

        # Unknown
        import re
        cleaned = re.sub(r'[^\w\s-]', '', clean_title)[:50].strip()
        if cleaned:
            return f'unknown:{cleaned}', browser_id

        return None, browser_id

    return None, None


class BrowserMonitor:
    """
    Monitors browser activity for a user.

    Maintains discovery candidates and integrates with the pattern system.
    """

    def __init__(self, db, user: str, uid: int):
        """
        Initialize browser monitor.

        Args:
            db: ActivityDB instance
            user: Username being monitored
            uid: User's UID
        """
        self.db = db
        self.user = user
        self.uid = uid

        # Discovery tracking: domain -> {first_seen, sample_count, last_seen}
        self._candidates: dict[str, dict] = {}

    def scan(self) -> dict[str, dict]:
        """
        Scan for browser domains.

        Returns:
            Dict of domain -> {browser, pattern_id, is_new} for active domains.
            pattern_id is None for unknown/discovered domains.
            is_new is True if this domain was just discovered this scan.
        """
        active = get_active_domains(self.uid)
        results = {}
        now = datetime.now()

        for domain, browser in active.items():
            # Skip "unknown:" domains from tracking (but still discover them)
            if domain.startswith('unknown:'):
                log.debug("Unknown browser title: %s", domain)
                continue

            # Check if we have an existing pattern
            pattern = self.db.get_pattern_by_domain_and_owner(domain, self.user)

            if pattern:
                # Known pattern - return it
                results[domain] = {
                    'browser': browser,
                    'pattern_id': pattern['id'],
                    'pattern': pattern,
                    'is_new': False,
                }
            else:
                # Unknown domain - check discovery threshold
                if domain not in self._candidates:
                    self._candidates[domain] = {
                        'first_seen': now,
                        'sample_count': 0,
                        'last_seen': now,
                        'browser': browser,
                    }

                cand = self._candidates[domain]
                cand['sample_count'] += 1
                cand['last_seen'] = now

                # Check if threshold met
                config = self.db.get_discovery_config()
                window = (now - cand['first_seen']).total_seconds()

                if (window <= config['sample_window_seconds'] and
                        cand['sample_count'] >= config['min_samples']):
                    # Threshold met - create discovered pattern
                    pattern_id = self.db.discover_browser_domain(
                        domain, browser, self.user
                    )
                    log.info("Discovered browser domain: %s (%s)", domain, browser)

                    # Remove from candidates
                    del self._candidates[domain]

                    results[domain] = {
                        'browser': browser,
                        'pattern_id': pattern_id,
                        'pattern': self.db.get_pattern_by_id(pattern_id),
                        'is_new': True,
                    }

        # Clean up stale candidates
        config = self.db.get_discovery_config()
        stale = []
        for domain, cand in self._candidates.items():
            if (now - cand['last_seen']).total_seconds() > config['sample_window_seconds']:
                stale.append(domain)
        for domain in stale:
            del self._candidates[domain]

        return results


__all__ = [
    # Core types
    'BrowserWindow',
    'BrowserTab',
    'BrowserWorker',
    'BrowserMonitor',

    # Workers
    'ChromeWorker',
    'FirefoxWorker',

    # Functions
    'get_browser_domains_for_user',
    'get_active_domains',
    'extract_domain_from_title',
    'get_window_titles',

    # Constants
    'SITE_SIGNATURES',
    'BROWSER_SUFFIXES',
]
