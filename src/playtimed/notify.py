"""
D-Bus notification support for playtimed.

Provides a NotificationBackend abstraction with priority-based fallback:
1. ClippyBackend - Future animated Clippy widget (org.playtimed.Clippy)
2. FreedesktopBackend - Standard desktop notifications (KDE, GNOME, etc.)
3. LogOnlyBackend - Fallback when no notification daemon available
"""

import logging
from typing import Optional, Protocol, runtime_checkable

log = logging.getLogger("playtimed.notify")

# Try to import dbus
try:
    import dbus
    DBUS_AVAILABLE = True
except ImportError:
    DBUS_AVAILABLE = False
    log.debug("dbus module not available")


# Urgency levels (shared across backends)
URGENCY_LOW = 0
URGENCY_NORMAL = 1
URGENCY_CRITICAL = 2


@runtime_checkable
class NotificationBackend(Protocol):
    """Protocol for notification delivery backends."""

    @property
    def name(self) -> str:
        """Backend identifier for logging."""
        ...

    def is_available(self) -> bool:
        """Check if this backend can deliver notifications."""
        ...

    def send(
        self,
        title: str,
        body: str,
        urgency: int = URGENCY_NORMAL,
        icon: str = "dialog-information",
        replaces_id: int = 0,
        timeout: int = -1,
    ) -> int:
        """
        Send a notification.

        Returns notification ID (>0) on success, 0 on failure.
        """
        ...

    def close(self, notification_id: int) -> bool:
        """Close/dismiss a notification by ID."""
        ...


class LogOnlyBackend:
    """Fallback backend that just logs notifications."""

    @property
    def name(self) -> str:
        return "log"

    def is_available(self) -> bool:
        return True  # Always available

    def send(
        self,
        title: str,
        body: str,
        urgency: int = URGENCY_NORMAL,
        icon: str = "dialog-information",
        replaces_id: int = 0,
        timeout: int = -1,
    ) -> int:
        urgency_str = {URGENCY_LOW: "LOW", URGENCY_NORMAL: "NORMAL", URGENCY_CRITICAL: "CRITICAL"}
        log.info(f"[{urgency_str.get(urgency, '?')}] {title}: {body}")
        # Return fake ID (negative to distinguish from real IDs)
        return -1

    def close(self, notification_id: int) -> bool:
        return True


class ClippyBackend:
    """
    Future: Animated Clippy notification widget.

    Will connect to org.playtimed.Clippy D-Bus service provided by
    a KDE Plasma widget.
    """

    DBUS_SERVICE = "org.playtimed.Clippy"
    DBUS_PATH = "/org/playtimed/Clippy"
    DBUS_INTERFACE = "org.playtimed.Clippy"

    def __init__(self):
        self._available = False
        self._interface = None

        if DBUS_AVAILABLE:
            self._connect()

    def _connect(self) -> bool:
        """Try to connect to Clippy D-Bus service."""
        try:
            bus = dbus.SessionBus()
            # Check if service exists
            bus.get_name_owner(self.DBUS_SERVICE)

            obj = bus.get_object(self.DBUS_SERVICE, self.DBUS_PATH)
            self._interface = dbus.Interface(obj, self.DBUS_INTERFACE)
            self._available = True
            log.info("Connected to Clippy notification service")
            return True

        except dbus.exceptions.DBusException:
            # Service not available - this is expected until widget is built
            self._available = False
            return False

    @property
    def name(self) -> str:
        return "clippy"

    def is_available(self) -> bool:
        return self._available

    def send(
        self,
        title: str,
        body: str,
        urgency: int = URGENCY_NORMAL,
        icon: str = "dialog-information",
        replaces_id: int = 0,
        timeout: int = -1,
    ) -> int:
        if not self._available:
            return 0

        try:
            # Future: call Clippy D-Bus method
            # notification_id = self._interface.ShowMessage(title, body, urgency)
            # return int(notification_id)
            return 0  # Not implemented yet
        except dbus.exceptions.DBusException as e:
            log.error(f"Clippy notification failed: {e}")
            return 0

    def close(self, notification_id: int) -> bool:
        if not self._available:
            return False
        # Future: self._interface.DismissMessage(notification_id)
        return False


class FreedesktopBackend:
    """
    Standard freedesktop.org notification backend.

    Works with KDE Plasma, GNOME, and other compliant desktop environments.
    """

    DBUS_SERVICE = "org.freedesktop.Notifications"
    DBUS_PATH = "/org/freedesktop/Notifications"
    DBUS_INTERFACE = "org.freedesktop.Notifications"

    def __init__(self, app_name: str = "playtimed"):
        self.app_name = app_name
        self._bus = None
        self._interface = None
        self._server_caps = []
        self._server_name = None
        self._available = False

        if DBUS_AVAILABLE:
            self._connect()

    def _connect(self) -> bool:
        """Connect to the session bus and notification service."""
        try:
            self._bus = dbus.SessionBus()
            notify_obj = self._bus.get_object(self.DBUS_SERVICE, self.DBUS_PATH)
            self._interface = dbus.Interface(notify_obj, self.DBUS_INTERFACE)

            # Get server info
            info = self._interface.GetServerInformation()
            self._server_name = str(info[0])
            server_vendor = str(info[1])
            server_version = str(info[2])
            log.info(f"Connected to notification server: {self._server_name} "
                     f"({server_vendor} {server_version})")

            # Get capabilities
            self._server_caps = [str(c) for c in self._interface.GetCapabilities()]
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
    def name(self) -> str:
        return "freedesktop"

    def is_available(self) -> bool:
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

    def send(
        self,
        title: str,
        body: str,
        urgency: int = URGENCY_NORMAL,
        icon: str = "dialog-information",
        replaces_id: int = 0,
        timeout: int = -1,
    ) -> int:
        if not self._available:
            return 0

        hints = {'urgency': dbus.Byte(urgency)}
        actions = []

        try:
            notification_id = self._interface.Notify(
                self.app_name,
                replaces_id,
                icon,
                title,
                body,
                actions,
                hints,
                timeout
            )
            log.debug(f"Sent notification {notification_id}: {title}")
            return int(notification_id)

        except dbus.exceptions.DBusException as e:
            log.error(f"Failed to send notification: {e}")
            return 0

    def close(self, notification_id: int) -> bool:
        if not self._available or notification_id <= 0:
            return False

        try:
            self._interface.CloseNotification(notification_id)
            return True
        except dbus.exceptions.DBusException:
            return False


