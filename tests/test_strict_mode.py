"""Tests for strict mode admission.

Strict mode is documented as "whitelist only". These tests pin down which
pattern states that whitelist actually contains, so a matched-but-unreviewed
process cannot slip through on the strength of having a row in the table.
"""

from playtimed.main import STRICT_ADMITTED_STATES, strict_admits


class TestStrictAdmission:
    """Which pattern states strict mode lets run."""

    def test_active_is_admitted(self):
        assert strict_admits('active')

    def test_ignored_is_admitted(self):
        assert strict_admits('ignored')

    def test_discovered_is_not_admitted(self):
        """A discovered pattern is an unreviewed sighting, not an approval.

        Regression: strict mode previously admitted any process that matched a
        pattern in any state, so an unreviewed discovery ran unchallenged and
        the whitelist promise was only true for processes with no pattern row.
        """
        assert not strict_admits('discovered')

    def test_disallowed_is_not_admitted(self):
        assert not strict_admits('disallowed')

    def test_unknown_state_is_not_admitted(self):
        """Admission is an allowlist, so a state added later defaults to closed."""
        assert not strict_admits('quarantined')
        assert not strict_admits('')

    def test_admitted_states_are_exactly_active_and_ignored(self):
        assert STRICT_ADMITTED_STATES == {'active', 'ignored'}
