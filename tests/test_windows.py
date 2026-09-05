"""Tests for window schedules (ADR-004).

The invariant worth pinning is that windows tile the week. An hour covered by
no window has two readings — deny and allow — and they are the lockdown and
its absence, so the gap must never resolve to "allow" by accident.
"""

import pytest

from playtimed.db import ActivityDB, init_db, migrate_db
from playtimed.windows import (
    DAY_NAMES,
    EVERY_DAY,
    METER_ALL,
    OPEN,
    RESTRICTED,
    WEEKDAYS,
    WEEKENDS,
    ScheduleError,
    Window,
    fill_gaps,
    for_user_default,
    from_legacy,
    parse_days,
    parse_spec,
    resolve_mode,
    to_spec,
    validate,
    window_for,
)

MON, SAT = 0, 5

#: The schedule brick runs, as an operator would type it.
BRICK_SPEC = (
    'mon-fri 0-15 restricted; mon-fri 15-18 open:60; '
    'mon-fri 18-22 open; mon-fri 22-24 restricted; '
    'sat-sun 0-8 restricted; sat-sun 8-18 open:360; '
    'sat-sun 18-23 open; sat-sun 23-24 restricted'
)


class TestTiling:

    def test_default_set_tiles_every_day(self):
        assert validate(for_user_default()) == []

    def test_gap_is_reported_with_the_day_that_has_it(self):
        problems = validate([Window(EVERY_DAY, 0, 10, OPEN),
                             Window(EVERY_DAY, 12, 24, OPEN)])
        assert problems
        assert all('10am-12pm' in p for p in problems)
        assert {p.split(':')[0] for p in problems} == set(DAY_NAMES)

    def test_overlap_is_reported(self):
        problems = validate([Window(EVERY_DAY, 0, 14, OPEN),
                             Window(EVERY_DAY, 12, 24, OPEN)])
        assert any('overlap' in p for p in problems)

    def test_uncovered_tail_is_reported(self):
        problems = validate([Window(EVERY_DAY, 0, 20, OPEN)])
        assert any('8pm-12am' in p for p in problems)

    def test_a_day_nobody_scheduled_is_a_gap(self):
        """Weekday-only windows leave the weekend uncovered, not open."""
        problems = validate([Window(WEEKDAYS, 0, 24, OPEN)])
        assert {p.split(':')[0] for p in problems} == {'sat', 'sun'}


class TestFillGaps:

    def test_gaps_fill_restricted_not_open(self):
        """The whole point: an unruled hour becomes a locked one."""
        filled = fill_gaps([Window(WEEKDAYS, 15, 18, OPEN, 60)])

        assert validate(filled) == []
        assert window_for(filled, MON, 3).mode == RESTRICTED
        assert window_for(filled, MON, 23).mode == RESTRICTED
        assert window_for(filled, SAT, 12).mode == RESTRICTED

    def test_filling_preserves_the_declared_window(self):
        filled = fill_gaps([Window(WEEKDAYS, 15, 18, OPEN, 60)])
        declared = window_for(filled, MON, 16)

        assert declared.mode == OPEN
        assert declared.budget_minutes == 60


class TestBrickSchedule:
    """The requirement, read back hour by hour."""

    @pytest.fixture
    def windows(self):
        return parse_spec(BRICK_SPEC)

    @pytest.mark.parametrize(('day', 'hour', 'mode', 'budget'), [
        (MON, 9, RESTRICTED, None),    # school hours
        (MON, 14, RESTRICTED, None),   # last hour before it opens
        (MON, 15, OPEN, 60),           # 3pm, metered
        (MON, 17, OPEN, 60),           # still inside the metered window
        (MON, 18, OPEN, None),         # 6pm, uncapped
        (MON, 21, OPEN, None),         # last uncapped hour
        (MON, 22, RESTRICTED, None),   # 10pm, back to IXL
        (SAT, 7, RESTRICTED, None),
        (SAT, 8, OPEN, 360),           # weekend day, six hours
        (SAT, 17, OPEN, 360),
        (SAT, 18, OPEN, None),         # 6pm, uncapped until 11
        (SAT, 22, OPEN, None),
        (SAT, 23, RESTRICTED, None),
    ])
    def test_hour_resolves_to_the_intended_rule(self, windows, day, hour, mode, budget):
        w = window_for(windows, day, hour)

        assert w.mode == mode
        assert w.budget_minutes == budget

    def test_every_hour_of_the_week_is_covered(self, windows):
        for day in range(7):
            for hour in range(24):
                assert window_for(windows, day, hour) is not None

    def test_ixl_hours_outnumber_open_hours_on_a_weekday(self, windows):
        """A sanity check on the shape: the restriction is the default state."""
        open_hours = sum(1 for h in range(24)
                         if window_for(windows, MON, h).mode == OPEN)
        assert open_hours == 7


