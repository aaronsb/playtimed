"""
D-Bus notification support for playtimed.

Uses the org.freedesktop.Notifications interface which is supported by
KDE Plasma, GNOME, and other desktop environments.
"""

import logging
from typing import Optional

log = logging.getLogger("playtimed.notify")

# Try to import dbus
try:
    import dbus
    DBUS_AVAILABLE = True
except ImportError:
    DBUS_AVAILABLE = False
    log.debug("dbus module not available - notifications disabled")


class Notifier:
    """Send desktop notifications via D-Bus."""

    # Notification urgency levels
    URGENCY_LOW = 0
    URGENCY_NORMAL = 1
    URGENCY_CRITICAL = 2

    def __init__(self, app_name: str = "playtimed"):
        self.app_name = app_name
        self._bus = None
        self._notify_interface = None
        self._server_caps = []
        self._server_name = None
        self._available = False

        if DBUS_AVAILABLE:
            self._connect()

    def _connect(self) -> bool:
        """Connect to the session bus and notification service."""
        try:
            self._bus = dbus.SessionBus()
            notify_obj = self._bus.get_object(
                'org.freedesktop.Notifications',
                '/org/freedesktop/Notifications'
            )
            self._notify_interface = dbus.Interface(
                notify_obj,
                'org.freedesktop.Notifications'
            )

            # Get server info
            info = self._notify_interface.GetServerInformation()
            self._server_name = str(info[0])
            server_vendor = str(info[1])
            server_version = str(info[2])
            log.info(f"Connected to notification server: {self._server_name} "
                     f"({server_vendor} {server_version})")

            # Get capabilities
            self._server_caps = [str(c) for c in self._notify_interface.GetCapabilities()]
            log.debug(f"Server capabilities: {self._server_caps}")

            self._available = True
            return True

        except dbus.exceptions.DBusException as e:
            log.warning(f"Could not connect to notification service: {e}")
            self._available = False
            return False
        except Exception as e:
            log.warning(f"Unexpected error connecting to D-Bus: {e}")
            self._available = False
            return False

    @property
    def available(self) -> bool:
        """Check if notifications are available."""
        return self._available

    @property
    def server_name(self) -> Optional[str]:
        """Get the notification server name (e.g., 'Plasma', 'notify-osd')."""
        return self._server_name

    @property
    def is_kde(self) -> bool:
        """Check if running under KDE Plasma."""
        return self._server_name and 'plasma' in self._server_name.lower()

    @property
    def supports_actions(self) -> bool:
        """Check if the server supports notification actions (buttons)."""
        return 'actions' in self._server_caps

    @property
    def supports_persistence(self) -> bool:
        """Check if notifications can persist until dismissed."""
        return 'persistence' in self._server_caps

    @property
    def supports_body_markup(self) -> bool:
        """Check if notification body supports HTML markup."""
        return 'body-markup' in self._server_caps

    def notify(
        self,
        summary: str,
        body: str = "",
        icon: str = "dialog-information",
        urgency: int = URGENCY_NORMAL,
        timeout: int = -1,
        actions: list = None,
        replaces_id: int = 0
    ) -> int:
        """
        Send a notification.

        Args:
            summary: Notification title
            body: Notification body text (may support HTML if server allows)
            icon: Icon name or path
            urgency: URGENCY_LOW, URGENCY_NORMAL, or URGENCY_CRITICAL
            timeout: Timeout in ms (-1 for server default, 0 for never)
            actions: List of [action_id, label, ...] pairs for buttons
            replaces_id: ID of notification to replace (0 for new)

        Returns:
            Notification ID (can be used to replace/close later)
        """
        if not self._available:
            log.debug(f"Notification (unavailable): {summary}")
            return 0

        if actions is None:
            actions = []

        hints = {
            'urgency': dbus.Byte(urgency),
        }

        try:
            notification_id = self._notify_interface.Notify(
                self.app_name,      # app_name
                replaces_id,        # replaces_id
                icon,               # app_icon
                summary,            # summary
                body,               # body
                actions,            # actions
                hints,              # hints
                timeout             # expire_timeout
            )
            log.debug(f"Sent notification {notification_id}: {summary}")
            return int(notification_id)

        except dbus.exceptions.DBusException as e:
            log.error(f"Failed to send notification: {e}")
            return 0

    def close(self, notification_id: int) -> bool:
        """Close a notification by ID."""
        if not self._available or notification_id == 0:
            return False

        try:
            self._notify_interface.CloseNotification(notification_id)
            return True
        except dbus.exceptions.DBusException:
            return False

    # Convenience methods with Claude personality

    def info(self, message: str, title: str = "Claude says...") -> int:
        """Send an informational notification."""
        return self.notify(title, message, "dialog-information", self.URGENCY_NORMAL)

    def warning(self, message: str, title: str = "Claude warns...") -> int:
        """Send a warning notification."""
        return self.notify(title, message, "dialog-warning", self.URGENCY_NORMAL)

    def critical(self, message: str, title: str = "Claude ALERT") -> int:
        """Send a critical notification that persists."""
        return self.notify(
            title, message, "dialog-error",
            self.URGENCY_CRITICAL, timeout=0
        )

    def time_warning(self, minutes_left: int, user: str) -> int:
        """Send a screen time warning."""
        if minutes_left <= 5:
            return self.critical(
                f"Hey {user}! Only {minutes_left} minutes left. "
                "Time to wrap up what you're doing!",
                "Time Almost Up!"
            )
        elif minutes_left <= 15:
            return self.warning(
                f"{minutes_left} minutes of screen time remaining. "
                "Maybe start thinking about a good stopping point?",
                "Time Check"
            )
        else:
            return self.info(
                f"Just a heads up - you have {minutes_left} minutes left today.",
                "Time Update"
            )

    def discovery_notice(self, app_name: str, user: str) -> int:
        """Notify about a newly discovered application."""
        return self.info(
            f"I noticed you're running '{app_name}'. "
            "I'll keep an eye on it. If it's a game, your parents might "
            "want to add it to the monitored list.",
            "New App Detected"
        )

    def enforcement_notice(self, app_name: str, reason: str) -> int:
        """Notify about app termination."""
        return self.critical(
            f"I had to close '{app_name}'. {reason}\n\n"
            "If you think this is a mistake, talk to your parents.",
            "App Closed"
        )


