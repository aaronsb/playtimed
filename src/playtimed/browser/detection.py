"""
Window title detection via KWin D-Bus.

Provides shared functionality for getting window titles from the
window manager. Used by all browser workers.

Works on KDE Plasma (Wayland and X11) via KWin's WindowsRunner D-Bus interface.
"""

import logging
import os
import pwd
import re
import subprocess
from typing import Optional

log = logging.getLogger(__name__)


def get_window_titles(uid: int) -> list[tuple[str, str]]:
    """
    Get all window titles for a user via KWin D-Bus.

    Uses subprocess to run qdbus6 as the target user, since root
    cannot directly access user session buses.

    Args:
        uid: User ID

    Returns:
        List of (window_id, title) tuples
    """
    # Get username from UID
    try:
        username = pwd.getpwuid(uid).pw_name
    except KeyError:
        log.debug("No user found for uid %d", uid)
        return []

    # Check if session bus exists
    bus_path = f'/run/user/{uid}/bus'
    if not os.path.exists(bus_path):
        log.debug("No session bus for uid %d", uid)
        return []

    # Query KWin via qdbus as the user
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

        return _parse_qdbus_output(result.stdout)

    except subprocess.TimeoutExpired:
        log.debug("qdbus6 timed out for uid %d", uid)
        return []
    except FileNotFoundError:
        log.debug("qdbus6 not found")
        return []
    except Exception as e:
        log.debug("Failed to query windows for uid %d: %s", uid, e)
        return []


def _parse_qdbus_output(output: str) -> list[tuple[str, str]]:
    """
    Parse qdbus --literal output for window list.

    The output format is nested D-Bus argument structures.
    We extract (window_id, title) from each (sssida{sv}) tuple.

    Args:
        output: Raw qdbus --literal output

    Returns:
        List of (window_id, title) tuples
    """
    windows = []

    # Match pattern: "window_id", "title" in each (sssida{sv}) tuple
    pattern = r'\[Argument: \(sssida\{sv\}\) "([^"]*)", "([^"]*)"'

    for match in re.finditer(pattern, output):
        window_id = match.group(1)
        title = match.group(2)
        windows.append((window_id, title))

    return windows


def get_window_icon_names(uid: int) -> dict[str, str]:
    """
    Get window icon names (useful for PWA detection).

    PWAs like WhatsApp Web have icon names like:
    'chrome-hnpfjngllnobngcgfapefoaidbinmjnm-Default'

    Args:
        uid: User ID

    Returns:
        Dict mapping window_id to icon_name
    """
    # Get username from UID
    try:
        username = pwd.getpwuid(uid).pw_name
    except KeyError:
        return {}

    bus_path = f'/run/user/{uid}/bus'
    if not os.path.exists(bus_path):
        return {}

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
            return {}

        return _parse_icon_names(result.stdout)

    except Exception:
        return {}


def _parse_icon_names(output: str) -> dict[str, str]:
    """
    Parse qdbus output to extract window_id -> icon_name mapping.

    Format: [Argument: (sssida{sv}) "window_id", "title", "icon_name", ...]
    """
    icons = {}

    # Match: "window_id", "title", "icon_name"
    pattern = r'\[Argument: \(sssida\{sv\}\) "([^"]*)", "[^"]*", "([^"]*)"'

    for match in re.finditer(pattern, output):
        window_id = match.group(1)
        icon_name = match.group(2)
        icons[window_id] = icon_name

    return icons


def is_chrome_pwa(icon_name: str) -> bool:
    """
    Check if an icon name indicates a Chrome PWA.

    Chrome PWAs have icon names like 'chrome-{app_id}-Default'

    Args:
        icon_name: Window icon name

    Returns:
        True if this appears to be a Chrome PWA
    """
    return icon_name.startswith('chrome-') and '-Default' in icon_name
