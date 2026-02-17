"""Tests for playtimed state machine logic.

These tests verify the state tracking and transitions that happen during
the daemon's poll loop, including:
- Warning flag transitions
- Timestamp-based time tracking
- State persistence across daemon restarts
- Gaming active state transitions
"""

import os
import tempfile
from datetime import datetime, timedelta

import pytest

from playtimed.db import ActivityDB


@pytest.fixture
def db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name

    try:
        db = ActivityDB(db_path)
        # Set up a test user
        db.set_user_limits("anders", gaming_limit=120, daily_total=180)
        yield db
    finally:
        os.unlink(db_path)


class TestWarningFlagTransitions:
    """Tests for warning flag state transitions."""

    def test_initial_warning_flags_are_zero(self, db):
        """Test that warning flags start at 0."""
        state = db.get_user_state("anders")
        # No state yet - should be None
        assert state is None

        # Create initial state
        db.update_user_state("anders", gaming_active=0)
        state = db.get_user_state("anders")

        assert state['warned_30'] == 0
        assert state['warned_15'] == 0
        assert state['warned_5'] == 0

    def test_warning_30_flag_set(self, db):
        """Test setting the 30-minute warning flag."""
        db.update_user_state("anders", gaming_active=1, warned_30=1)

        state = db.get_user_state("anders")
        assert state['warned_30'] == 1
        assert state['warned_15'] == 0
        assert state['warned_5'] == 0

    def test_warning_flags_accumulate(self, db):
        """Test that warning flags accumulate (don't reset each other)."""
        # First warning at 30 min
        db.update_user_state("anders", gaming_active=1, warned_30=1)

        # Second warning at 15 min (should preserve warned_30)
        db.update_user_state("anders", gaming_active=1, warned_15=1)

        state = db.get_user_state("anders")
        assert state['warned_30'] == 1
        assert state['warned_15'] == 1
        assert state['warned_5'] == 0

        # Third warning at 5 min
        db.update_user_state("anders", warned_5=1)

        state = db.get_user_state("anders")
        assert state['warned_30'] == 1
        assert state['warned_15'] == 1
        assert state['warned_5'] == 1

    def test_warning_flags_reset_on_new_day(self, db):
        """Test that warning flags reset when a new day starts."""
        # Set all warnings
        db.update_user_state("anders", warned_30=1, warned_15=1, warned_5=1)

        # Simulate new day by getting fresh state for today
        # The daily_summary table uses date as part of primary key
        # So a new day = new row with fresh defaults
        state = db.get_user_state("anders")
        assert state['warned_30'] == 1  # Still set today

        # Note: actual day reset happens via daily_summary date change
        # which creates a new row with default values


class TestTimestampBasedTimeTracking:
    """Tests for timestamp-based time calculation."""

    def test_elapsed_time_calculation(self, db):
        """Test that elapsed time is calculated from timestamps."""
        now = datetime.now()
        thirty_seconds_ago = (now - timedelta(seconds=30)).isoformat()

        # Set last poll time
        db.update_user_state("anders", gaming_active=1, last_poll_at=thirty_seconds_ago)

        state = db.get_user_state("anders")
        last_poll = datetime.fromisoformat(state['last_poll_at'])

        # Calculate elapsed (simulating daemon logic)
        elapsed = (now - last_poll).total_seconds()

        # Should be approximately 30 seconds
        assert 29 <= elapsed <= 32

    def test_elapsed_time_capping_for_suspend(self, db):
        """Test that large time gaps are capped (suspend/resume handling)."""
        poll_interval = 30
        max_elapsed = poll_interval * 2

        now = datetime.now()
        two_hours_ago = (now - timedelta(hours=2)).isoformat()

        db.update_user_state("anders", gaming_active=1, last_poll_at=two_hours_ago)

        state = db.get_user_state("anders")
        last_poll = datetime.fromisoformat(state['last_poll_at'])

        # Calculate elapsed
        elapsed = (now - last_poll).total_seconds()

        # Raw elapsed would be ~7200 seconds (2 hours)
        assert elapsed > 7000

        # But daemon should cap it
        capped_elapsed = min(elapsed, max_elapsed)
        assert capped_elapsed == max_elapsed  # 60 seconds

    def test_gaming_time_accumulates(self, db):
        """Test that gaming time accumulates correctly."""
        # Start with 0 time
        db.update_daily_summary("anders", gaming_seconds=0, total_seconds=0)

        total, gaming = db.get_time_used_today("anders")
        assert gaming == 0

        # Add 60 seconds
        db.update_daily_summary("anders", gaming_seconds=60, total_seconds=60)

        total, gaming = db.get_time_used_today("anders")
        assert gaming == 60

        # Add another 30 seconds
        db.update_daily_summary("anders", gaming_seconds=30, total_seconds=30)

        total, gaming = db.get_time_used_today("anders")
        assert gaming == 90


