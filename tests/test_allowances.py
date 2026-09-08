"""Tests for allowances (ADR-005).

An allowance is a ration of minutes per clock hour that a restricted window
grants to patterns carrying its name. The invariants worth pinning: the spend
is charged once per poll however many processes and tabs are present, the
ration renews with the clock hour and not with a counter, and once spent the
thing is closed and withheld until the hour turns.
"""

import os
import sqlite3
import tempfile
from datetime import datetime

import pytest

from playtimed.browser import policy
from playtimed.db import ActivityDB, init_db, migrate_db
from playtimed.main import ClaudeDaemon
from playtimed.windows import (
    OPEN,
    RESTRICTED,
    WEEKDAYS,
    ScheduleError,
    Window,
    describe,
    parse_spec,
    to_spec,
    validate,
    window_for,
)

MON = 0
TUE_NOON = datetime.fromisoformat('2026-09-08T12:05')  # a Tuesday, inside a weekday restricted window; naive like the daemon's clock
TUE_NEXT_HOUR = datetime.fromisoformat('2026-09-08T13:00')

SPEC = ('mon-fri 0-16 restricted discord=5; mon-fri 16-20 open:120; '
        'mon-fri 20-22 open; mon-fri 22-24 restricted discord=5; '
        'sat-sun 0-13 restricted; sat-sun 13-23 open:360; sat-sun 23-24 restricted')


class TestWindowModel:

    def test_spec_round_trips(self):
        windows = parse_spec(SPEC)
        assert parse_spec(to_spec(windows)) == windows

    def test_restricted_window_grants_by_name(self):
        w = window_for(parse_spec(SPEC), MON, 12)
        assert w.mode == RESTRICTED
        assert w.allowance('discord') == 5
        assert w.allowance('youtube') is None

    def test_allowances_compare_equal_regardless_of_order(self):
        a = Window(WEEKDAYS, 0, 16, RESTRICTED, allowances=(('b', 1), ('a', 2)))
        b = Window(WEEKDAYS, 0, 16, RESTRICTED, allowances={'a': 2, 'b': 1})
        assert a == b and hash(a) == hash(b)

    def test_windows_differing_only_in_allowance_do_not_merge(self):
        windows = parse_spec('mon-fri 0-24 restricted discord=5; sat-sun 0-24 restricted')
        assert len(windows) == 2

    def test_open_window_refuses_an_allowance(self):
        with pytest.raises(ScheduleError, match='nothing to allow'):
            parse_spec('all 0-24 open discord=5')
        assert validate([Window(WEEKDAYS, 0, 24, OPEN, allowances=(('discord', 5),))])

    @pytest.mark.parametrize('bad', [
        'all 0-24 restricted discord=0',
        'all 0-24 restricted discord=61',
        'all 0-24 restricted discord=five',
        'all 0-24 restricted discord',
        'all 0-24 restricted discord=5 discord=3',
        'all 0-24 restricted dis.cord=5',
    ])
    def test_malformed_allowance_is_refused(self, bad):
        with pytest.raises(ScheduleError):
            parse_spec(bad)

    def test_name_is_lowercased(self):
        w = parse_spec('all 0-24 restricted Discord=5')[0]
        assert w.allowances == (('discord', 5),)

    def test_describe_names_the_ration(self):
        lines = describe(parse_spec(SPEC), MON)
        assert lines[0] == '12am-4pm     restricted, discord 5 min/hr'


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 't.db')
        init_db(path)
        migrate_db(path)
        db = ActivityDB(path)
        db.set_user_limits('anders')
        yield db


