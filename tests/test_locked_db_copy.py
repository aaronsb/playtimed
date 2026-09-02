"""Tests for the browser-database copy helper.

The daemon runs as root and copies files the monitored user owns. Both
properties pinned here are security properties, not conveniences.
"""

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from playtimed.browser.chrome import _copy_locked_db as chrome_copy
from playtimed.browser.firefox import _copy_locked_db as firefox_copy


@pytest.fixture(params=[chrome_copy, firefox_copy], ids=['chrome', 'firefox'])
def copy_locked_db(request):
    """Both workers carry the same helper; both must hold the same properties."""
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

    def test_destination_exists_before_it_is_written(self, copy_locked_db, source_db,
                                                     monkeypatch, tmp_path):
        """The path must be created by mkstemp, not merely predicted.

        Regression: `tempfile.mktemp` returned a name without creating the file,
        so the monitored user could plant a symlink at that path and have root
        write content they control — the source is a file they own — wherever
        the symlink pointed.
        """
        observed = {}
        real_copyfile = __import__('shutil').copyfile

        def spy(src, dst, *args, **kwargs):
            # By the time content is written, the destination must already be a
            # real file created with O_EXCL — not an absent path, not a symlink.
            observed['existed'] = Path(dst).exists()
            observed['is_symlink'] = Path(dst).is_symlink()
            return real_copyfile(src, dst, *args, **kwargs)

        monkeypatch.setattr('shutil.copyfile', spy)
        destination = copy_locked_db(source_db)
        try:
            assert observed['existed'] is True
            assert observed['is_symlink'] is False
        finally:
            destination.unlink()

    def test_each_call_gets_its_own_path(self, copy_locked_db, source_db):
        first = copy_locked_db(source_db)
        second = copy_locked_db(source_db)
        try:
            assert first != second
        finally:
            first.unlink()
            second.unlink()