class TestSpec:

    def test_roundtrip(self):
        windows = parse_spec(BRICK_SPEC)
        assert parse_spec(to_spec(windows)) == windows

    def test_unlisted_hours_are_filled_restricted(self):
        windows = parse_spec('mon-fri 15-18 open:60')
        assert validate(windows) == []
        assert window_for(windows, MON, 2).mode == RESTRICTED

    def test_meters_all_survives_the_roundtrip(self):
        windows = parse_spec('all 0-24 open:60/all')
        assert window_for(windows, MON, 5).meters == METER_ALL
        assert '/all' in to_spec(windows)

    @pytest.mark.parametrize('spec', [
        'mon-fri 0-24 restricted:60',   # a lock has nothing to budget
        'mon-fri 0-24 sideways',        # unknown mode
        'funday 0-24 open',             # unknown day
        'mon-fri 0-24 open:soon',       # budget is not minutes
        'mon-fri 24-25 open',           # hours out of range
        'mon-fri 18-15 open',           # end before start
        'mon-fri 0-24 open/sometimes',  # unknown meter
    ])
    def test_nonsense_is_rejected(self, spec):
        with pytest.raises(ScheduleError):
            parse_spec(spec)

    @pytest.mark.parametrize(('token', 'expected'), [
        ('all', EVERY_DAY),
        ('mon-fri', WEEKDAYS),
        ('sat-sun', WEEKENDS),
        ('wed', '0010000'),
        ('mon,wed,fri', '1010100'),
        ('sat-mon', '1000011'),  # wrapping the end of the week
    ])
    def test_day_tokens(self, token, expected):
        assert parse_days(token) == expected


class TestLegacyMigration:

    def test_runs_become_windows(self):
        """16:00-21:00 permitted becomes one open window, the rest restricted."""
        schedule = ''.join(
            ''.join('1' if 16 <= h < 21 else '0' for h in range(24))
            for _ in range(7))

        windows = from_legacy(schedule, [120] * 7)

        assert validate(windows) == []
        assert window_for(windows, MON, 17).mode == OPEN
        assert window_for(windows, MON, 17).budget_minutes == 120
        assert window_for(windows, MON, 9).mode == RESTRICTED

    def test_budget_lands_on_the_first_open_window_only(self):
        """A per-day budget was spendable from the first permitted hour."""
        day = ''.join('1' if h in (9, 10, 20, 21) else '0' for h in range(24))
        windows = from_legacy(day * 7, [90] * 7)

        assert window_for(windows, MON, 9).budget_minutes == 90
        assert window_for(windows, MON, 20).budget_minutes is None

    def test_a_fully_blocked_schedule_migrates_to_all_restricted(self):
        windows = from_legacy('0' * 168, [0] * 7)

        assert validate(windows) == []
        assert all(w.mode == RESTRICTED for w in windows)


