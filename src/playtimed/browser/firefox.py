"""
Firefox browser worker for playtimed.

TODO: Implement full Firefox support using places.sqlite

Firefox stores history in:
    ~/.mozilla/firefox/<profile>/places.sqlite

Tables of interest:
    - moz_places: URLs and titles
    - moz_historyvisits: Visit timestamps

Note: Firefox uses em-dash (—) not hyphen (-) in window titles:
    "Page Title — Mozilla Firefox"
"""

import logging
import pwd
from pathlib import Path
from typing import Optional

import psutil

from .base import BrowserWorker, BrowserTab

log = logging.getLogger(__name__)


class FirefoxWorker(BrowserWorker):
    """
    Worker for Firefox browser.

    Currently a stub - returns empty results.
    Full implementation would query places.sqlite for domain resolution.
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
            ' — Mozilla Firefox': 'firefox',
            ' — Firefox': 'firefox',
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

        TODO: Implement with places.sqlite lookup
        Currently returns tabs with unknown domains.
        """
        tabs = []

        for window_id, title in window_titles:
            browser_id = self.matches_window(title)
            if browser_id is None:
                continue

            clean_title = self.strip_browser_suffix(title)

            # TODO: Implement domain resolution via places.sqlite
            # For now, mark as unknown
            cleaned = clean_title[:50].strip()
            domain = f'unknown:{cleaned}' if cleaned else None

            if domain:
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

        TODO: Implement using:
            ~/.mozilla/firefox/<profile>/places.sqlite

        Query would be:
            SELECT url FROM moz_places WHERE title LIKE ?
            ORDER BY last_visit_date DESC LIMIT 1
        """
        # Stub - not implemented yet
        return None

    def _find_firefox_profile(self, uid: int) -> Optional[Path]:
        """
        Find the default Firefox profile directory.

        Firefox profiles are in ~/.mozilla/firefox/<random>.default-release/
        """
        try:
            home = Path(pwd.getpwuid(uid).pw_dir)
        except KeyError:
            return None

        firefox_dir = home / '.mozilla' / 'firefox'
        if not firefox_dir.exists():
            return None

        # Look for default-release profile
        for profile in firefox_dir.iterdir():
            if profile.is_dir() and 'default' in profile.name.lower():
                places = profile / 'places.sqlite'
                if places.exists():
                    return profile

        return None