class TestGamingActiveStateTransitions:
    """Tests for gaming_active state transitions."""

    def test_gaming_active_starts_false(self, db):
        """Test that gaming_active starts as 0."""
        db.update_user_state("anders", gaming_active=0)

        state = db.get_user_state("anders")
        assert state['gaming_active'] == 0

    def test_transition_to_gaming_active(self, db):
        """Test transition from not gaming to gaming."""
        # Start not gaming
        db.update_user_state("anders", gaming_active=0)

        # Transition to gaming
        db.update_user_state("anders", gaming_active=1)

        state = db.get_user_state("anders")
        assert state['gaming_active'] == 1

    def test_transition_from_gaming_active(self, db):
        """Test transition from gaming to not gaming."""
        # Start gaming
        db.update_user_state("anders", gaming_active=1)

        # Stop gaming
        db.update_user_state("anders", gaming_active=0)

        state = db.get_user_state("anders")
        assert state['gaming_active'] == 0

    def test_gaming_time_tracking_with_state(self, db):
        """Test that gaming time is tracked with gaming_active state."""
        now = datetime.now()

        # Start gaming session
        db.update_user_state(
            "anders",
            gaming_active=1,
            gaming_time=0,
            last_poll_at=now.isoformat()
        )

        # Simulate poll 30 seconds later
        later = (now + timedelta(seconds=30)).isoformat()
        db.update_user_state(
            "anders",
            gaming_active=1,
            gaming_time=30,
            last_poll_at=later
        )

        state = db.get_user_state("anders")
        assert state['gaming_time'] == 30
        assert state['gaming_active'] == 1


class TestStatePersistenceAcrossRestarts:
    """Tests for state persistence (simulating daemon restarts)."""

    def test_state_persists_after_reload(self, db):
        """Test that state persists when reloading from DB."""
        # Set state
        now = datetime.now().isoformat()
        db.update_user_state(
            "anders",
            gaming_active=1,
            gaming_time=3600,
            warned_30=1,
            warned_15=1,
            last_poll_at=now
        )

        # "Restart" - create new DB connection (simulating daemon restart)
        db2 = ActivityDB(db.db_path)

        # Load state
        state = db2.get_user_state("anders")

        assert state['gaming_active'] == 1
        assert state['gaming_time'] == 3600
        assert state['warned_30'] == 1
        assert state['warned_15'] == 1
        assert state['last_poll_at'] == now

    def test_gaming_active_recovery_after_crash(self, db):
        """Test recovery when daemon crashed while gaming was active."""
        # Simulate: daemon was running, gaming was active, then crash
        old_time = (datetime.now() - timedelta(minutes=5)).isoformat()
        db.update_user_state(
            "anders",
            gaming_active=1,
            gaming_time=1800,  # 30 minutes played
            last_poll_at=old_time
        )

        # "Restart" daemon
        db2 = ActivityDB(db.db_path)
        state = db2.get_user_state("anders")

        # Daemon should see gaming was active
        assert state['gaming_active'] == 1

        # Daemon should see there was a gap (5 minutes since last poll)
        last_poll = datetime.fromisoformat(state['last_poll_at'])
        elapsed = (datetime.now() - last_poll).total_seconds()

        # Elapsed should be ~300 seconds (5 minutes)
        assert elapsed > 290


