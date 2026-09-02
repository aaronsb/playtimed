"""Shared test fixtures.

The suite exercises MessageRouter against whatever notification backend the
machine offers. On a developer's KDE desktop that is the real freedesktop
D-Bus server, so a test run leaves a stack of popups waiting to be dismissed
by hand. The fixture below closes them again.
"""

import pytest

from playtimed.notify import FreedesktopBackend


def _live_backend():
    """The real notification server, or None when there is no desktop.

    Returns None under CI, in a container, and anywhere dbus-python is absent,
    which is also every case where the suite raises no popups to begin with.
    """
    try:
        backend = FreedesktopBackend(app_name="playtimed-tests")
    except Exception:
        return None
    return backend if backend.is_available() else None


def _probe(backend):
    """Post a throwaway notification and return the id the server issued.

    Notification ids are handed out in ascending order, so a probe either side
    of the run bounds every id the run could have produced. The probe closes
    itself; the 1ms timeout keeps it from being seen even if closing fails.
    """
    nid = backend.send(
        title="playtimed test suite",
        body="probe",
        timeout=1,
    )
    if nid > 0:
        backend.close(nid)
    return nid


@pytest.fixture(scope="session", autouse=True)
def dismiss_test_notifications():
    """Close every popup raised while the suite ran.

    Bounded by a probe before and after rather than tracked per notification,
    because the router hands its ids to the test that called it and nothing
    collects them. Anything else the desktop raised in the same window is
    closed too — acceptable for a suite that is only run interactively.
    """
    backend = _live_backend()
    if backend is None:
        yield
        return

    first = _probe(backend)

    yield

    last = _probe(backend)
    if first <= 0 or last <= 0:
        return

    for nid in range(first, last + 1):
        backend.close(nid)
