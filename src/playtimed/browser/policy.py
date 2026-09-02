"""
Browser managed-policy generation (ADR-003).

playtimed's browser_domain patterns are the source of truth for which sites a
user may reach. The browser is what enforces it, by reading a root-owned
managed-policy file. This module turns the former into the latter.

Two families, two schemas:

  Chrome family  URLBlocklist / URLAllowlist, plain domain strings
  Firefox        WebsiteFilter Block / Exceptions, match patterns

Both take the same three inputs — daemon mode, disallowed domains, permitted
domains — so the mode table lives here once and each generator only renders it.
"""

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# The file playtimed owns in each policy directory. Anything else in these
# directories belongs to the administrator and is never read or written.
POLICY_FILENAME = 'playtimed.json'

# Where each browser family reads managed policy, and the commands that prove
# it is installed. A browser is targeted if its policy directory already exists
# (its package made it) or its binary is on PATH.
CHROME_FAMILY = {
    'chrome': ('/etc/opt/chrome/policies/managed',
               ('google-chrome', 'google-chrome-stable')),
    'chromium': ('/etc/chromium/policies/managed',
                 ('chromium', 'chromium-browser')),
    'brave': ('/etc/brave/policies/managed',
              ('brave', 'brave-browser')),
    'edge': ('/etc/opt/edge/policies/managed',
             ('microsoft-edge', 'microsoft-edge-stable')),
}

FIREFOX_FAMILY = {
    'firefox': ('/etc/firefox/policies', ('firefox',)),
}

FIREFOX_POLICY_FILENAME = 'policies.json'


class PolicyTarget:
    """One policy file playtimed may write, for one installed browser."""

    def __init__(self, family: str, path: str, renderer):
        self.family = family
        self.path = path
        # Callable(mode, permitted, blocked) -> policy body or None.
        self.renderer = renderer


class PolicyPlan:
    """What a generator intends to write, before it writes it.

    Separating the plan from the write is what lets `playtimed browser-policy`
    show an administrator the effect of a state change without making it.
    """

    def __init__(self, path: str, content: dict | None, reason: str):
        self.path = path
        # None means "no policy applies" — the file should not exist.
        self.content = content
        self.reason = reason

    @property
    def removes(self) -> bool:
        return self.content is None

    def render(self) -> str:
        if self.content is None:
            return ''
        return json.dumps(self.content, indent=2, sort_keys=True) + '\n'


def partition_domains(patterns: list[dict]) -> tuple[list[str], list[str]]:
    """Split browser domain patterns into (permitted, blocked).

    Permitted mirrors strict mode's process admission: 'active' and 'ignored'
    only. A 'discovered' domain is an unreviewed sighting and appears in
    neither list, which under an allowlist means it is unreachable.
    """
    permitted, blocked = [], []
    for p in patterns:
        domain = (p.get('pattern') or '').strip()
        if not domain:
            continue
        state = p.get('monitor_state')
        if state in ('active', 'ignored'):
            permitted.append(domain)
        elif state == 'disallowed':
            blocked.append(domain)
    return sorted(set(permitted)), sorted(set(blocked))


def chrome_policy(mode: str, permitted: list[str], blocked: list[str]) -> dict | None:
    """Render the Chrome-family policy body for a daemon mode."""
    if mode == 'passthrough':
        return None

    if mode == 'strict':
        # Allowlist. Chrome resolves blocklist/allowlist conflicts to the more
        # specific rule, so "*" plus explicit hosts permits exactly those hosts.
        return {
            'URLBlocklist': ['*'],
            'URLAllowlist': permitted,
        }

    if not blocked:
        return None
    return {'URLBlocklist': blocked}


def _firefox_pattern(domain: str) -> str:
    """Render a domain as a Firefox WebsiteFilter match pattern."""
    return f'*://*.{domain}/*'


def firefox_policy(mode: str, permitted: list[str], blocked: list[str]) -> dict | None:
    """Render the Firefox policy body for a daemon mode."""
    if mode == 'passthrough':
        return None

    if mode == 'strict':
        return {
            'policies': {
                'WebsiteFilter': {
                    'Block': ['<all_urls>'],
                    'Exceptions': [_firefox_pattern(d) for d in permitted],
                }
            }
        }

    if not blocked:
        return None
    return {
        'policies': {
            'WebsiteFilter': {
                'Block': [_firefox_pattern(d) for d in blocked],
            }
        }
    }


def detect_targets(chrome_family: dict = None,
                   firefox_family: dict = None) -> list[PolicyTarget]:
    """Find the policy files for browsers actually present on this system."""
    chrome_family = CHROME_FAMILY if chrome_family is None else chrome_family
    firefox_family = FIREFOX_FAMILY if firefox_family is None else firefox_family

    targets = []
    for family, (directory, commands) in chrome_family.items():
        if _browser_installed(directory, commands):
            targets.append(PolicyTarget(
                family, os.path.join(directory, POLICY_FILENAME), chrome_policy))

    for family, (directory, commands) in firefox_family.items():
        if _browser_installed(directory, commands):
            targets.append(PolicyTarget(
                family, os.path.join(directory, FIREFOX_POLICY_FILENAME),
                firefox_policy))

    return targets


def _browser_installed(policy_dir: str, commands: tuple) -> bool:
    """Whether this browser is present: its policy directory or its binary."""
    if Path(policy_dir).is_dir():
        return True
    return any(shutil.which(cmd) for cmd in commands)


def plan_policies(mode: str, patterns: list[dict],
                  targets: list[PolicyTarget] = None) -> list[PolicyPlan]:
    """Build the policy plan for every installed browser."""
    if targets is None:
        targets = detect_targets()

    permitted, blocked = partition_domains(patterns)

    if mode == 'strict':
        reason = f'strict mode: allowlist of {len(permitted)} domain(s)'
    elif blocked:
        reason = f'normal mode: blocklist of {len(blocked)} domain(s)'
    else:
        reason = f'{mode} mode: no rules to enforce'

    return [
        PolicyPlan(t.path, t.renderer(mode, permitted, blocked), reason)
        for t in targets
    ]


def apply_plans(plans: list[PolicyPlan]) -> list[str]:
    """Write (or remove) each planned policy file. Returns a log of actions.

    Writes are atomic: a temporary file in the target directory, then a rename,
    so a browser reading mid-write sees either the old policy or the new one.
    """
    actions = []
    for plan in plans:
        try:
            if plan.removes:
                if os.path.exists(plan.path):
                    os.unlink(plan.path)
                    actions.append(f'removed {plan.path}')
                continue

            directory = os.path.dirname(plan.path)
            os.makedirs(directory, exist_ok=True)

            fd, tmp = tempfile.mkstemp(dir=directory, prefix='.playtimed-policy-')
            try:
                with os.fdopen(fd, 'w') as f:
                    f.write(plan.render())
                os.chmod(tmp, 0o644)
                os.replace(tmp, plan.path)
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

            actions.append(f'wrote {plan.path}')
        except OSError as e:
            # A browser policy that failed to update is a degraded state, not a
            # reason to stop enforcing process limits (ADR-003).
            log.warning('Could not write browser policy %s: %s', plan.path, e)
            actions.append(f'FAILED {plan.path}: {e}')

    return actions


def sync(db, owner: str = None) -> list[str]:
    """Regenerate every browser policy from the database. Returns action log."""
    mode = db.get_daemon_mode()
    patterns = db.get_browser_patterns(owner=owner, include_all_states=True)
    return apply_plans(plan_policies(mode, patterns))
