"""
Browser domain detection for playtimed.

Detects which websites are open in browser windows by parsing window titles
via KWin's D-Bus interface. Works on KDE Wayland.

See ADR-001 for design details.
"""

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# Lazy import dbus - may not be available in all environments
_dbus = None


def _get_dbus():
    """Lazy load dbus module."""
    global _dbus
    if _dbus is None:
        try:
            import dbus
            _dbus = dbus
        except ImportError:
            log.warning("dbus-python not available - browser monitoring disabled")
            return None
    return _dbus


# Site signature lookup - maps title keywords to domains
# Check longer signatures first to avoid partial matches
SITE_SIGNATURES = {
    'Discord': 'discord.com',
    'YouTube Music': 'music.youtube.com',
    'YouTube': 'youtube.com',
    'IXL': 'ixl.com',
    'Google Search': 'google.com',
    'Google': 'google.com',
    'Gmail': 'mail.google.com',
    'Twitch': 'twitch.tv',
    'Reddit': 'reddit.com',
    'Twitter': 'twitter.com',
    'GitHub': 'github.com',
    'Netflix': 'netflix.com',
    'Amazon': 'amazon.com',
    'Wikipedia': 'wikipedia.org',
    'Stack Overflow': 'stackoverflow.com',
    'Coolmath Games': 'coolmathgames.com',
    'Poki': 'poki.com',
    'Roblox': 'roblox.com',
}

# Browser suffixes to strip from window titles
BROWSER_SUFFIXES = [
    ' - Google Chrome',
    ' - Chromium',
    ' - Mozilla Firefox',
    ' - Firefox',
    ' - Brave',
    ' - Microsoft Edge',
]

# Map suffix to browser identifier
BROWSER_SUFFIX_TO_ID = {
    ' - Google Chrome': 'chrome',
    ' - Chromium': 'chromium',
    ' - Mozilla Firefox': 'firefox',
    ' - Firefox': 'firefox',
    ' - Brave': 'brave',
    ' - Microsoft Edge': 'edge',
}


@dataclass
class BrowserWindow:
    """Represents a browser window with detected domain."""
    title: str
    browser: str  # 'chrome', 'firefox', etc.
    domain: Optional[str]  # None if domain couldn't be extracted


def get_windows_from_kwin(bus) -> list[tuple[str, str]]:
    """
    Query KWin for all window titles via D-Bus.

    Returns list of (window_id, title) tuples.
    """
    dbus = _get_dbus()
    if dbus is None:
        return []

    try:
        kwin = bus.get_object('org.kde.KWin', '/WindowsRunner')
        runner = dbus.Interface(kwin, 'org.kde.krunner1')

        # Match('') returns all windows
        results = runner.Match('')

        windows = []
        for item in results:
            window_id, title, icon_name, relevance, score, props = item
            windows.append((str(window_id), str(title)))

        return windows
    except Exception as e:
        log.debug("Failed to query KWin: %s", e)
        return []


def extract_domain_from_title(title: str) -> tuple[Optional[str], Optional[str]]:
    """
    Extract domain and browser from window title.

    Args:
        title: Window title like "(3) Discord | #general - Google Chrome"

    Returns:
        Tuple of (domain, browser) where:
        - domain is like "discord.com" or "unknown:<title>" or None if not a browser
        - browser is like "chrome" or None if not a browser window
    """
    # Detect browser and strip suffix
    original_title = title
    browser = None

    for suffix, browser_id in BROWSER_SUFFIX_TO_ID.items():
        if title.endswith(suffix):
            title = title[:-len(suffix)]
            browser = browser_id
            break

    if browser is None:
        return None, None

    # Remove notification count prefix like "(3) "
    title = re.sub(r'^\(\d+\)\s*', '', title)

    # Check site signatures (longer ones first to avoid partial matches)
    for sig, domain in sorted(SITE_SIGNATURES.items(), key=lambda x: -len(x[0])):
        if sig in title:
            return domain, browser

    # Try to extract from title patterns like "Page | Site Name"
    if ' | ' in title:
        parts = title.split(' | ')
        # Last part is often the site name
        site_name = parts[-1].strip()
        if site_name in SITE_SIGNATURES:
            return SITE_SIGNATURES[site_name], browser

    # Unknown - return cleaned title as identifier for discovery
    cleaned = re.sub(r'[^\w\s-]', '', title)[:50].strip()
    if cleaned:
        return f'unknown:{cleaned}', browser

    return None, browser