class TestDatabase:

    def test_windows_round_trip_allowances(self, db):
        db.set_windows('anders', parse_spec(SPEC))
        assert to_spec(db.get_windows('anders')) == to_spec(parse_spec(SPEC))

    def test_spend_is_keyed_on_the_clock_hour(self, db):
        db.add_allowance_use('anders', 'discord', 30, TUE_NOON)
        db.add_allowance_use('anders', 'discord', 30, TUE_NOON)
        assert db.get_allowance_use('anders', 'discord', TUE_NOON) == 60
        assert db.get_allowance_use('anders', 'discord', TUE_NEXT_HOUR) == 0
        assert db.get_allowance_use('someone', 'discord', TUE_NOON) == 0

    def test_pattern_carries_an_allowance(self, db):
        pid = db.add_pattern('Discord', 'Discord', 'productive')
        assert db.get_allowance_patterns() == []
        db.set_pattern_allowance(pid, 'discord')
        assert [p['id'] for p in db.get_allowance_patterns()] == [pid]
        db.set_pattern_allowance(pid, None)
        assert db.get_allowance_patterns() == []

    def test_a_disallowed_pattern_does_not_draw(self, db):
        pid = db.add_pattern('Discord', 'Discord', 'productive')
        db.set_pattern_allowance(pid, 'discord')
        db.set_pattern_state(pid, 'disallowed')
        assert db.get_allowance_patterns() == []

    def test_charge_returns_the_running_total(self, db):
        assert db.add_allowance_use('anders', 'discord', 30, TUE_NOON) == 30
        assert db.add_allowance_use('anders', 'discord', 30, TUE_NOON) == 60

    def test_migration_adds_columns_to_an_older_database(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 't.db')
            init_db(path)
            migrate_db(path)
            conn = sqlite3.connect(path)
            conn.execute("ALTER TABLE schedule_windows DROP COLUMN allowances")
            conn.execute("ALTER TABLE process_patterns DROP COLUMN allowance")
            conn.execute("DELETE FROM message_templates WHERE intention LIKE 'allowance_%'")
            conn.commit()
            conn.close()

            migrate_db(path)

            db = ActivityDB(path)
            db.set_user_limits('anders')
            db.set_windows('anders', parse_spec(SPEC))
            assert window_for(db.get_windows('anders'), MON, 12).allowance('discord') == 5
            assert db.get_templates('allowance_expired')
            assert db.get_templates('allowance_blocked')

    def test_migration_leaves_existing_templates_alone(self, db):
        rows = db.get_templates('discovery')
        db.update_template(rows[0]['id'], title='Edited by hand')
        migrate_db(db.db_path)
        assert db.get_templates('discovery')[0]['title'] == 'Edited by hand'


def domain(pattern, state='active', category='productive', owner='anders',
           allowance='discord'):
    return {'pattern': pattern, 'monitor_state': state, 'category': category,
            'owner': owner, 'allowance': allowance, 'pattern_type': 'browser_domain'}


class TestWithheldDomains:

    def make(self, db, spec=SPEC, owner='anders'):
        db.set_windows('anders', parse_spec(spec))
        db.add_browser_pattern('discord.com', 'discord.com', 'productive', 'chrome', owner)
        db.set_pattern_allowance(db.get_allowance_patterns()[0]['id']
                                 if db.get_allowance_patterns() else
                                 db.get_pattern_by_domain_and_owner('discord.com', 'anders')['id'],
                                 'discord')

    def test_unspent_allowance_admits_the_domain(self, db):
        self.make(db)
        assert policy.withheld_domains(db, TUE_NOON) == frozenset()

    def test_spent_allowance_withholds_it(self, db):
        self.make(db)
        db.add_allowance_use('anders', 'discord', 300, TUE_NOON)
        assert policy.withheld_domains(db, TUE_NOON) == {'discord.com'}

    def test_ration_renews_with_the_hour(self, db):
        self.make(db)
        db.add_allowance_use('anders', 'discord', 300, TUE_NOON)
        assert policy.withheld_domains(db, TUE_NEXT_HOUR) == frozenset()

    def test_restricted_window_without_the_grant_withholds(self, db):
        self.make(db, spec='all 0-24 restricted')
        assert policy.withheld_domains(db, TUE_NOON) == {'discord.com'}

    def test_open_window_does_not_withhold(self, db):
        self.make(db, spec='all 0-24 open')
        assert policy.withheld_domains(db, TUE_NOON) == frozenset()

    def test_ownerless_domain_follows_the_most_restrictive_user(self, db):
        self.make(db, owner=None)
        db.set_user_limits('sibling')
        db.set_windows('sibling', parse_spec('all 0-24 restricted'))
        assert policy.withheld_domains(db, TUE_NOON) == {'discord.com'}

    def test_withheld_domain_is_blocked_whatever_its_state(self):
        permitted, blocked = policy.partition_domains(
            [domain('discord.com'), domain('ixl.com', category='educational', allowance=None)],
            withheld=frozenset({'discord.com'}))
        assert permitted == ['ixl.com']
        assert blocked == ['discord.com']

    def test_strict_plan_moves_it_from_allowlist_to_blocklist(self):
        target = policy.PolicyTarget('chrome', '/tmp/x.json', policy.chrome_policy)
        [plan] = policy.plan_policies('strict', [domain('discord.com')], [target],
                                      withheld=frozenset({'discord.com'}))
        assert plan.content == {'URLBlocklist': ['*'], 'URLAllowlist': []}


