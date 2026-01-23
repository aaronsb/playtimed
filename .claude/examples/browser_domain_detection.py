#!/usr/bin/env python3
"""
Browser Domain Detection Examples for playtimed

These examples demonstrate how to detect browser window titles and extract
domains on KDE Wayland using D-Bus. Validated on brick (Anders' machine)
on 2026-01-22.

Context:
- brick runs KDE Plasma on Wayland
- Anders uses Google Chrome
- We need to track which websites accumulate time (for educational vs gaming)
- Window titles contain page titles, which we map to domains

D-Bus Details:
- Service: org.kde.KWin
- Path: /WindowsRunner
- Interface: org.kde.krunner1
- Method: Match('') - empty string returns all windows
- Requires user's session bus: unix:path=/run/user/<uid>/bus

Usage:
    # As root, query anders' windows:
    sudo -u anders python3 browser_domain_detection.py

    # Or set DBUS_SESSION_BUS_ADDRESS manually:
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus python3 browser_domain_detection.py

Related: ADR-001-browser-domain-tracking.md
"""

import dbus
import os
import re
from typing import Optional


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
}

# Browser suffixes to strip from window titles
BROWSER_SUFFIXES = [
    ' - Google Chrome',
    ' - Chromium',
    ' - Mozilla Firefox',
    ' - Firefox',
]


def get_windows_from_kwin(bus: dbus.SessionBus) -> list[tuple[str, str]]:
    """
    Query KWin for all window titles via D-Bus.

    Returns list of (window_id, title) tuples.

    Example output:
        [
            ('0_{uuid}', '(3) Discord | #general | Server - Google Chrome'),
            ('0_{uuid}', 'YouTube Music - Google Chrome'),
            ('0_{uuid}', 'Steam'),
        ]
    """
    kwin = bus.get_object('org.kde.KWin', '/WindowsRunner')
    runner = dbus.Interface(kwin, 'org.kde.krunner1')

    # Match('') returns all windows
    results = runner.Match('')

    windows = []
    for item in results:
        window_id, title, icon_name, relevance, score, props = item
        windows.append((str(window_id), str(title)))

    return windows


def extract_domain_from_title(title: str) -> Optional[str]:
    """
    Extract domain from browser window title.

    Args:
        title: Window title like "(3) Discord | #general - Google Chrome"

    Returns:
        Domain string like "discord.com", or None if not a browser window.
        Returns "unknown:<title>" if browser window but domain not recognized.

    Examples:
        >>> extract_domain_from_title("(3) Discord | #general - Google Chrome")
        'discord.com'
        >>> extract_domain_from_title("YouTube Music - Google Chrome")
        'music.youtube.com'
        >>> extract_domain_from_title("IXL | Dashboard - Google Chrome")
        'ixl.com'
        >>> extract_domain_from_title("Steam")
        None
    """
    # Remove browser suffix to identify browser windows
    original_title = title
    is_browser = False

    for suffix in BROWSER_SUFFIXES:
        if title.endswith(suffix):
            title = title[:-len(suffix)]
            is_browser = True
            break

    if not is_browser:
        return None

    # Remove notification count prefix like "(3) "
    title = re.sub(r'^\(\d+\)\s*', '', title)

    # Check site signatures (check longer ones first to avoid partial matches)
    for sig, domain in sorted(SITE_SIGNATURES.items(), key=lambda x: -len(x[0])):
        if sig in title:
            return domain

    # Try to extract from title patterns like "Page | Site Name"
    if ' | ' in title:
        parts = title.split(' | ')
        # Last part is often the site name
        site_name = parts[-1].strip()
        if site_name in SITE_SIGNATURES:
            return SITE_SIGNATURES[site_name]

    # Unknown - return cleaned title as identifier for discovery
    cleaned = re.sub(r'[^\w\s-]', '', title)[:50].strip()
    return f'unknown:{cleaned}'


def get_browser_domains_for_user(uid: int) -> set[str]:
    """
    Get all browser domains currently open for a user.

    Args:
        uid: User ID (e.g., 1000 for anders)

    Returns:
        Set of domain strings, deduplicated.

    Example:
        >>> get_browser_domains_for_user(1000)
        {'discord.com', 'music.youtube.com'}
    """
    # Connect to user's session bus
    bus_address = f'unix:path=/run/user/{uid}/bus'
    os.environ['DBUS_SESSION_BUS_ADDRESS'] = bus_address
    bus = dbus.SessionBus()

    windows = get_windows_from_kwin(bus)

    domains = set()
    for window_id, title in windows:
        domain = extract_domain_from_title(title)
        if domain:
            domains.add(domain)

    return domains


# -----------------------------------------------------------------------------
# Example: List all windows (not just browsers)
# -----------------------------------------------------------------------------
def example_list_all_windows():
    """List all windows for the current user's session."""
    bus = dbus.SessionBus()
    windows = get_windows_from_kwin(bus)

    print("All windows:")
    seen = set()
    for window_id, title in windows:
        if title not in seen:  # Dedupe
            seen.add(title)
            is_chrome = 'Google Chrome' in title or 'Chromium' in title
            prefix = "CHROME:" if is_chrome else "OTHER: "
            print(f"  {prefix} {title}")


# -----------------------------------------------------------------------------
# Example: Extract browser domains
# -----------------------------------------------------------------------------
def example_extract_domains():
    """Extract and display browser domains."""
    bus = dbus.SessionBus()
    windows = get_windows_from_kwin(bus)

    domains = set()
    for window_id, title in windows:
        domain = extract_domain_from_title(title)
        if domain:
            domains.add(domain)

    print("Detected browser domains:")
    for d in sorted(domains):
        print(f"  {d}")


# -----------------------------------------------------------------------------
# Example: Full detection as root for another user
# -----------------------------------------------------------------------------
def example_detect_for_user(username: str = 'anders'):
    """
    Detect browser domains for another user (requires root).

    Run as: sudo python3 browser_domain_detection.py
    """
    import pwd

    pw = pwd.getpwnam(username)
    uid = pw.pw_uid

    print(f"Detecting browser domains for {username} (uid={uid})...")
    domains = get_browser_domains_for_user(uid)

    print(f"Found {len(domains)} domains:")
    for d in sorted(domains):
        print(f"  {d}")


if __name__ == '__main__':
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == '--user':
        # Run as root to detect for another user
        username = sys.argv[2] if len(sys.argv) > 2 else 'anders'
        example_detect_for_user(username)
    else:
        # Run as current user
        print("=== All Windows ===")
        example_list_all_windows()
        print()
        print("=== Browser Domains ===")
        example_extract_domains()