class NotificationDispatcher:
    """
    Dispatches notifications through available backends with priority fallback.

    Priority order:
    1. Clippy (animated widget) - if available
    2. Freedesktop (KDE/GNOME) - standard desktop notifications
    3. Log-only - always available fallback
    """

    def __init__(self, app_name: str = "playtimed"):
        self.backends: list[NotificationBackend] = [
            ClippyBackend(),
            FreedesktopBackend(app_name),
            LogOnlyBackend(),
        ]
        self._last_backend: Optional[str] = None

    @property
    def available_backend(self) -> Optional[NotificationBackend]:
        """Get the first available backend."""
        for backend in self.backends:
            if backend.is_available():
                return backend
        return None

    @property
    def backend_name(self) -> str:
        """Name of the backend that will be used."""
        backend = self.available_backend
        return backend.name if backend else "none"

    def send(
        self,
        title: str,
        body: str,
        urgency: int = URGENCY_NORMAL,
        icon: str = "dialog-information",
        replaces_id: int = 0,
        timeout: int = -1,
    ) -> tuple[int, str]:
        """
        Send notification through first available backend.

        Returns (notification_id, backend_name).
        """
        for backend in self.backends:
            if backend.is_available():
                result = backend.send(title, body, urgency, icon, replaces_id, timeout)
                if result != 0:
                    self._last_backend = backend.name
                    return result, backend.name
        return 0, "failed"

    def close(self, notification_id: int) -> bool:
        """Close notification using the backend that sent it."""
        for backend in self.backends:
            if backend.is_available() and backend.close(notification_id):
                return True
        return False

    # Convenience methods with standard messaging

    def info(self, message: str, title: str = "Claude says...") -> int:
        """Send an informational notification."""
        nid, _ = self.send(title, message, URGENCY_NORMAL, "dialog-information")
        return nid

    def warning(self, message: str, title: str = "Heads up...") -> int:
        """Send a warning notification."""
        nid, _ = self.send(title, message, URGENCY_NORMAL, "dialog-warning")
        return nid

    def critical(self, message: str, title: str = "Important!") -> int:
        """Send a critical notification that persists."""
        nid, _ = self.send(title, message, URGENCY_CRITICAL, "dialog-error", timeout=0)
        return nid


# Module-level singleton
_dispatcher: Optional[NotificationDispatcher] = None


def get_dispatcher() -> NotificationDispatcher:
    """Get the global notification dispatcher."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = NotificationDispatcher()
    return _dispatcher


def send(title: str, body: str = "", **kwargs) -> int:
    """Convenience function to send a notification."""
    nid, _ = get_dispatcher().send(title, body, **kwargs)
    return nid


# Backwards compatibility alias
Notifier = FreedesktopBackend
get_notifier = lambda: get_dispatcher().backends[1]  # FreedesktopBackend


# CLI for testing
def main():
    """Test the notification system."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Test playtimed notification backends"
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
                        help="Show backend info and exit")
    parser.add_argument("--backend", choices=["clippy", "freedesktop", "log"],
                        help="Force specific backend")

    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')

    dispatcher = NotificationDispatcher()

    if args.info:
        print("Available backends:")
        for backend in dispatcher.backends:
            status = "available" if backend.is_available() else "unavailable"
            print(f"  {backend.name}: {status}")

            if isinstance(backend, FreedesktopBackend) and backend.is_available():
                print(f"    Server: {backend.server_name}")
                print(f"    Is KDE: {backend.is_kde}")
                print(f"    Supports actions: {backend.supports_actions}")
                print(f"    Supports persistence: {backend.supports_persistence}")

        print(f"\nWill use: {dispatcher.backend_name}")
        sys.exit(0)

    urgency_map = {
        "low": URGENCY_LOW,
        "normal": URGENCY_NORMAL,
        "critical": URGENCY_CRITICAL,
    }

    # Send via specific backend or dispatcher
    if args.backend:
        backend = next((b for b in dispatcher.backends if b.name == args.backend), None)
        if not backend:
            print(f"Unknown backend: {args.backend}", file=sys.stderr)
            sys.exit(1)
        if not backend.is_available():
            print(f"Backend {args.backend} not available", file=sys.stderr)
            sys.exit(1)
        nid = backend.send(args.title, args.message, urgency_map[args.urgency], args.icon)
        backend_used = args.backend
    else:
        nid, backend_used = dispatcher.send(
            args.title, args.message, urgency_map[args.urgency], args.icon
        )

    if nid:
        print(f"Notification sent via {backend_used} (id={nid})")
    else:
        print("Failed to send notification", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
