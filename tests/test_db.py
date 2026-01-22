"""Tests for playtimed database functionality."""

import os
import tempfile
from datetime import datetime

import pytest

from playtimed.db import ActivityDB, init_db, migrate_db


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


class TestPatternManagement:
    """Tests for process pattern management."""

    def test_add_pattern(self, db):
        """Test adding a new pattern."""
        pattern_id = db.add_pattern(
            pattern="factorio",
            name="Factorio",
            category="gaming",
            cpu_threshold=10.0
        )
        assert pattern_id > 0

        patterns = db.get_all_patterns()
        assert len(patterns) == 1
        assert patterns[0]['name'] == "Factorio"
        assert patterns[0]['monitor_state'] == 'active'

    def test_add_pattern_with_owner(self, db):
        """Test adding a pattern for a specific user."""
        pattern_id = db.add_pattern(
            pattern="minecraft",
            name="Minecraft",
            category="gaming",
            owner="anders"
        )

        patterns = db.get_patterns(owner="anders")
        assert len(patterns) == 1
        assert patterns[0]['owner'] == "anders"

    def test_get_patterns_by_state(self, db):
        """Test filtering patterns by state."""
        db.add_pattern("game1", "Game 1", "gaming", monitor_state='active')
        db.add_pattern("game2", "Game 2", "gaming", monitor_state='discovered')
        db.add_pattern("game3", "Game 3", "gaming", monitor_state='ignored')

        active = db.get_patterns_by_state('active')
        assert len(active) == 1
        assert active[0]['name'] == "Game 1"

        discovered = db.get_patterns_by_state('discovered')
        assert len(discovered) == 1
        assert discovered[0]['name'] == "Game 2"

    def test_set_pattern_state(self, db):
        """Test changing a pattern's state."""
        pattern_id = db.add_pattern("test", "Test", "gaming", monitor_state='discovered')

        db.set_pattern_state(pattern_id, 'active', category='gaming')

        patterns = db.get_patterns_by_state('active')
        assert len(patterns) == 1
        assert patterns[0]['id'] == pattern_id


class TestDiscovery:
    """Tests for process discovery functionality."""

    def test_discover_pattern(self, db):
        """Test creating a discovered pattern."""
        pattern_id = db.discover_pattern(
            pattern="newgame",
            name="New Game",
            owner="anders",
            cmdline="/usr/bin/newgame --fullscreen"
        )

        patterns = db.get_patterns_by_state('discovered')
        assert len(patterns) == 1
        assert patterns[0]['name'] == "New Game"
        assert patterns[0]['owner'] == "anders"
        assert patterns[0]['discovered_cmdline'] == "/usr/bin/newgame --fullscreen"

    def test_get_pattern_by_name_and_owner(self, db):
        """Test finding patterns by name and owner."""
        db.discover_pattern("game", "Test Game", "anders")

        # Should find for specific owner
        found = db.get_pattern_by_name_and_owner("Test Game", "anders")
        assert found is not None
        assert found['name'] == "Test Game"

        # Should not find for different owner (unless pattern has no owner)
        not_found = db.get_pattern_by_name_and_owner("Test Game", "other_user")
        assert not_found is None

    def test_discovery_config(self, db):
        """Test discovery configuration."""
        config = db.get_discovery_config()
        assert config['enabled'] is True
        assert config['cpu_threshold'] == 25.0
        assert config['sample_window_seconds'] == 120
        assert config['min_samples'] == 3

        db.set_discovery_config('cpu_threshold', '35')
        config = db.get_discovery_config()
        assert config['cpu_threshold'] == 35.0


class TestStatistics:
    """Tests for runtime statistics tracking."""

    def test_record_pid_seen(self, db):
        """Test recording unique PIDs."""
        pattern_id = db.add_pattern("test", "Test", "gaming")

        # First PID should be new
        is_new = db.record_pid_seen(pattern_id, 1234)
        assert is_new is True

        # Same PID should not be new
        is_new = db.record_pid_seen(pattern_id, 1234)
        assert is_new is False

        # Different PID should be new
        is_new = db.record_pid_seen(pattern_id, 5678)
        assert is_new is True

        # Check count updated
        patterns = db.get_all_patterns()
        assert patterns[0]['unique_pid_count'] == 2

    def test_add_runtime(self, db):
        """Test adding runtime to a pattern."""
        pattern_id = db.add_pattern("test", "Test", "gaming")

        db.add_runtime(pattern_id, 30)
        db.add_runtime(pattern_id, 30)

        patterns = db.get_all_patterns()
        assert patterns[0]['total_runtime_seconds'] == 60

    def test_cleanup_seen_pids(self, db):
        """Test cleaning up old PID records."""
        pattern_id = db.add_pattern("test", "Test", "gaming")
        db.record_pid_seen(pattern_id, 1234)

        # Should not delete recent PIDs
        db.cleanup_seen_pids(days=7)

        # PID count should still be 1
        patterns = db.get_all_patterns()
        assert patterns[0]['unique_pid_count'] == 1


