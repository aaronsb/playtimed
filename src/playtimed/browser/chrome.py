"""
Chrome browser worker for playtimed.

Handles Chrome, Chromium, Brave, and Edge browsers.
Uses window title parsing with Chrome history DB fallback for domain resolution.
"""

import logging
import os
import pwd
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import psutil

from .base import BrowserWorker, BrowserTab

log = logging.getLogger(__name__)


# Site signatures for fast-path domain resolution (skip DB lookup)
# Checked in order - longer matches first to avoid partial matches
SITE_SIGNATURES = {
    'Discord': 'discord.com',
    'YouTube Music': 'music.youtube.com',
    'YouTube': 'youtube.com',
    'IXL': 'ixl.com',
    'Google Search': 'google.com',
    'Google Docs': 'docs.google.com',
    'Google Sheets': 'docs.google.com',
    'Google Slides': 'docs.google.com',
    'Google Drive': 'drive.google.com',
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
    'ChatGPT': 'chatgpt.com',
    'Claude': 'claude.ai',
}

# Chrome profile paths by browser variant
CHROME_PROFILE_PATHS = {
    'chrome': '.config/google-chrome',
    'chromium': '.config/chromium',
    'brave': '.config/BraveSoftware/Brave-Browser',
    'edge': '.config/microsoft-edge',
}

# Process names to detect running browser
CHROME_PROCESS_NAMES = {
    'chrome', 'chromium', 'chromium-browser',
    'brave', 'brave-browser',
    'msedge', 'microsoft-edge',
    'google-chrome', 'google-chrome-stable',
}


