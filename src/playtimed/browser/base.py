"""
Base classes and protocols for browser workers.

Each browser platform (Chrome, Firefox, etc.) implements BrowserWorker
to provide platform-specific tab detection and domain resolution.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class BrowserTab:
    """
    Represents a detected browser tab.

    Attributes:
        title: Window/tab title as shown in window manager
        domain: Resolved domain (e.g., 'reddit.com') or None if unknown
        browser: Browser identifier (e.g., 'chrome', 'firefox')
        url: Full URL if available, None otherwise
    """
    title: str
    domain: Optional[str]
    browser: str
    url: Optional[str] = None

    @property
    def is_resolved(self) -> bool:
        """True if domain was successfully resolved."""
        return self.domain is not None and not self.domain.startswith('unknown:')


class BrowserWorker(ABC):
    """
    Abstract base class for browser-specific workers.

    Each worker handles detection and domain resolution for a family
    of browsers (e.g., ChromeWorker handles Chrome, Chromium, Brave, Edge).

    Workers are responsible for:
    1. Identifying which window titles belong to their browser
    2. Resolving window titles to domains (via signatures or history DB)
    3. Detecting if their browser is running
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for this worker (e.g., 'Chrome')."""
        pass

    @property
    @abstractmethod
    def browser_ids(self) -> list[str]:
        """
        List of browser identifiers this worker handles.

        Used for pattern matching and database storage.
        Example: ['chrome', 'chromium', 'brave', 'edge']
        """
        pass

    @property
    @abstractmethod
    def window_suffixes(self) -> dict[str, str]:
        """
        Map of window title suffixes to browser IDs.

        Used to identify which browser a window belongs to.
        Example: {' - Google Chrome': 'chrome', ' - Chromium': 'chromium'}
        """
        pass

    @abstractmethod
    def detect_running(self, uid: int) -> bool:
        """
        Check if this browser is running for the given user.

        Args:
            uid: User ID to check

        Returns:
            True if browser process is running for this user
        """
        pass

    @abstractmethod
    def get_active_tabs(
        self,
        uid: int,
        window_titles: list[tuple[str, str]]
    ) -> list[BrowserTab]:
        """
        Get active tabs from window titles.

        Args:
            uid: User ID
            window_titles: List of (window_id, title) from window manager

        Returns:
            List of BrowserTab objects for windows belonging to this browser
        """
        pass

    @abstractmethod
    def resolve_domain(self, uid: int, title: str) -> Optional[str]:
        """
        Resolve a window title to a domain using browser history.

        This is the fallback when signature matching fails.

        Args:
            uid: User ID (to find correct browser profile)
            title: Window title to look up

        Returns:
            Domain string if found in history, None otherwise
        """
        pass

    def matches_window(self, title: str) -> Optional[str]:
        """
        Check if a window title belongs to this browser.

        Args:
            title: Window title to check

        Returns:
            Browser ID if this window belongs to us, None otherwise
        """
        for suffix, browser_id in self.window_suffixes.items():
            if title.endswith(suffix):
                return browser_id
        return None

    def strip_browser_suffix(self, title: str) -> str:
        """
        Remove browser suffix from window title.

        Args:
            title: Full window title

        Returns:
            Title with browser suffix removed
        """
        for suffix in self.window_suffixes.keys():
            if title.endswith(suffix):
                return title[:-len(suffix)]
        return title
