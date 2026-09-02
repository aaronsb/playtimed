"""Tests for browser managed-policy generation (ADR-003).

The generated file is what actually enforces browser rules, so these tests pin
down the mapping from pattern state to policy content, and the safety property
that playtimed only ever touches the one file it owns.
"""

import json
import os

import pytest

from playtimed.browser import policy


def domain(pattern, state, ptype='browser_domain'):
    return {'pattern': pattern, 'monitor_state': state, 'pattern_type': ptype}


PATTERNS = [
    domain('ixl.com', 'active'),
    domain('classroom.google.com', 'ignored'),
    domain('discord.com', 'disallowed'),
    domain('coolmathgames.com', 'disallowed'),
    domain('search.sunbiz.org', 'discovered'),
]


class TestPartitionDomains:
    """Which states land in which bucket."""

    def test_active_and_ignored_are_permitted(self):
        permitted, _ = policy.partition_domains(PATTERNS)
        assert permitted == ['classroom.google.com', 'ixl.com']

    def test_disallowed_is_blocked(self):
        _, blocked = policy.partition_domains(PATTERNS)
        assert blocked == ['coolmathgames.com', 'discord.com']

    def test_discovered_is_in_neither_bucket(self):
        """An unreviewed domain is not an approval, matching strict_admits()."""
        permitted, blocked = policy.partition_domains(PATTERNS)
        assert 'search.sunbiz.org' not in permitted
        assert 'search.sunbiz.org' not in blocked

    def test_blank_patterns_are_skipped(self):
        permitted, blocked = policy.partition_domains(
            [domain('', 'active'), domain('   ', 'disallowed')])
        assert permitted == []
        assert blocked == []

    def test_duplicates_collapse(self):
        permitted, _ = policy.partition_domains(
            [domain('ixl.com', 'active'), domain('ixl.com', 'ignored')])
        assert permitted == ['ixl.com']


class TestChromePolicy:
    """Chrome-family policy bodies."""

    def test_strict_is_an_allowlist(self):
        body = policy.chrome_policy('strict', ['ixl.com'], ['discord.com'])
        assert body['URLBlocklist'] == ['*']
        assert body['URLAllowlist'] == ['ixl.com']

    def test_strict_with_nothing_permitted_blocks_everything(self):
        body = policy.chrome_policy('strict', [], [])
        assert body['URLBlocklist'] == ['*']
        assert body['URLAllowlist'] == []

    def test_normal_is_a_blocklist(self):
        body = policy.chrome_policy('normal', ['ixl.com'], ['discord.com'])
        assert body == {'URLBlocklist': ['discord.com']}

    def test_normal_with_nothing_blocked_writes_no_policy(self):
        assert policy.chrome_policy('normal', ['ixl.com'], []) is None

    def test_passthrough_writes_no_policy(self):
        """Passthrough is explicitly no enforcement, browser included."""
        assert policy.chrome_policy('passthrough', ['ixl.com'], ['discord.com']) is None


class TestFirefoxPolicy:
    """Firefox uses WebsiteFilter with match patterns, not bare domains."""

    def test_strict_blocks_all_and_excepts_permitted(self):
        body = policy.firefox_policy('strict', ['ixl.com'], [])
        f = body['policies']['WebsiteFilter']
        assert f['Block'] == ['<all_urls>']
        assert f['Exceptions'] == ['*://*.ixl.com/*']

    def test_normal_blocks_disallowed_as_match_patterns(self):
        body = policy.firefox_policy('normal', [], ['discord.com'])
        assert body['policies']['WebsiteFilter']['Block'] == ['*://*.discord.com/*']

    def test_passthrough_writes_no_policy(self):
        assert policy.firefox_policy('passthrough', ['ixl.com'], ['discord.com']) is None


def chrome_target(path):
    return policy.PolicyTarget('chrome', str(path), policy.chrome_policy)


