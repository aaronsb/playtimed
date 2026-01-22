"""Tests for playtimed message router functionality."""

import os
import tempfile

import pytest

from playtimed.db import ActivityDB
from playtimed.router import MessageRouter, MessageContext


@pytest.fixture
def db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name

    try:
        db = ActivityDB(db_path)
        yield db
    finally:
        os.unlink(db_path)


@pytest.fixture
def router(db):
    """Create a router with a test database."""
    return MessageRouter(db)


class TestMessageContext:
    """Tests for MessageContext dataclass."""

    def test_default_values(self):
        """Test that MessageContext has sensible defaults."""
        ctx = MessageContext()
        assert ctx.user == ""
        assert ctx.process == ""
        assert ctx.time_left == 0
        assert ctx.grace_seconds == 0

    def test_to_dict(self):
        """Test converting context to dict for rendering."""
        ctx = MessageContext(
            user="anders",
            process="Minecraft",
            time_left=30,
            category="gaming"
        )
        d = ctx.to_dict()
        assert d['user'] == "anders"
        assert d['process'] == "Minecraft"
        assert d['time_left'] == "30"  # Converted to string
        assert d['category'] == "gaming"


class TestMessageRouter:
    """Tests for MessageRouter class."""

    def test_send_with_template(self, router, db):
        """Test sending a notification with a template."""
        ctx = MessageContext(
            user="anders",
            process="Minecraft",
            time_left=30
        )
        notification_id, backend = router.send('process_start', ctx)

        # Should return something
        assert notification_id >= 0
        assert backend in ('freedesktop', 'log')

    def test_send_fallback_no_template(self, router, db):
        """Test fallback when no template exists."""
        # Use an intention that doesn't exist
        ctx = MessageContext(user="anders")
        notification_id, backend = router.send('nonexistent_intention', ctx)

        # Should still send via fallback
        assert notification_id >= 0

    def test_variable_rendering(self, router):
        """Test that variables are rendered in templates."""
        template = "Hello {user}, you have {time_left} minutes left!"
        context = {'user': 'anders', 'time_left': '30'}

        rendered = router._render(template, context)
        assert rendered == "Hello anders, you have 30 minutes left!"

    def test_variable_rendering_missing_var(self, router):
        """Test that missing variables are left as-is."""
        template = "Hello {user}, your score is {score}!"
        context = {'user': 'anders'}  # 'score' is missing

        rendered = router._render(template, context)
        assert rendered == "Hello anders, your score is {score}!"

    def test_message_logging(self, router, db):
        """Test that messages are logged to database."""
        ctx = MessageContext(user="anders", process="Minecraft")
        router.send('process_start', ctx)

        messages = db.get_recent_messages(user="anders")
        assert len(messages) >= 1
        assert messages[0]['intention'] == 'process_start'
        assert 'anders' in messages[0]['rendered_body'] or 'Minecraft' in messages[0]['rendered_body']


class TestConvenienceMethods:
    """Tests for router convenience methods."""

    def test_process_started(self, router, db):
        """Test process_started convenience method."""
        nid = router.process_started("anders", "Minecraft", 45)
        assert nid >= 0

        messages = db.get_recent_messages(user="anders")
        assert len(messages) >= 1
        assert messages[0]['intention'] == 'process_start'

    def test_time_warning(self, router, db):
        """Test time_warning convenience method."""
        # Test 30-minute warning
        nid = router.time_warning("anders", 30)
        assert nid >= 0

        # Test 15-minute warning
        nid = router.time_warning("anders", 15)
        assert nid >= 0

        # Test 5-minute warning
        nid = router.time_warning("anders", 5)
        assert nid >= 0

        # Test no warning for high time remaining
        nid = router.time_warning("anders", 60)
        assert nid == 0  # No warning needed

    def test_enforcement(self, router, db):
        """Test enforcement convenience method."""
        nid = router.enforcement("anders", "Minecraft")
        assert nid >= 0

        messages = db.get_recent_messages(user="anders")
        assert any(m['intention'] == 'enforcement' for m in messages)

    def test_blocked_launch(self, router, db):
        """Test blocked_launch convenience method."""
        nid = router.blocked_launch("anders", "Minecraft")
        assert nid >= 0

        messages = db.get_recent_messages(user="anders")
        assert any(m['intention'] == 'blocked_launch' for m in messages)


class TestNotificationReplacement:
    """Tests for notification replacement feature."""

    def test_replace_previous(self, router):
        """Test that replace_previous tracks notification IDs."""
        ctx = MessageContext(user="anders", grace_seconds=30)

        # First notification
        nid1, _ = router.send('grace_period', ctx, replace_previous=False)

        # Verify the router tracks the notification ID for replacement
        assert 'grace_period' in router._last_notification
        assert router._last_notification['grace_period'] == nid1

    def test_close_notification(self, router):
        """Test closing a notification by intention."""
        ctx = MessageContext(user="anders", grace_seconds=30)
        router.send('grace_period', ctx)

        # Should be able to close it
        result = router.close_notification('grace_period')
        # Result depends on backend, but should not crash
        assert isinstance(result, bool)
