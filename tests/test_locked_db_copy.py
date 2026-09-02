"""Tests for the browser-database copy helper.

The daemon runs as root and copies files the monitored user owns. Both
properties pinned here are security properties, not conveniences.
"""

import os
import sqlite3
import stat

import pytest

from playtimed.browser.base import copy_locked_db as base_copy
from playtimed.browser.chrome import copy_locked_db as chrome_copy
from playtimed.browser.firefox import copy_locked_db as firefox_copy


def test_both_workers_share_one_definition():
    """Regression: the helper was duplicated verbatim in both workers.

    A security fix applied to one copy and not the other is the failure mode;
    it happened once already while this was being written.
    """
    assert chrome_copy is base_copy
    assert firefox_copy is base_copy


@pytest.fixture(params=[chrome_copy, firefox_copy], ids=['chrome', 'firefox'])
def copy_locked_db(request):
    """Reached through both workers, so neither import can silently diverge."""
    return request.param


@pytest.fixture
def source_db(tmp_path):
    path = tmp_path / 'History'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE urls (url TEXT, title TEXT)')
    conn.execute("INSERT INTO urls VALUES ('https://ixl.com/math', 'IXL Math')")
    conn.commit()
    conn.close()
    # Browsers leave their profile files group- and world-readable.
    os.chmod(path, 0o644)
    return path


class TestCopyLockedDb:

    def test_copy_is_queryable(self, copy_locked_db, source_db):
        destination = copy_locked_db(source_db)
        try:
            row = sqlite3.connect(destination).execute('SELECT url FROM urls').fetchone()
            assert row == ('https://ixl.com/math',)
        finally:
            destination.unlink()

    def test_copy_is_not_readable_by_other_users(self, copy_locked_db, source_db):
        """The source is world-readable; the copy must not inherit that.

        `shutil.copy2` carries the source's mode across and would widen the
        0600 that mkstemp creates, publishing the user's browsing history to
        every local account for the lifetime of the copy.
        """
        destination = copy_locked_db(source_db)
        try:
            mode = os.stat(destination).st_mode
            assert not mode & stat.S_IRGRP
            assert not mode & stat.S_IROTH
        finally:
            destination.unlink()

    def test_destination_is_never_opened_by_name(self, copy_locked_db, source_db,
                                                 monkeypatch):
        """The copy must go through mkstemp's own descriptor.

        Regression: the helper used to close that descriptor and hand the path
        to `shutil.copyfile`, which reopens by name — and a write by name
        follows a symlink and carries no O_EXCL. Naming the destination twice
        reopens the question mkstemp was chosen to close, leaving only /tmp's
        sticky bit between root and a user-directed write.
        """
        import builtins

        opened = []
        real_open = builtins.open

        def spy(file, *args, **kwargs):
            opened.append(str(file))
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, 'open', spy)
        destination = copy_locked_db(source_db)
        try:
            assert str(destination) not in opened
        finally:
            destination.unlink()

    def test_failed_copy_leaves_nothing_behind(self, copy_locked_db, tmp_path,
                                               monkeypatch):
        """A failure has to clean up after itself.

        Regression: mkstemp creates the file before the copy runs, and the
        caller has no name for it until the helper returns — so its `finally`
        cannot remove what a raising copy left behind. The monitored user can
        drive this: a directory at the profile path satisfies the caller's
        `.exists()` guard and makes the copy raise on every poll, orphaning a
        root-owned file each time, without bound.
        """
        import tempfile as tempfile_module

        monkeypatch.setattr(tempfile_module, 'tempdir', str(tmp_path))
        planted = tmp_path / 'History'
        planted.mkdir()

        with pytest.raises(OSError):
            copy_locked_db(planted)

        assert list(tmp_path.glob('playtimed-*.db')) == []

    def test_each_call_gets_its_own_path(self, copy_locked_db, source_db):
        first = copy_locked_db(source_db)
        second = copy_locked_db(source_db)
        try:
            assert first != second
        finally:
            first.unlink()
            second.unlink()