class FakeDB:
    """Only what `_meter_allowances` and `_apply_allowance_policy` touch."""

    def __init__(self):
        self.spend = {}
        self.events = []

    def add_allowance_use(self, user, allowance, seconds, now=None):
        key = (user, allowance, now.date().isoformat(), now.hour)
        self.spend[key] = self.spend.get(key, 0) + seconds
        return self.spend[key]

    def get_allowance_use(self, user, allowance, now=None):
        return self.spend.get((user, allowance, now.date().isoformat(), now.hour), 0)

    def log_event(self, user, event_type, **kw):
        self.events.append((event_type, kw.get('app')))


class FakeRouter:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **kw: self.calls.append((name, *a))


def proc(pid, name='Discord'):
    from playtimed.main import ProcessMatch
    return ProcessMatch(pid=pid, name=name, category='allowance', cmdline='', cpu_percent=1.0)


@pytest.fixture
def daemon():
    d = ClaudeDaemon.__new__(ClaudeDaemon)
    d.db = FakeDB()
    d.router = FakeRouter()
    d.mode = 'strict'
    d.allowance_pending = {}
    d.withheld_domains = frozenset()
    d.killed = []
    d._kill_process = lambda p, user, notify=True, reason='KILLED': d.killed.append(p.pid)
    return d


WINDOW = window_for(parse_spec(SPEC), MON, 12)
POLL, GRACE = 30, 30


def sighting(*pids, label='Discord'):
    return {'discord': {'label': label, 'procs': [proc(pid) for pid in pids]}}


def at(minutes, seconds=0):
    return TUE_NOON.replace(minute=minutes, second=seconds)


class TestMetering:

    def test_one_poll_is_charged_once_however_many_processes(self, daemon):
        daemon._meter_allowances('anders', WINDOW, sighting(1, 2, 3, 4, 5, 6),
                                 POLL, GRACE, now=at(0))
        assert daemon.db.get_allowance_use('anders', 'discord', at(0)) == POLL
        assert daemon.killed == []

    def test_a_tab_alone_is_charged_too(self, daemon):
        daemon._meter_allowances('anders', WINDOW, sighting(), POLL, GRACE, now=at(0))
        assert daemon.db.get_allowance_use('anders', 'discord', at(0)) == POLL

    def test_unseen_allowance_is_not_charged(self, daemon):
        daemon._meter_allowances('anders', WINDOW, {}, POLL, GRACE, now=at(0))
        assert daemon.db.spend == {}