class TestDetectTargets:
    """A browser is targeted only if it is actually present."""

    def test_absent_browser_is_not_targeted(self, tmp_path):
        targets = policy.detect_targets(
            chrome_family={'chrome': (str(tmp_path / 'nope'), ('no-such-browser-xyz',))},
            firefox_family={})
        assert targets == []

    def test_existing_policy_dir_marks_a_browser_present(self, tmp_path):
        managed = tmp_path / 'managed'
        managed.mkdir()
        targets = policy.detect_targets(
            chrome_family={'chrome': (str(managed), ('no-such-browser-xyz',))},
            firefox_family={})
        assert len(targets) == 1
        assert targets[0].path == str(managed / policy.POLICY_FILENAME)

    def test_binary_on_path_marks_a_browser_present(self, tmp_path):
        """The policy dir may not exist yet; the binary still proves the browser."""
        targets = policy.detect_targets(
            chrome_family={'chrome': (str(tmp_path / 'not-made-yet'), ('sh',))},
            firefox_family={})
        assert len(targets) == 1

    def test_firefox_uses_its_own_filename(self, tmp_path):
        d = tmp_path / 'ff'
        d.mkdir()
        targets = policy.detect_targets(
            chrome_family={}, firefox_family={'firefox': (str(d), ('firefox',))})
        assert targets[0].path.endswith('policies.json')


class TestPlanPolicies:
    """Plans render one body per target."""

    def test_plan_file_is_always_the_one_playtimed_owns(self, tmp_path):
        plans = policy.plan_policies(
            'strict', PATTERNS,
            targets=[chrome_target(tmp_path / policy.POLICY_FILENAME)])
        assert os.path.basename(plans[0].path) == 'playtimed.json'

    def test_no_targets_means_no_plans(self):
        assert policy.plan_policies('strict', PATTERNS, targets=[]) == []

    def test_reason_names_the_mode(self, tmp_path):
        plans = policy.plan_policies(
            'strict', PATTERNS,
            targets=[chrome_target(tmp_path / policy.POLICY_FILENAME)])
        assert 'strict mode' in plans[0].reason


class TestApplyPlans:
    """Writing, removing, and leaving other files alone."""

    @pytest.fixture
    def managed(self, tmp_path):
        d = tmp_path / 'etc' / 'chrome' / 'policies' / 'managed'
        d.mkdir(parents=True)
        return d

    def test_write_produces_valid_json(self, managed):
        plans = policy.plan_policies(
            'strict', PATTERNS,
            targets=[chrome_target(managed / policy.POLICY_FILENAME)])
        policy.apply_plans(plans)
        written = json.loads((managed / 'playtimed.json').read_text())
        assert written['URLAllowlist'] == ['classroom.google.com', 'ixl.com']

    def test_write_is_world_readable(self, managed):
        """The browser reads this as an unprivileged process."""
        plans = policy.plan_policies(
            'strict', PATTERNS,
            targets=[chrome_target(managed / policy.POLICY_FILENAME)])
        policy.apply_plans(plans)
        assert os.stat(managed / 'playtimed.json').st_mode & 0o444

    def test_passthrough_removes_a_previously_written_policy(self, managed):
        strict = policy.plan_policies(
            'strict', PATTERNS,
            targets=[chrome_target(managed / policy.POLICY_FILENAME)])
        policy.apply_plans(strict)
        assert (managed / 'playtimed.json').exists()

        relax = policy.plan_policies(
            'passthrough', PATTERNS,
            targets=[chrome_target(managed / policy.POLICY_FILENAME)])
        policy.apply_plans(relax)
        assert not (managed / 'playtimed.json').exists()

    def test_administrator_files_are_untouched(self, managed):
        """playtimed owns one filename and must not disturb the rest."""
        other = managed / 'site-rules.json'
        other.write_text('{"ExtensionInstallBlocklist": ["*"]}')

        for mode in ('strict', 'normal', 'passthrough'):
            policy.apply_plans(policy.plan_policies(
                mode, PATTERNS,
                targets=[chrome_target(managed / policy.POLICY_FILENAME)]))

        assert other.exists()
        assert json.loads(other.read_text()) == {'ExtensionInstallBlocklist': ['*']}

    def test_no_temp_files_are_left_behind(self, managed):
        plans = policy.plan_policies(
            'strict', PATTERNS,
            targets=[chrome_target(managed / policy.POLICY_FILENAME)])
        policy.apply_plans(plans)
        leftovers = [p for p in os.listdir(managed) if p.startswith('.playtimed-policy-')]
        assert leftovers == []

    def test_removing_an_absent_file_is_not_an_error(self, managed):
        plans = policy.plan_policies(
            'passthrough', PATTERNS,
            targets=[chrome_target(managed / policy.POLICY_FILENAME)])
        assert policy.apply_plans(plans) == []
