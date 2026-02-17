"""
Message Router for playtimed.

Handles template selection, variable rendering, and notification delivery.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .db import ActivityDB
from .notify import (
    NotificationDispatcher,
    get_dispatcher,
    URGENCY_LOW,
    URGENCY_NORMAL,
    URGENCY_CRITICAL,
)

log = logging.getLogger("playtimed.router")


@dataclass
class MessageContext:
    """Context for message variable rendering."""
    user: str = ""
    process: str = ""
    pattern: str = ""
    time_left: int = 0      # minutes
    time_used: int = 0      # minutes
    time_limit: int = 0     # minutes
    category: str = ""
    day: str = ""
    mode: str = ""
    grace_seconds: int = 0
    allowed_window: str = ""  # human-readable schedule window

    def to_dict(self) -> dict:
        """Convert to dict for template rendering."""
        return {
            'user': self.user,
            'process': self.process,
            'pattern': self.pattern,
            'time_left': str(self.time_left),
            'time_used': str(self.time_used),
            'time_limit': str(self.time_limit),
            'category': self.category,
            'day': self.day,
            'mode': self.mode,
            'grace_seconds': str(self.grace_seconds),
            'allowed_window': self.allowed_window,
        }


class MessageRouter:
    """
    Routes events to notifications via templates.

    Handles:
    - Template selection (random variant for variety)
    - Variable rendering (fills in {user}, {process}, etc.)
    - Delivery via NotificationDispatcher
    - Logging to message_log table
    """

    # Map urgency strings to constants
    URGENCY_MAP = {
        'low': URGENCY_LOW,
        'normal': URGENCY_NORMAL,
        'critical': URGENCY_CRITICAL,
    }

    def __init__(self, db: ActivityDB, dispatcher: NotificationDispatcher = None):
        self.db = db
        self.dispatcher = dispatcher or get_dispatcher()

        # Cache for last notification ID per intention (for replacement)
        self._last_notification: dict[str, int] = {}

    def send(
        self,
        intention: str,
        context: MessageContext = None,
        replace_previous: bool = False,
        **kwargs
    ) -> tuple[int, str]:
        """
        Send a notification for the given intention.

        Args:
            intention: Message intention (e.g., 'process_start', 'time_warning_30')
            context: MessageContext with variables for rendering
            replace_previous: If True, replace the last notification of this intention
            **kwargs: Additional context variables (override MessageContext)

        Returns:
            (notification_id, backend_name) tuple
        """
        # Build context
        if context is None:
            context = MessageContext()

        ctx_dict = context.to_dict()
        ctx_dict.update(kwargs)  # Allow overrides

        # Get random template for this intention
        template = self.db.get_random_template(intention)

        if not template:
            log.warning(f"No template found for intention: {intention}")
            # Fallback: send basic message
            return self._send_fallback(intention, ctx_dict)

        # Render template
        title = self._render(template['title'], ctx_dict)
        body = self._render(template['body'], ctx_dict)

        # Get urgency
        urgency = self.URGENCY_MAP.get(template['urgency'], URGENCY_NORMAL)

        # Handle replacement
        replaces_id = 0
        if replace_previous and intention in self._last_notification:
            replaces_id = self._last_notification[intention]

        # Send via dispatcher (target user's session bus if we know the user)
        target_user = ctx_dict.get('user') or None
        notification_id, backend = self.dispatcher.send(
            title=title,
            body=body,
            urgency=urgency,
            icon=template['icon'],
            replaces_id=replaces_id,
            target_user=target_user,
        )

        # Track for potential replacement
        if notification_id > 0:
            self._last_notification[intention] = notification_id

        # Log to database
        user = ctx_dict.get('user', '')
        self._log_message(
            user=user,
            intention=intention,
            template_id=template['id'],
            rendered_title=title,
            rendered_body=body,
            notification_id=notification_id,
            backend=backend,
        )

        log.debug(f"Sent {intention} via {backend}: {title}")
        return notification_id, backend

    def _render(self, template: str, context: dict) -> str:
        """
        Render a template with context variables.

        Uses {variable} syntax. Missing variables are left as-is.
        """
        def replace_var(match):
            var_name = match.group(1)
            return context.get(var_name, match.group(0))

        return re.sub(r'\{(\w+)\}', replace_var, template)

    def _send_fallback(self, intention: str, context: dict) -> tuple[int, str]:
        """Send a fallback message when no template exists."""
        # Basic fallback messages
        fallbacks = {
            'process_start': ("Game started", "{process} is now running."),
            'process_end': ("Game ended", "{process} has closed."),
            'time_warning_30': ("30 minutes left", "You have 30 minutes remaining."),
            'time_warning_15': ("15 minutes left", "You have 15 minutes remaining."),
            'time_warning_5': ("5 minutes left", "Almost time to stop!"),
            'time_expired': ("Time is up", "Your gaming time has ended."),
            'enforcement': ("Game closed", "{process} was terminated."),
            'blocked_launch': ("Blocked", "{process} cannot run right now."),
            'discovery': ("New app", "Detected {process}."),
        }

        title, body = fallbacks.get(intention, (intention, "Notification"))
        title = self._render(title, context)
        body = self._render(body, context)

        target_user = context.get('user') or None
        notification_id, backend = self.dispatcher.send(
            title, body, target_user=target_user
        )

        # Log even fallback messages
        self._log_message(
            user=context.get('user', ''),
            intention=intention,
            template_id=None,
            rendered_title=title,
            rendered_body=body,
            notification_id=notification_id,
            backend=backend,
        )

        return notification_id, backend

    def _log_message(self, user: str, intention: str, template_id: Optional[int],
                     rendered_title: str, rendered_body: str,
                     notification_id: int, backend: str):
        """Log message to database."""
        try:
            self.db.log_message(
                user=user,
                intention=intention,
                template_id=template_id,
                rendered_title=rendered_title,
                rendered_body=rendered_body,
                notification_id=notification_id,
                backend=backend,
            )
        except Exception as e:
            # Logging failure shouldn't break notifications
            log.error(f"Failed to log message: {e}")

    def close_notification(self, intention: str) -> bool:
        """Close the last notification for an intention."""
        if intention in self._last_notification:
            notification_id = self._last_notification[intention]
            if self.dispatcher.close(notification_id):
                del self._last_notification[intention]
                return True
        return False

    # Convenience methods for common events

    def process_started(self, user: str, process: str, time_left: int,
                        category: str = "gaming") -> int:
        """Notify that a tracked process started."""
        ctx = MessageContext(
            user=user,
            process=process,
            time_left=time_left,
            category=category,
            day=datetime.now().strftime("%A"),
        )
        nid, _ = self.send('process_start', ctx)
        return nid

    def process_ended(self, user: str, process: str, time_left: int) -> int:
        """Notify that a tracked process ended."""
        ctx = MessageContext(user=user, process=process, time_left=time_left)
        nid, _ = self.send('process_end', ctx)
        return nid

    def time_warning(self, user: str, minutes_left: int, time_limit: int = 0) -> int:
        """Send appropriate time warning based on minutes remaining."""
        ctx = MessageContext(
            user=user,
            time_left=minutes_left,
            time_limit=time_limit,
        )

        if minutes_left <= 5:
            intention = 'time_warning_5'
        elif minutes_left <= 15:
            intention = 'time_warning_15'
        elif minutes_left <= 30:
            intention = 'time_warning_30'
        else:
            # No warning needed
            return 0

        nid, _ = self.send(intention, ctx)
        return nid

    def time_expired(self, user: str, time_limit: int) -> int:
        """Notify that time limit has been reached."""
        ctx = MessageContext(user=user, time_limit=time_limit)
        nid, _ = self.send('time_expired', ctx)
        return nid

    def grace_period(self, user: str, seconds_remaining: int) -> int:
        """Send grace period countdown notification."""
        ctx = MessageContext(user=user, grace_seconds=seconds_remaining)
        nid, _ = self.send('grace_period', ctx, replace_previous=True)
        return nid

    def enforcement(self, user: str, process: str) -> int:
        """Notify that a process was terminated."""
        ctx = MessageContext(user=user, process=process)
        nid, _ = self.send('enforcement', ctx)
        return nid

    def blocked_launch(self, user: str, process: str) -> int:
        """Notify that a process launch was blocked."""
        ctx = MessageContext(user=user, process=process)
        nid, _ = self.send('blocked_launch', ctx)
        return nid

    def outside_hours(self, user: str, allowed_window: str) -> int:
        """Notify that gaming is outside allowed hours."""
        ctx = MessageContext(user=user, allowed_window=allowed_window)
        nid, _ = self.send('outside_hours', ctx)
        return nid

    def discovery(self, user: str, process: str) -> int:
        """Notify about a newly discovered process."""
        ctx = MessageContext(user=user, process=process)
        nid, _ = self.send('discovery', ctx)
        return nid

    def day_reset(self, user: str, time_limit: int) -> int:
        """Notify about daily reset."""
        ctx = MessageContext(
            user=user,
            time_limit=time_limit,
            day=datetime.now().strftime("%A"),
        )
        nid, _ = self.send('day_reset', ctx)
        return nid

    def mode_change(self, mode: str) -> int:
        """Notify about daemon mode change."""
        ctx = MessageContext(mode=mode)
        nid, _ = self.send('mode_change', ctx)
        return nid


# Module-level singleton
_router: Optional[MessageRouter] = None


def get_router(db: ActivityDB = None) -> MessageRouter:
    """Get the global message router instance."""
    global _router
    if _router is None:
        if db is None:
            db = ActivityDB()
        _router = MessageRouter(db)
    return _router