class TestEnforcement:

    def spend_it_all(self, daemon):
        daemon.db.add_allowance_use('anders', 'discord', 5 * 60 - POLL, at(0))

    def test_reaching_the_ration_warns_and_waits(self, daemon):
        self.spend_it_all(daemon)
        daemon._meter_allowances('anders', WINDOW, sighting(1), POLL, GRACE, now=at(5))
        assert daemon.killed == []
        assert daemon.router.calls == [('allowance_expired', 'anders', 'Discord', 5, GRACE)]

    def test_grace_expiry_closes_every_process(self, daemon):
        self.spend_it_all(daemon)
        daemon._meter_allowances('anders', WINDOW, sighting(1, 2), POLL, GRACE, now=at(5))
        daemon._meter_allowances('anders', WINDOW, sighting(1, 2), POLL, GRACE, now=at(5, 15))
        assert daemon.killed == []
        daemon._meter_allowances('anders', WINDOW, sighting(1, 2), POLL, GRACE, now=at(5, 30))
        assert daemon.killed == [1, 2]
        assert ('blocked_allowance', 'Discord') in daemon.db.events
        # The warning already said it would close; no second notification.
        assert [c[0] for c in daemon.router.calls] == ['allowance_expired']

    def test_relaunch_in_the_same_hour_is_closed_on_sight(self, daemon):
        self.spend_it_all(daemon)
        daemon._meter_allowances('anders', WINDOW, sighting(1), POLL, GRACE, now=at(5))
        daemon._meter_allowances('anders', WINDOW, sighting(1), POLL, GRACE, now=at(6))
        daemon.killed.clear()
        daemon._meter_allowances('anders', WINDOW, sighting(7), POLL, GRACE, now=at(20))
        assert daemon.killed == [7]
        assert daemon.router.calls[-1] == ('allowance_blocked', 'anders', 'Discord', 5)

    def test_relaunch_after_a_voluntary_close_is_told_why(self, daemon):
        self.spend_it_all(daemon)
        daemon._meter_allowances('anders', WINDOW, sighting(1), POLL, GRACE, now=at(5))
        # Closed by hand before the grace kill; relaunched later in the hour.
        daemon._meter_allowances('anders', WINDOW, sighting(9), POLL, GRACE, now=at(20))
        assert daemon.killed == [9]
        assert daemon.router.calls[-1] == ('allowance_blocked', 'anders', 'Discord', 5)

    def test_the_hour_turning_renews_the_ration(self, daemon):
        self.spend_it_all(daemon)
        daemon._meter_allowances('anders', WINDOW, sighting(1), POLL, GRACE, now=at(5))
        daemon._meter_allowances('anders', WINDOW, sighting(1), POLL, GRACE, now=at(6))
        daemon.killed.clear()
        daemon._meter_allowances('anders', WINDOW, sighting(8), POLL, GRACE, now=TUE_NEXT_HOUR)
        assert daemon.killed == []
        assert daemon.allowance_pending == {}

    def test_ungranted_allowance_is_shut_out_immediately(self, daemon):
        plain = Window(WEEKDAYS, 0, 16, RESTRICTED)
        daemon._meter_allowances('anders', plain, sighting(1), POLL, GRACE, now=at(0))
        assert daemon.killed == [1]
        assert daemon.router.calls == [('blocked_launch', 'anders', 'Discord')]
        assert daemon.db.spend == {}


class TestPolicyFollowsTheSpend:

    def test_policy_is_rewritten_only_when_the_withheld_set_changes(self, daemon, monkeypatch):
        answers = iter([frozenset(), frozenset({'discord.com'}), frozenset({'discord.com'})])
        monkeypatch.setattr(policy, 'withheld_domains', lambda db: next(answers))
        syncs = []
        daemon._sync_browser_policy = lambda: (
            syncs.append(1), setattr(daemon, 'withheld_domains', frozenset({'discord.com'})))

        daemon._apply_allowance_policy()
        assert syncs == []
        daemon._apply_allowance_policy()
        assert syncs == [1]
        daemon._apply_allowance_policy()
        assert syncs == [1]

    def test_nothing_is_withheld_outside_strict_mode(self, daemon, monkeypatch):
        monkeypatch.setattr(policy, 'withheld_domains',
                            lambda db: (_ for _ in ()).throw(AssertionError('consulted')))
        daemon.mode = 'normal'
        assert daemon._withheld_domains() == frozenset()