class TestPersistence:

    @pytest.fixture
    def db(self, tmp_path):
        path = str(tmp_path / 'test.db')
        init_db(path)
        database = ActivityDB(path)
        database.set_user_limits('anders', daily_total=180)
        migrate_db(path)
        return database

    def test_windows_roundtrip_through_the_database(self, db):
        db.set_windows('anders', parse_spec(BRICK_SPEC))
        assert to_spec(db.get_windows('anders')) == to_spec(parse_spec(BRICK_SPEC))

    def test_a_non_tiling_set_is_refused_and_changes_nothing(self, db):
        db.set_windows('anders', parse_spec(BRICK_SPEC))

        with pytest.raises(ScheduleError):
            db.set_windows('anders', [Window(EVERY_DAY, 0, 10, OPEN)])

        assert to_spec(db.get_windows('anders')) == to_spec(parse_spec(BRICK_SPEC))

    def test_migration_gives_an_existing_user_windows(self, db):
        """A 0.5.x database arrives with a grid and no windows."""
        assert db.get_windows('anders')
        assert validate(db.get_windows('anders')) == []

    def test_migration_does_not_overwrite_an_edited_schedule(self, db):
        db.set_windows('anders', parse_spec('all 0-24 open'))
        migrate_db(db.db_path)

        assert to_spec(db.get_windows('anders')) == 'all 0-24 open'

    def test_consumption_counts_only_hours_inside_the_window(self, db):
        from datetime import date

        db.set_windows('anders', parse_spec(BRICK_SPEC))
        today = date.today().isoformat()

        with db_conn(db) as conn:
            for hour, seconds in ((9, 600), (16, 900), (20, 1200)):
                conn.execute(
                    "INSERT INTO hourly_activity "
                    "(date, hour, user, gaming_seconds, total_seconds) "
                    "VALUES (?, ?, 'anders', ?, ?)", (today, hour, seconds, seconds))

        windows = db.get_windows('anders')
        metered = window_for(windows, MON, 16)
        evening = window_for(windows, MON, 20)

        assert db.get_window_consumption('anders', metered, today) == 900
        assert db.get_window_consumption('anders', evening, today) == 1200

    def test_meters_all_reads_the_other_column(self, db):
        from datetime import date

        db.set_windows('anders', parse_spec('all 0-24 open:60/all'))
        today = date.today().isoformat()

        with db_conn(db) as conn:
            conn.execute(
                "INSERT INTO hourly_activity "
                "(date, hour, user, gaming_seconds, total_seconds) "
                "VALUES (?, 12, 'anders', 60, 3600)", (today,))

        window = db.get_windows('anders')[0]
        assert db.get_window_consumption('anders', window, today) == 3600


def db_conn(database):
    from playtimed.db import get_connection
    return get_connection(database.db_path)


class TestResolveMode:
    """What the daemon runs as, given the windows in force (ADR-004)."""

    RESTRICTED_NOW = Window(EVERY_DAY, 0, 24, RESTRICTED)
    OPEN_NOW = Window(EVERY_DAY, 0, 24, OPEN)

    def test_restricted_window_means_strict(self):
        assert resolve_mode([self.RESTRICTED_NOW], 'normal') == 'strict'

    def test_open_window_means_normal(self):
        assert resolve_mode([self.OPEN_NOW], 'strict') == 'normal'

    def test_the_schedule_outranks_the_stored_mode(self):
        """A mode left behind by a manual `playtimed mode strict` does not stick."""
        assert resolve_mode([self.OPEN_NOW], 'strict') == 'normal'
        assert resolve_mode([self.RESTRICTED_NOW], 'normal') == 'strict'

    def test_passthrough_outranks_the_schedule(self):
        """The manual override has to survive, or enforcement cannot be suspended."""
        assert resolve_mode([self.RESTRICTED_NOW], 'passthrough') == 'passthrough'
        assert resolve_mode([self.OPEN_NOW], 'passthrough') == 'passthrough'

    def test_most_restrictive_user_wins(self):
        """Chrome policy is machine-wide, so one open user cannot unlock it."""
        assert resolve_mode([self.OPEN_NOW, self.RESTRICTED_NOW], 'normal') == 'strict'

    def test_unscheduled_users_do_not_vote(self):
        assert resolve_mode([None, self.OPEN_NOW], 'normal') == 'normal'

    def test_nobody_scheduled_leaves_the_configured_mode_alone(self):
        assert resolve_mode([], 'strict') == 'strict'
        assert resolve_mode([None, None], 'strict') == 'strict'

    def test_brick_schedule_drives_the_mode_across_a_weekday(self):
        windows = parse_spec(BRICK_SPEC)
        modes = [resolve_mode([window_for(windows, MON, h)], 'normal')
                 for h in range(24)]

        assert modes[:15] == ['strict'] * 15      # midnight to 3pm
        assert modes[15:22] == ['normal'] * 7     # 3pm to 10pm
        assert modes[22:] == ['strict'] * 2       # 10pm to midnight
