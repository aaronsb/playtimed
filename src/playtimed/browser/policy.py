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
import re
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

# Firefox has no policy directory to merge — every browser reads one
# policies.json. So playtimed owns exactly this key inside it and leaves the
# administrator's other policies in place.
FIREFOX_OWNED_KEY = ('policies', 'WebsiteFilter')


class PolicyTarget:
    """One policy file playtimed may write, for one installed browser."""

    def __init__(self, family: str, path: str, renderer, merge_key: tuple = None):
        self.family = family
        self.path = path
        # Callable(mode, permitted, blocked) -> policy body or None.
        self.renderer = renderer
        # Set when playtimed owns a key inside a shared file (see PolicyPlan).
        self.merge_key = merge_key


class PolicyPlan:
    """What a generator intends to write, before it writes it.

    Separating the plan from the write is what lets `playtimed browser-policy`
    show an administrator the effect of a state change without making it.

    `merge_key` is set for browsers with no policy directory to merge, where
    playtimed owns one key inside a shared file rather than the whole file.
    """

    def __init__(self, path: str, content: dict | None, reason: str,
                 merge_key: tuple = None):
        self.path = path
        # None means "no policy applies" — playtimed's rules come out.
        self.content = content
        self.reason = reason
        self.merge_key = merge_key

    @property
    def removes(self) -> bool:
        return self.content is None

    def resolve(self) -> dict | None:
        """The file's full intended content, merging with what is on disk.

        Returns None when the file should not exist at all.
        """
        if self.merge_key is None:
            return self.content

        existing = _read_json(self.path) or {}

        # Walk to the parent of the owned key, creating dicts as needed.
        node = existing
        for segment in self.merge_key[:-1]:
            child = node.get(segment)
            if not isinstance(child, dict):
                child = {}
                node[segment] = child
            node = child

        leaf = self.merge_key[-1]
        if self.content is None:
            node.pop(leaf, None)
        else:
            node[leaf] = _dig(self.content, self.merge_key)

        return _prune(existing) or None

    def render(self) -> str:
        resolved = self.resolve()
        if resolved is None:
            return ''
        return json.dumps(resolved, indent=2, sort_keys=True) + '\n'


def _dig(body: dict, key_path: tuple):
    """Pull the owned key's value out of a rendered policy body."""
    node = body
    for segment in key_path:
        node = node[segment]
    return node


def _read_json(path: str) -> dict | None:
    """Read a JSON file, treating unreadable or malformed as absent."""
    try:
        with open(path) as f:
            loaded = json.load(f)
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _prune(node: dict) -> dict:
    """Drop empty dicts left behind by removing an owned key."""
    for key in list(node):
        value = node[key]
        if isinstance(value, dict):
            _prune(value)
            if not value:
                del node[key]
    return node


# A hostname that reached the database via urlparse().netloc can carry a port
# or credentials. Chrome tolerates a port; Firefox silently drops a match
# pattern containing one, which fails open. Strip to the bare host.
_HOST_RE = re.compile(r'^(?:[^@/]*@)?([A-Za-z0-9._-]+?)(?::\d+)?$')


def _clean_domain(raw) -> str:
    """Reduce a stored domain to a bare hostname, or '' if it is not one."""
    value = (raw or '').strip().strip('.').lower()
    if not value:
        return ''
    match = _HOST_RE.match(value)
    return match.group(1) if match else ''


# 'active' carries two meanings inherited from ADR-001. For a process it means
# "tracked, and counted against the limit". For a domain it means the same, but
# playtimed cannot close a tab when that limit expires — so an 'active' gaming
# domain is a budget it has no way to enforce. Under an allowlist it is treated
# as blocked rather than admitted on a promise that cannot be kept.
UNENFORCEABLE_BUDGET_CATEGORIES = frozenset({'gaming'})


def partition_domains(patterns: list[dict]) -> tuple[list[str], list[str]]:
    """Split browser domain patterns into (permitted, blocked).

    Permitted mirrors strict mode's process admission — 'active' and 'ignored'
    — minus the gaming-category rows whose time budget cannot be enforced. A
    'discovered' domain is an unreviewed sighting and appears in neither list,
    which under an allowlist means it is unreachable.
    """
    permitted, blocked = [], []
    for p in patterns:
        domain = _clean_domain(p.get('pattern'))
        if not domain:
            continue
        state = p.get('monitor_state')
        category = (p.get('category') or '').lower()

        if state == 'disallowed':
            blocked.append(domain)
        elif state == 'active' and category in UNENFORCEABLE_BUDGET_CATEGORIES:
            blocked.append(domain)
        elif state in ('active', 'ignored'):
            permitted.append(domain)

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
                firefox_policy, merge_key=FIREFOX_OWNED_KEY))

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
        PolicyPlan(t.path, t.renderer(mode, permitted, blocked), reason,
                   merge_key=t.merge_key)
        for t in targets
    ]


def apply_plans(plans: list[PolicyPlan]) -> list[str]:
    """Write (or remove) each planned policy file. Returns a log of actions.

    Writes are atomic: a temporary file in the target directory, then a rename,
    so a browser reading mid-write sees either the old policy or the new one.
    A plan whose result already matches what is on disk writes nothing, so the
    daemon's periodic reload does not rewrite files every few minutes.
    """
    actions = []
    for plan in plans:
        try:
            desired = plan.render()

            if not desired:
                if os.path.exists(plan.path):
                    os.unlink(plan.path)
                    actions.append(f'removed {plan.path}')
                continue

            if _file_matches(plan.path, desired):
                continue

            directory = os.path.dirname(plan.path)
            os.makedirs(directory, exist_ok=True)

            fd, tmp = tempfile.mkstemp(dir=directory, prefix='.playtimed-policy-')
            try:
                with os.fdopen(fd, 'w') as f:
                    f.write(desired)
                os.chmod(tmp, 0o644)
                os.replace(tmp, plan.path)
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

            actions.append(f'wrote {plan.path}')
        except OSError as e:
            # A browser policy that failed to update is a degraded state, not a
            # reason to stop enforcing process limits (ADR-003). The daemon runs
            # under ProtectSystem=strict, so this is what a missing
            # ReadWritePaths entry for a newly installed browser looks like.
            log.warning('Could not write browser policy %s: %s', plan.path, e)
            actions.append(f'FAILED {plan.path}: {e}')

    return actions


def _file_matches(path: str, desired: str) -> bool:
    """Whether the file already holds exactly what we mean to write."""
    try:
        with open(path) as f:
            return f.read() == desired
    except OSError:
        return False


def sync(db) -> list[str]:
    """Regenerate every browser policy from the database. Returns action log.

    Managed policy is per-browser and machine-wide; there is no per-user
    equivalent. So the policy is built from every owner's domains at once, and
    it applies to everyone who logs into the machine. On a host where an
    administrator also browses, that administrator is subject to the same
    rules — the workaround is a separate machine or a separate browser.
    """
    mode = db.get_daemon_mode()
    patterns = db.get_browser_patterns(include_all_states=True)
    return apply_plans(plan_policies(mode, patterns))
