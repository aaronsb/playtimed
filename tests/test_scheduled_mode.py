"""The daemon's mode follows the clock, and nothing else quietly resets it.

`_reload_config` runs every tenth poll. It used to assign the stored mode
directly, which under ADR-004 means the daemon adopted `daemon_config['mode']`
for the rest of that reload — announcing the change and writing a browser
policy for it — before the schedule moved it back on the same poll. On a host
whose stored mode is `strict` and whose current window is open, that is a
notification and a policy rewrite every five minutes, both wrong.
"""

import pytest

from playtimed.main import ClaudeDaemon
from playtimed.windows import parse_spec


class FakeRouter:
    def __init__(self):
        self.mode_changes = []

    def mode_change(self, mode):
        self.mode_changes.append(mode)


class FakeDB:
    """Only the reads `_reload_config` and `_apply_scheduled_mode` perform."""

    def __init__(self, stored_mode, windows):
        self.stored_mode = stored_mode
        self.windows = windows

    def get_daemon_config(self):
        return {'mode': self.stored_mode, 'strict_grace_seconds': 30}

    def get_discovery_config(self):
        return {'cpu_threshold': 25}

    def get_all_monitored_users(self):
        return ['anders']

    def get_windows(self, user):
        return self.windows


@pytest.fixture
def daemon():
    """A daemon with its collaborators replaced, never constructed for real."""
    d = ClaudeDaemon.__new__(ClaudeDaemon)
    d.router = FakeRouter()
    d.users = ['anders']
    d.syncs = 0
    d._sync_browser_policy = lambda: setattr(d, 'syncs', d.syncs + 1)
    return d


def configure(daemon, stored_mode, spec):
    daemon.db = FakeDB(stored_mode, parse_spec(spec))
    daemon.daemon_config = daemon.db.get_daemon_config()
    daemon.discovery_config = daemon.db.get_discovery_config()


ALWAYS_OPEN = 'all 0-24 open'
ALWAYS_RESTRICTED = 'all 0-24 restricted'


class TestScheduledMode:

    def test_open_window_overrides_a_stored_strict_mode(self, daemon):
        configure(daemon, 'strict', ALWAYS_OPEN)
        daemon.mode = 'strict'

        daemon._apply_scheduled_mode()

        assert daemon.mode == 'normal'
        assert daemon.router.mode_changes == ['normal']
        assert daemon.syncs == 1

    def test_no_transition_means_no_notification_and_no_write(self, daemon):
        configure(daemon, 'strict', ALWAYS_RESTRICTED)
        daemon.mode = 'strict'

        daemon._apply_scheduled_mode()

        assert daemon.router.mode_changes == []
        assert daemon.syncs == 0

    def test_repeated_polls_do_not_re_announce(self, daemon):
        configure(daemon, 'strict', ALWAYS_OPEN)
        daemon.mode = 'strict'

        for _ in range(10):
            daemon._apply_scheduled_mode()

        assert daemon.router.mode_changes == ['normal']
        assert daemon.syncs == 1

    def test_passthrough_survives_the_schedule(self, daemon):
        configure(daemon, 'passthrough', ALWAYS_RESTRICTED)
        daemon.mode = 'passthrough'

        daemon._apply_scheduled_mode()

        assert daemon.mode == 'passthrough'
        assert daemon.router.mode_changes == []


class TestReloadDoesNotClobber:
    """The regression this file exists for."""

    def test_reload_leaves_the_scheduled_mode_in_place(self, daemon):
        configure(daemon, 'strict', ALWAYS_OPEN)
        daemon.mode = 'strict'
        daemon._apply_scheduled_mode()
        daemon.router.mode_changes.clear()
        syncs_before = daemon.syncs

        daemon._reload_config()

        assert daemon.mode == 'normal'
        assert daemon.router.mode_changes == []
        # One unconditional sync for pattern edits; no second one for a
        # mode change that did not happen.
        assert daemon.syncs == syncs_before + 1

    def test_ten_reloads_announce_nothing(self, daemon):
        """Five minutes of polling on brick's shape: stored strict, window open."""
        configure(daemon, 'strict', ALWAYS_OPEN)
        daemon.mode = 'strict'
        daemon._apply_scheduled_mode()
        daemon.router.mode_changes.clear()

        for _ in range(10):
            daemon._reload_config()

        assert daemon.mode == 'normal'
        assert daemon.router.mode_changes == []