class ChromeWorker(BrowserWorker):
    """
    Worker for Chrome-family browsers.

    Handles Chrome, Chromium, Brave, and Microsoft Edge.
    """

    @property
    def name(self) -> str:
        return "Chrome"

    @property
    def browser_ids(self) -> list[str]:
        return ['chrome', 'chromium', 'brave', 'edge']

    @property
    def window_suffixes(self) -> dict[str, str]:
        return {
            ' - Google Chrome': 'chrome',
            ' - Chromium': 'chromium',
            ' - Brave': 'brave',
            ' - Microsoft Edge': 'edge',
        }

    def detect_running(self, uid: int) -> bool:
        """Check if any Chrome-family browser is running for user."""
        try:
            username = pwd.getpwuid(uid).pw_name
        except KeyError:
            return False

        for proc in psutil.process_iter(['name', 'username']):
            try:
                if (proc.info['username'] == username and
                        proc.info['name'].lower() in CHROME_PROCESS_NAMES):
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return False

    def get_active_tabs(
        self,
        uid: int,
        window_titles: list[tuple[str, str]]
    ) -> list[BrowserTab]:
        """
        Get active tabs from window titles.

        For each Chrome-family window:
        1. Try signature matching (fast path)
        2. If unknown, try Chrome history DB lookup (fallback)
        """
        tabs = []
        seen_domains = set()

        for window_id, title in window_titles:
            browser_id = self.matches_window(title)
            if browser_id is None:
                continue

            # Strip browser suffix and notification count
            clean_title = self.strip_browser_suffix(title)
            clean_title = re.sub(r'^\(\d+\)\s*', '', clean_title)

            # Try signature matching first
            domain = self._match_signature(clean_title)

            # Fallback to history DB lookup
            if domain is None:
                domain = self.resolve_domain(uid, clean_title)

            # Still nothing? Mark as unknown
            if domain is None:
                # Clean the title for display
                cleaned = re.sub(r'[^\w\s-]', '', clean_title)[:50].strip()
                domain = f'unknown:{cleaned}' if cleaned else None

            if domain and domain not in seen_domains:
                seen_domains.add(domain)
                tabs.append(BrowserTab(
                    title=title,
                    domain=domain,
                    browser=browser_id,
                    url=None,  # Could be populated from history lookup
                ))

        return tabs

    def resolve_domain(self, uid: int, title: str) -> Optional[str]:
        """
        Resolve title to domain via Chrome history DB.

        Copies the history DB (Chrome locks it), then queries for
        matching page titles.
        """
        try:
            username = pwd.getpwuid(uid).pw_name
            home = Path(pwd.getpwuid(uid).pw_dir)
        except KeyError:
            return None

        # Try each browser variant's history
        for browser_id, profile_subpath in CHROME_PROFILE_PATHS.items():
            history_path = home / profile_subpath / 'Default' / 'History'

            if not history_path.exists():
                continue

            domain = self._lookup_in_history(history_path, title)
            if domain:
                log.debug("Resolved '%s' to '%s' via %s history",
                          title[:30], domain, browser_id)
                return domain

        return None

    def _match_signature(self, title: str) -> Optional[str]:
        """
        Try to match title against known site signatures.

        Checks longer signatures first to avoid partial matches.
        """
        # Sort by length descending
        for sig, domain in sorted(SITE_SIGNATURES.items(), key=lambda x: -len(x[0])):
            if sig in title:
                return domain

        # Check for pipe-separated site names (common format)
        if ' | ' in title:
            parts = title.split(' | ')
            site_name = parts[-1].strip()
            if site_name in SITE_SIGNATURES:
                return SITE_SIGNATURES[site_name]

        return None

    def _lookup_in_history(self, history_path: Path, title: str) -> Optional[str]:
        """
        Look up title in Chrome history DB.

        Copies the DB first to avoid lock issues with running Chrome.
        """
        temp_db = None
        try:
            # Copy to temp file (Chrome locks the original)
            temp_db = Path(tempfile.mktemp(suffix='.db'))
            shutil.copy2(history_path, temp_db)

            conn = sqlite3.connect(temp_db)

            # Search for matching title
            # Use LIKE for partial matching (titles may be truncated)
            cursor = conn.execute("""
                SELECT url FROM urls
                WHERE title LIKE ?
                ORDER BY last_visit_time DESC
                LIMIT 1
            """, (f'%{title[:50]}%',))

            row = cursor.fetchone()
            conn.close()

            if row:
                parsed = urlparse(row[0])
                return parsed.netloc

            return None

        except Exception as e:
            log.debug("History lookup failed: %s", e)
            return None

        finally:
            if temp_db and temp_db.exists():
                try:
                    temp_db.unlink()
                except Exception:
                    pass

    def get_recent_domains(self, uid: int, limit: int = 20) -> list[dict]:
        """
        Get recently visited domains from history.

        Useful for discovery/reporting.

        Returns:
            List of dicts with domain, title, visit_time
        """
        try:
            home = Path(pwd.getpwuid(uid).pw_dir)
        except KeyError:
            return []

        results = []

        for browser_id, profile_subpath in CHROME_PROFILE_PATHS.items():
            history_path = home / profile_subpath / 'Default' / 'History'

            if not history_path.exists():
                continue

            temp_db = None
            try:
                temp_db = Path(tempfile.mktemp(suffix='.db'))
                shutil.copy2(history_path, temp_db)

                conn = sqlite3.connect(temp_db)
                cursor = conn.execute("""
                    SELECT url, title, last_visit_time
                    FROM urls
                    ORDER BY last_visit_time DESC
                    LIMIT ?
                """, (limit,))

                for url, title, visit_time in cursor:
                    parsed = urlparse(url)
                    # WebKit timestamp to datetime
                    unix_ts = (visit_time / 1000000) - 11644473600
                    results.append({
                        'domain': parsed.netloc,
                        'title': title,
                        'browser': browser_id,
                        'visit_time': datetime.fromtimestamp(unix_ts),
                    })

                conn.close()
                break  # Only use first available history

            except Exception as e:
                log.debug("Failed to read history: %s", e)

            finally:
                if temp_db and temp_db.exists():
                    try:
                        temp_db.unlink()
                    except Exception:
                        pass

        return results

    def get_active_urls_from_session(self, uid: int) -> list[str]:
        """
        Get currently active URLs from Chrome's session files.

        Reads the SNSS session files directly, bypassing D-Bus entirely.
        This works even when the daemon runs as root.

        Args:
            uid: User ID

        Returns:
            List of URLs currently open in Chrome
        """
        try:
            home = Path(pwd.getpwuid(uid).pw_dir)
        except KeyError:
            return []

        urls = []

        for browser_id, profile_subpath in CHROME_PROFILE_PATHS.items():
            sessions_dir = home / profile_subpath / 'Default' / 'Sessions'

            if not sessions_dir.exists():
                continue

            # Find most recent Session file
            session_files = sorted(
                sessions_dir.glob('Session_*'),
                key=lambda p: p.stat().st_mtime,
                reverse=True
            )

            if not session_files:
                continue

            latest_session = session_files[0]

            try:
                # Use strings to extract URLs from binary SNSS format
                result = subprocess.run(
                    ['strings', str(latest_session)],
                    capture_output=True, text=True, timeout=5
                )

                if result.returncode == 0:
                    # Extract HTTP(S) URLs
                    url_pattern = re.compile(r'https?://[^\s"<>]+')
                    found = url_pattern.findall(result.stdout)

                    # Clean up URLs (remove trailing garbage)
                    for url in found:
                        # Strip common trailing characters that get captured
                        clean_url = re.sub(r'[^\w/\-_.~:/?#\[\]@!$&\'()*+,;=%]+$', '', url)
                        if clean_url and len(clean_url) > 10:
                            urls.append(clean_url)

                break  # Only use first available browser

            except Exception as e:
                log.debug("Failed to read session file: %s", e)

        # Deduplicate while preserving order
        seen = set()
        unique_urls = []
        for url in urls:
            if url not in seen:
                seen.add(url)
                unique_urls.append(url)

        return unique_urls

    def get_active_domains_from_session(self, uid: int) -> dict[str, str]:
        """
        Get currently active domains from Chrome's session files.

        Args:
            uid: User ID

        Returns:
            Dict mapping domain -> browser_id for active tabs
        """
        urls = self.get_active_urls_from_session(uid)
        domains = {}

        for url in urls:
            try:
                parsed = urlparse(url)
                domain = parsed.netloc
                if domain and not domain.startswith('chrome'):
                    # Determine browser_id from profile path used
                    domains[domain] = 'chrome'  # Could enhance to track actual browser
            except Exception:
                continue

        return domains