# Module-level singleton for easy access
_notifier: Optional[Notifier] = None


def get_notifier() -> Notifier:
    """Get the global notifier instance."""
    global _notifier
    if _notifier is None:
        _notifier = Notifier()
    return _notifier


def notify(summary: str, body: str = "", **kwargs) -> int:
    """Convenience function to send a notification."""
    return get_notifier().notify(summary, body, **kwargs)


# CLI for testing
def main():
    """Test the notification system."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Test playtimed D-Bus notifications"
    )
    parser.add_argument("message", nargs="?", default="Hello from Claude!",
                        help="Message to send")
    parser.add_argument("-t", "--title", default="playtimed test",
                        help="Notification title")
    parser.add_argument("-i", "--icon", default="dialog-information",
                        help="Icon name")
    parser.add_argument("-u", "--urgency", choices=["low", "normal", "critical"],
                        default="normal", help="Urgency level")
    parser.add_argument("--info", action="store_true",
                        help="Show server info and exit")
    parser.add_argument("--time-warning", type=int, metavar="MINUTES",
                        help="Send a time warning notification")
    parser.add_argument("--discovery", metavar="APP_NAME",
                        help="Send a discovery notification")

    args = parser.parse_args()

    # Set up logging
    logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')

    notifier = Notifier()

    if not notifier.available:
        print("ERROR: D-Bus notifications not available", file=sys.stderr)
        print("\nPossible causes:")
        print("  - dbus-python not installed (pip install dbus-python)")
        print("  - No notification daemon running")
        print("  - Not running in a desktop session")
        sys.exit(1)

    if args.info:
        print(f"Server: {notifier.server_name}")
        print(f"Is KDE: {notifier.is_kde}")
        print(f"Supports actions: {notifier.supports_actions}")
        print(f"Supports persistence: {notifier.supports_persistence}")
        print(f"Supports markup: {notifier.supports_body_markup}")
        sys.exit(0)

    if args.time_warning:
        nid = notifier.time_warning(args.time_warning, "Anders")
        print(f"Sent time warning (id={nid})")
        sys.exit(0)

    if args.discovery:
        nid = notifier.discovery_notice(args.discovery, "Anders")
        print(f"Sent discovery notice (id={nid})")
        sys.exit(0)

    # Regular message
    urgency_map = {
        "low": Notifier.URGENCY_LOW,
        "normal": Notifier.URGENCY_NORMAL,
        "critical": Notifier.URGENCY_CRITICAL,
    }

    nid = notifier.notify(
        args.title,
        args.message,
        icon=args.icon,
        urgency=urgency_map[args.urgency]
    )

    if nid:
        print(f"Notification sent (id={nid})")
    else:
        print("Failed to send notification", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
