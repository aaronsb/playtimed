"""Tests for browser domain detection."""

import pytest
from playtimed.browser import extract_domain_from_title, SITE_SIGNATURES


class TestExtractDomainFromTitle:
    """Tests for extract_domain_from_title function."""

    def test_chrome_discord_with_notification_count(self):
        """Discord with unread notification count."""
        domain, browser = extract_domain_from_title(
            "(3) Discord | #general | Server Name - Google Chrome"
        )
        assert domain == "discord.com"
        assert browser == "chrome"

    def test_chrome_discord_simple(self):
        """Discord without notification count."""
        domain, browser = extract_domain_from_title(
            "Discord | #general | Server - Google Chrome"
        )
        assert domain == "discord.com"
        assert browser == "chrome"

    def test_chrome_youtube_music(self):
        """YouTube Music should map to subdomain."""
        domain, browser = extract_domain_from_title(
            "YouTube Music - Google Chrome"
        )
        assert domain == "music.youtube.com"
        assert browser == "chrome"

    def test_chrome_youtube(self):
        """Regular YouTube."""
        domain, browser = extract_domain_from_title(
            "Video Title - YouTube - Google Chrome"
        )
        assert domain == "youtube.com"
        assert browser == "chrome"

    def test_chrome_ixl(self):
        """Educational site IXL."""
        domain, browser = extract_domain_from_title(
            "IXL | Dashboard - Google Chrome"
        )
        assert domain == "ixl.com"
        assert browser == "chrome"

    def test_chrome_coolmath(self):
        """Gaming site Coolmath."""
        domain, browser = extract_domain_from_title(
            "Game Name - Coolmath Games - Google Chrome"
        )
        assert domain == "coolmathgames.com"
        assert browser == "chrome"

    def test_firefox_github(self):
        """Firefox browser detection."""
        domain, browser = extract_domain_from_title(
            "repo/file.py at main - GitHub - Mozilla Firefox"
        )
        assert domain == "github.com"
        assert browser == "firefox"

    def test_chromium_reddit(self):
        """Chromium browser detection."""
        domain, browser = extract_domain_from_title(
            "r/linux - Reddit - Chromium"
        )
        assert domain == "reddit.com"
        assert browser == "chromium"

    def test_non_browser_window(self):
        """Non-browser window returns None."""
        domain, browser = extract_domain_from_title("Steam")
        assert domain is None
        assert browser is None

    def test_non_browser_minecraft(self):
        """Minecraft is not a browser."""
        domain, browser = extract_domain_from_title("Minecraft 1.20.1")
        assert domain is None
        assert browser is None

    def test_unknown_site_returns_unknown_prefix(self):
        """Unknown sites return 'unknown:' prefix."""
        domain, browser = extract_domain_from_title(
            "Random Site Title - Google Chrome"
        )
        assert domain.startswith("unknown:")
        assert "Random Site Title" in domain
        assert browser == "chrome"

    def test_unknown_site_cleaned(self):
        """Unknown site titles are cleaned of special chars."""
        domain, browser = extract_domain_from_title(
            "Site!@#$%Title - Google Chrome"
        )
        assert domain.startswith("unknown:")
        # Special chars removed
        assert "!" not in domain
        assert "@" not in domain
        assert browser == "chrome"

    def test_youtube_music_preferred_over_youtube(self):
        """YouTube Music should match before YouTube."""
        # This tests that longer signatures are checked first
        domain, browser = extract_domain_from_title(
            "Song Title - YouTube Music - Google Chrome"
        )
        assert domain == "music.youtube.com"

    def test_google_search(self):
        """Google Search page."""
        domain, browser = extract_domain_from_title(
            "search query - Google Search - Google Chrome"
        )
        assert domain == "google.com"
        assert browser == "chrome"

    def test_gmail(self):
        """Gmail maps to mail.google.com."""
        domain, browser = extract_domain_from_title(
            "Inbox (5) - Gmail - Google Chrome"
        )
        assert domain == "mail.google.com"
        assert browser == "chrome"

    def test_large_notification_count(self):
        """Large notification counts are handled."""
        domain, browser = extract_domain_from_title(
            "(999) Discord | Server - Google Chrome"
        )
        assert domain == "discord.com"
        assert browser == "chrome"

    def test_brave_browser(self):
        """Brave browser detection."""
        domain, browser = extract_domain_from_title(
            "GitHub - Brave"
        )
        assert domain == "github.com"
        assert browser == "brave"

    def test_edge_browser(self):
        """Microsoft Edge browser detection."""
        domain, browser = extract_domain_from_title(
            "GitHub - Microsoft Edge"
        )
        assert domain == "github.com"
        assert browser == "edge"


class TestSiteSignatures:
    """Tests for site signature coverage."""

    def test_all_signatures_valid(self):
        """All signatures map to valid-looking domains."""
        for sig, domain in SITE_SIGNATURES.items():
            assert '.' in domain, f"Signature '{sig}' has invalid domain: {domain}"
            assert len(domain) > 3, f"Signature '{sig}' has too short domain: {domain}"

    def test_key_sites_covered(self):
        """Important sites for playtimed are covered."""
        required = ['ixl.com', 'youtube.com', 'discord.com', 'coolmathgames.com']
        domains = set(SITE_SIGNATURES.values())
        for site in required:
            assert site in domains, f"Missing signature for {site}"
