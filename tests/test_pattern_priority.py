"""Ordering of gaming and launcher patterns in the match list.

The bug this guards against: a global catch-all outranking a user-scoped
pattern, so Wine helpers already classified as launchers were tracked as
games anyway.
"""

from playtimed.main import rank_patterns


def _p(name, category, owner=None, pattern=None):
    return {'name': name, 'category': category, 'owner': owner,
            'pattern': pattern or name}


class TestRankPatterns:

    def test_user_scoped_launcher_outranks_global_gaming_catchall(self):
        """The regression: user-scoped launcher must beat the global catch-all."""
        catchall = _p('Proton Game', 'gaming', owner=None, pattern=r'\.exe$')
        helper = _p('explorer', 'launcher', owner='anders', pattern=r'explorer\.exe')

        ranked = rank_patterns([catchall], [helper])

        assert ranked[0] is helper

    def test_gaming_beats_launcher_within_same_scope(self):
        """Preserves 844373a: a game misfiled as a launcher still counts."""
        game = _p('PEAK', 'gaming', owner='anders')
        launcher = _p('PEAK Launcher', 'launcher', owner='anders')

        ranked = rank_patterns([game], [launcher])

        assert ranked[0] is game

    def test_scope_dominates_category(self):
        """Scope is the primary key -- a scoped launcher beats a global game."""
        global_game = _p('Global Game', 'gaming', owner=None)
        scoped_launcher = _p('Scoped Launcher', 'launcher', owner='anders')

        ranked = rank_patterns([global_game], [scoped_launcher])

        assert [p['name'] for p in ranked] == ['Scoped Launcher', 'Global Game']

    def test_full_ordering_across_both_axes(self):
        scoped_game = _p('scoped game', 'gaming', owner='anders')
        scoped_launcher = _p('scoped launcher', 'launcher', owner='anders')
        global_game = _p('global game', 'gaming', owner=None)
        global_launcher = _p('global launcher', 'launcher', owner=None)

        ranked = rank_patterns([global_game, scoped_game],
                               [global_launcher, scoped_launcher])

        assert [p['name'] for p in ranked] == [
            'scoped game', 'scoped launcher', 'global game', 'global launcher',
        ]

    def test_sort_is_stable_within_a_tier(self):
        """Database ordering must survive within an equal-priority tier."""
        first = _p('aaa', 'gaming', owner='anders')
        second = _p('bbb', 'gaming', owner='anders')
        third = _p('ccc', 'gaming', owner='anders')

        ranked = rank_patterns([first, second, third], [])

        assert [p['name'] for p in ranked] == ['aaa', 'bbb', 'ccc']

    def test_empty_launcher_list(self):
        game = _p('PEAK', 'gaming', owner='anders')
        assert rank_patterns([game], []) == [game]

    def test_empty_inputs(self):
        assert rank_patterns([], []) == []

    def test_falsy_owner_treated_as_global(self):
        """An empty-string owner is not user scoping."""
        empty_owner = _p('empty', 'gaming', owner='')
        scoped = _p('scoped', 'launcher', owner='anders')

        ranked = rank_patterns([empty_owner], [scoped])

        assert ranked[0] is scoped


class TestPatternTypeIsolation:
    """Browser domain rows must not be matched against process command lines.

    A browser_domain pattern holds a hostname, not a process regex. Because
    matching is an unanchored `re.search` over both process name and cmdline,
    a domain row reaching the process scan means an unrelated process whose
    cmdline happens to contain that string is treated as that pattern — and a
    disallowed domain would kill it. `discover disallow <domain>` producing a
    dead process is the outcome ADR-003 explicitly rejects.
    """

    def test_get_patterns_excludes_browser_domains(self, tmp_path):
        from playtimed.db import ActivityDB

        db = ActivityDB(str(tmp_path / 'test.db'))
        db.set_user_limits('anders', gaming_limit=120, daily_total=180)
        db.add_browser_pattern('discord.com', 'Discord', 'gaming', 'chrome', 'anders')

        names = [p['name'] for p in db.get_patterns(
            owner='anders', enabled_only=True, include_all_states=True)]
        assert 'Discord' not in names

    def test_category_filtered_queries_exclude_browser_domains(self, tmp_path):
        """The gaming/launcher queries feeding _find_gaming_processes leak too."""
        from playtimed.db import ActivityDB

        db = ActivityDB(str(tmp_path / 'test.db'))
        db.set_user_limits('anders', gaming_limit=120, daily_total=180)
        db.add_browser_pattern('youtube.com', 'YouTube', 'gaming', 'chrome', 'anders')

        names = [p['name'] for p in db.get_patterns(category='gaming', owner='anders')]
        assert 'YouTube' not in names

    def test_browser_domains_are_still_reachable_by_their_own_query(self, tmp_path):
        from playtimed.db import ActivityDB

        db = ActivityDB(str(tmp_path / 'test.db'))
        db.add_browser_pattern('ixl.com', 'IXL', 'educational', 'chrome', 'anders')

        names = [p['name'] for p in db.get_browser_patterns(include_all_states=True)]
        assert 'IXL' in names

    def test_disabled_browser_domains_are_excluded(self, tmp_path):
        from playtimed.db import ActivityDB

        db = ActivityDB(str(tmp_path / 'test.db'))
        pid = db.add_browser_pattern('ixl.com', 'IXL', 'educational', 'chrome', 'anders')
        db.update_pattern(pid, enabled=0)

        names = [p['name'] for p in db.get_browser_patterns(include_all_states=True)]
        assert 'IXL' not in names