class TestUserManagement:
    """Tests for user limit management."""

    def test_set_user_limits(self, db):
        """Test setting user limits."""
        db.set_user_limits(
            "anders",
            daily_total=180,
            gaming_limit=120,
            weekday_start="16:00",
            weekday_end="21:00"
        )

        limits = db.get_user_limits("anders")
        assert limits['daily_total'] == 180
        assert limits['gaming_limit'] == 120
        assert limits['weekday_start'] == "16:00"

    def test_get_all_monitored_users(self, db):
        """Test getting list of monitored users."""
        db.set_user_limits("anders")
        db.set_user_limits("other", enabled=0)

        users = db.get_all_monitored_users()
        assert "anders" in users
        assert "other" not in users  # Disabled


class TestMigration:
    """Tests for database migration."""

    def test_migration_is_idempotent(self, db):
        """Test that running migration multiple times is safe."""
        # Migration already ran in fixture
        migrate_db(db.db_path)
        migrate_db(db.db_path)

        # Should still work
        patterns = db.get_all_patterns()
        assert patterns is not None


class TestDaemonConfig:
    """Tests for daemon configuration."""

    def test_get_daemon_config(self, db):
        """Test getting daemon config with defaults."""
        config = db.get_daemon_config()
        assert config['mode'] == 'normal'
        assert config['strict_grace_seconds'] == 30

    def test_set_daemon_mode(self, db):
        """Test setting daemon mode."""
        db.set_daemon_mode('passthrough')
        assert db.get_daemon_mode() == 'passthrough'

        db.set_daemon_mode('strict')
        assert db.get_daemon_mode() == 'strict'

        db.set_daemon_mode('normal')
        assert db.get_daemon_mode() == 'normal'

    def test_invalid_daemon_mode(self, db):
        """Test that invalid modes are rejected."""
        import pytest
        with pytest.raises(ValueError):
            db.set_daemon_mode('invalid_mode')


class TestDailySummary:
    """Tests for daily summary tracking."""

    def test_get_time_used_today(self, db):
        """Test getting time used today."""
        db.set_user_limits("anders")
        db.update_daily_summary("anders", gaming_seconds=3600, total_seconds=3600)

        total, gaming = db.get_time_used_today("anders")
        assert total == 3600
        assert gaming == 3600

    def test_increment_session_count(self, db):
        """Test incrementing session count."""
        db.set_user_limits("anders")
        db.increment_session_count("anders")
        db.increment_session_count("anders")

        summary = db.get_daily_summary("anders")
        assert summary['session_count'] == 2


class TestPatternNotes:
    """Tests for pattern notes."""

    def test_set_and_get_pattern_notes(self, db):
        """Test setting and getting notes on a pattern."""
        pattern_id = db.add_pattern("test", "Test", "gaming")
        db.set_pattern_notes(pattern_id, "This is a test pattern")

        pattern = db.get_pattern_by_id(pattern_id)
        assert pattern['notes'] == "This is a test pattern"

    def test_update_pattern_notes(self, db):
        """Test updating notes on a pattern."""
        pattern_id = db.add_pattern("test", "Test", "gaming", notes="Initial notes")
        db.set_pattern_notes(pattern_id, "Updated notes")

        pattern = db.get_pattern_by_id(pattern_id)
        assert pattern['notes'] == "Updated notes"

    def test_get_pattern_by_id(self, db):
        """Test getting a pattern by ID."""
        pattern_id = db.add_pattern("test", "Test Pattern", "gaming")
        pattern = db.get_pattern_by_id(pattern_id)

        assert pattern is not None
        assert pattern['name'] == "Test Pattern"
        assert pattern['id'] == pattern_id

    def test_get_nonexistent_pattern(self, db):
        """Test getting a pattern that doesn't exist."""
        pattern = db.get_pattern_by_id(9999)
        assert pattern is None
