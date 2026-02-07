"""
Firefox browser worker for playtimed.

Uses places.sqlite for domain resolution from window titles,
and recovery.jsonlz4 session files for active tab detection.

Firefox stores history in:
    ~/.mozilla/firefox/<profile>/places.sqlite

Session data in:
    ~/.mozilla/firefox/<profile>/sessionstore-backups/recovery.jsonlz4

Tables of interest:
    - moz_places: URLs and titles (has last_visit_date column)

Note: Firefox uses em-dash (—) not hyphen (-) in window titles:
    "Page Title — Mozilla Firefox"
"""

import json
import logging
import pwd
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import psutil

from .base import BrowserWorker, BrowserTab

log = logging.getLogger(__name__)

# Try to import lz4 for session file reading
try:
    import lz4.block
    HAS_LZ4 = True
except ImportError:
    HAS_LZ4 = False

# Mozilla lz4 magic header
MOZLZ4_MAGIC = b'mozLz40\x00'


class FirefoxWorker(BrowserWorker):
    """
    Worker for Firefox browser.

    Resolves domains via places.sqlite history lookup and
    session file reading for active tab detection.
    """

    @property
    def name(self) -> str:
        return "Firefox"

    @property
    def browser_ids(self) -> list[str]:
        return ['firefox']

    @property
    def window_suffixes(self) -> dict[str, str]:
        # Note: Firefox uses em-dash (—) not hyphen (-)
        return {
            ' \u2014 Mozilla Firefox': 'firefox',
            ' \u2014 Firefox': 'firefox',
            ' - Mozilla Firefox': 'firefox',  # Some versions use hyphen
            ' - Firefox': 'firefox',
        }

    def detect_running(self, uid: int) -> bool:
        """Check if Firefox is running for user."""
        try:
            username = pwd.getpwuid(uid).pw_name
        except KeyError:
            return False

        for proc in psutil.process_iter(['name', 'username']):
            try:
                if (proc.info['username'] == username and
                        proc.info['name'].lower() in ('firefox', 'firefox-esr')):
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

        For each Firefox window:
        1. Try signature matching (fast path, shared with Chrome)
        2. If unknown, try places.sqlite lookup (fallback)
        """
        tabs = []
        seen_domains = set()

        for window_id, title in window_titles:
            browser_id = self.matches_window(title)
            if browser_id is None:
                continue

            # Strip browser suffix and notification count
            clean_title = self.clean_title(title)

            # Try signature matching first
            domain = self.match_signature(clean_title)

            # Fallback to places.sqlite lookup
            if domain is None:
                domain = self.resolve_domain(uid, clean_title)

            # Still nothing? Mark as unknown
            if domain is None:
                cleaned = re.sub(r'[^\w\s-]', '', clean_title)[:50].strip()
                domain = f'unknown:{cleaned}' if cleaned else None

            if domain and domain not in seen_domains:
                seen_domains.add(domain)
                tabs.append(BrowserTab(
                    title=title,
                    domain=domain,
                    browser=browser_id,
                    url=None,
                ))

        return tabs

    def resolve_domain(self, uid: int, title: str) -> Optional[str]:
        """
        Resolve title to domain via Firefox places.sqlite.

        Copies the DB to a temp file first (Firefox locks it while running).
        """
        profile = self._find_firefox_profile(uid)
        if not profile:
            return None

        places_path = profile / 'places.sqlite'
        if not places_path.exists():
            return None

        domain = self._lookup_in_places(places_path, title)
        if domain:
            log.debug("Resolved '%s' to '%s' via Firefox places", title[:30], domain)
        return domain

    def _lookup_in_places(self, places_path: Path, title: str) -> Optional[str]:
        """
        Look up title in Firefox places.sqlite.

        Copies the DB first to avoid lock issues with running Firefox.
        """
        temp_db = None
        try:
            # Copy to temp file (Firefox locks the original)
            temp_db = Path(tempfile.mktemp(suffix='.db'))
            shutil.copy2(places_path, temp_db)

            conn = sqlite3.connect(temp_db)

            # Search for matching title
            cursor = conn.execute("""
                SELECT url FROM moz_places
                WHERE title LIKE ?
                ORDER BY last_visit_date DESC
                LIMIT 1
            """, (f'%{title[:50]}%',))

            row = cursor.fetchone()
            conn.close()

            if row:
                parsed = urlparse(row[0])
                return parsed.netloc

            return None

        except Exception as e:
            log.debug("Firefox places lookup failed: %s", e)
            return None

        finally:
            if temp_db and temp_db.exists():
                try:
                    temp_db.unlink()
                except Exception:
                    pass

    def _find_firefox_profile(self, uid: int) -> Optional[Path]:
        """
        Find the default Firefox profile directory.

        Firefox profiles are in ~/.mozilla/firefox/<random>.default-release/
        Prefers profiles with 'default-release' in name, falls back to 'default'.
        """
        try:
            home = Path(pwd.getpwuid(uid).pw_dir)
        except KeyError:
            return None

        firefox_dir = home / '.mozilla' / 'firefox'
        if not firefox_dir.exists():
            return None

        # Look for default-release profile first (preferred)
        best = None
        for profile in firefox_dir.iterdir():
            if not profile.is_dir():
                continue
            places = profile / 'places.sqlite'
            if not places.exists():
                continue

            name_lower = profile.name.lower()
            if 'default-release' in name_lower:
                return profile  # Best match
            elif 'default' in name_lower and best is None:
                best = profile

        return best

    def get_active_urls_from_session(self, uid: int) -> list[str]:
        """
        Get currently active URLs from Firefox's session recovery file.

        Reads recovery.jsonlz4 (Mozilla's custom lz4 format) to get
        all open tab URLs, including background tabs.

        Requires python-lz4. Returns empty list if not available.
        """
        if not HAS_LZ4:
            return []

        profile = self._find_firefox_profile(uid)
        if not profile:
            return []

        recovery = profile / 'sessionstore-backups' / 'recovery.jsonlz4'
        if not recovery.exists():
            return []

        try:
            with open(recovery, 'rb') as f:
                magic = f.read(8)
                if magic != MOZLZ4_MAGIC:
                    log.debug("Bad magic in recovery.jsonlz4: %s", magic)
                    return []
                compressed = f.read()

            decompressed = lz4.block.decompress(compressed)
            data = json.loads(decompressed)

            urls = []
            for window in data.get('windows', []):
                for tab in window.get('tabs', []):
                    entries = tab.get('entries', [])
                    if entries:
                        url = entries[-1].get('url', '')
                        if url and url.startswith('http'):
                            urls.append(url)

            return urls

        except Exception as e:
            log.debug("Failed to read Firefox session: %s", e)
            return []

    def get_active_domains_from_session(self, uid: int) -> dict[str, str]:
        """
        Get currently active domains from Firefox's session file.

        Returns:
            Dict mapping domain -> 'firefox' for active tabs
        """
        urls = self.get_active_urls_from_session(uid)
        domains = {}

        for url in urls:
            try:
                parsed = urlparse(url)
                domain = parsed.netloc
                if domain:
                    domains[domain] = 'firefox'
            except Exception:
                continue

        return domains