def get_browser_domains_for_user(uid: int) -> list[BrowserWindow]:
    """
    Get all browser windows for a user.

    Uses subprocess to run D-Bus query as the user, since root cannot
    directly access user session buses due to D-Bus security policy.

    Args:
        uid: User ID (e.g., 1000 for anders)

    Returns:
        List of BrowserWindow objects with detected domains.
    """
    import pwd
    import subprocess

    # Get username from UID
    try:
        username = pwd.getpwuid(uid).pw_name
    except KeyError:
        log.debug("No user found for uid %d", uid)
        return []

    # Check if bus socket exists
    bus_path = f'/run/user/{uid}/bus'
    if not os.path.exists(bus_path):
        log.debug("No session bus for uid %d", uid)
        return []

    # Query KWin via qdbus as the user (with session bus address set)
    try:
        env = os.environ.copy()
        env['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path=/run/user/{uid}/bus'

        result = subprocess.run(
            ['sudo', '-u', username, '--preserve-env=DBUS_SESSION_BUS_ADDRESS',
             'qdbus6', '--literal', 'org.kde.KWin', '/WindowsRunner',
             'org.kde.krunner1.Match', ''],
            capture_output=True, text=True, timeout=5, env=env
        )
        if result.returncode != 0:
            log.debug("qdbus6 failed: %s", result.stderr)
            return []

        # Parse qdbus literal output
        windows = _parse_qdbus_output(result.stdout)

    except subprocess.TimeoutExpired:
        log.debug("qdbus6 timed out for uid %d", uid)
        return []
    except FileNotFoundError:
        log.debug("qdbus6 not found")
        return []
    except Exception as e:
        log.debug("Failed to query windows for uid %d: %s", uid, e)
        return []

    results = []
    seen_domains = set()

    for window_id, title in windows:
        domain, browser = extract_domain_from_title(title)
        if domain and domain not in seen_domains:
            seen_domains.add(domain)
            results.append(BrowserWindow(
                title=title,
                browser=browser,
                domain=domain,
            ))

    return results


def _parse_qdbus_output(output: str) -> list[tuple[str, str]]:
    """Parse qdbus --literal output for window list."""
    import re

    windows = []
    # Match pattern: "window_id", "title",
    # The output has nested [Argument: ...] structures
    # We want the second quoted string in each (sssida{sv}) tuple
    pattern = r'\[Argument: \(sssida\{sv\}\) "([^"]*)", "([^"]*)"'

    for match in re.finditer(pattern, output):
        window_id = match.group(1)
        title = match.group(2)
        windows.append((window_id, title))

    return windows


def get_active_domains(uid: int) -> dict[str, str]:
    """
    Get currently active browser domains for a user.

    This is the main entry point for the daemon's scan loop.

    Args:
        uid: User ID

    Returns:
        Dict mapping domain -> browser (e.g., {'discord.com': 'chrome'})
    """
    windows = get_browser_domains_for_user(uid)
    return {w.domain: w.browser for w in windows if w.domain}


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
        from datetime import datetime

        active = get_active_domains(self.uid)
        results = {}
        now = datetime.now()

        for domain, browser in active.items():
            # Skip "unknown:" domains from tracking (but still discover them)
            if domain.startswith('unknown:'):
                # Could log these for manual mapping later
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