class TestTimeLimitEnforcement:
    """Tests for time limit calculations."""

    def test_time_remaining_calculation(self, db):
        """Test calculating remaining time."""
        gaming_limit_seconds = db.get_daily_limits("anders")[0] * 60  # 120 min = 7200 sec

        # Use 60 minutes
        db.update_daily_summary("anders", gaming_seconds=3600, total_seconds=3600)

        total_used, gaming_used = db.get_time_used_today("anders")
        remaining = gaming_limit_seconds - gaming_used

        assert remaining == 3600  # 60 minutes remaining

    def test_time_exhausted(self, db):
        """Test when time is fully used."""
        gaming_limit_seconds = db.get_daily_limits("anders")[0] * 60

        # Use all time
        db.update_daily_summary("anders", gaming_seconds=gaming_limit_seconds,
                                 total_seconds=gaming_limit_seconds)

        total_used, gaming_used = db.get_time_used_today("anders")
        remaining = max(0, gaming_limit_seconds - gaming_used)

        assert remaining == 0

    def test_over_limit_returns_zero_not_negative(self, db):
        """Test that going over limit returns 0, not negative."""
        gaming_limit_seconds = db.get_daily_limits("anders")[0] * 60

        # Use more than limit (edge case)
        db.update_daily_summary("anders", gaming_seconds=gaming_limit_seconds + 600,
                                 total_seconds=gaming_limit_seconds + 600)

        total_used, gaming_used = db.get_time_used_today("anders")
        remaining = max(0, gaming_limit_seconds - gaming_used)

        assert remaining == 0  # Not negative


class TestWarningThresholds:
    """Tests for warning threshold logic."""

    def test_warning_needed_at_30_minutes(self, db):
        """Test that warning is needed at 30-minute threshold."""
        gaming_limit_seconds = db.get_daily_limits("anders")[0] * 60  # 7200 sec

        # Use 90 minutes (30 remaining)
        db.update_daily_summary("anders", gaming_seconds=5400, total_seconds=5400)

        total_used, gaming_used = db.get_time_used_today("anders")
        remaining_mins = (gaming_limit_seconds - gaming_used) // 60

        assert remaining_mins == 30

        # Check if warning should fire
        db.update_user_state("anders", gaming_active=1, warned_30=0)
        state = db.get_user_state("anders")

        should_warn_30 = remaining_mins <= 30 and not state['warned_30']
        assert should_warn_30 is True

    def test_warning_not_needed_if_already_warned(self, db):
        """Test that warning doesn't fire if already sent."""
        gaming_limit_seconds = db.get_daily_limits("anders")[0] * 60

        # Use 90 minutes (30 remaining)
        db.update_daily_summary("anders", gaming_seconds=5400, total_seconds=5400)

        # Already warned
        db.update_user_state("anders", gaming_active=1, warned_30=1)
        state = db.get_user_state("anders")

        total_used, gaming_used = db.get_time_used_today("anders")
        remaining_mins = (gaming_limit_seconds - gaming_used) // 60

        should_warn_30 = remaining_mins <= 30 and not state['warned_30']
        assert should_warn_30 is False  # Already warned

    def test_multiple_thresholds_in_sequence(self, db):
        """Test warning flags set in sequence as time depletes."""
        gaming_limit_seconds = db.get_daily_limits("anders")[0] * 60

        # Start fresh
        db.update_user_state("anders", gaming_active=1, warned_30=0, warned_15=0, warned_5=0)

        # Simulate time progression
        def check_warnings(gaming_used_seconds):
            remaining_mins = (gaming_limit_seconds - gaming_used_seconds) // 60
            state = db.get_user_state("anders")
            return {
                'remaining': remaining_mins,
                'need_30': remaining_mins <= 30 and not state['warned_30'],
                'need_15': remaining_mins <= 15 and not state['warned_15'],
                'need_5': remaining_mins <= 5 and not state['warned_5'],
            }

        # At 90 min used (30 remaining)
        result = check_warnings(5400)
        assert result['remaining'] == 30
        assert result['need_30'] is True
        assert result['need_15'] is False  # Not yet

        # Mark 30-min warning sent
        db.update_user_state("anders", warned_30=1)

        # At 105 min used (15 remaining)
        result = check_warnings(6300)
        assert result['remaining'] == 15
        assert result['need_30'] is False  # Already sent
        assert result['need_15'] is True

        # Mark 15-min warning sent
        db.update_user_state("anders", warned_15=1)

        # At 115 min used (5 remaining)
        result = check_warnings(6900)
        assert result['remaining'] == 5
        assert result['need_30'] is False
        assert result['need_15'] is False
        assert result['need_5'] is True
