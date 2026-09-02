# ADR-003: Browser Enforcement via Generated Managed Policy

Status: Accepted
Date: 2026-09-02
Deciders: @aaron, @claude

## Context

ADR-001 established browser domain patterns as a parallel entity type to process patterns, sharing the same `monitor_state` machine: `discovered` → `active` / `ignored` / `disallowed`. It listed `disallow` as a stretch goal.

The stretch goal was never built. `playtimed discover disallow <id>` accepts a browser domain pattern, writes `disallowed` to the row, prints "will be terminated on detection", and nothing enforces it. The kill path in `_scan_all_processes()` operates on PIDs; browser domains are handled in a separate block that only records runtime and emits discovery notifications. On brick, `discord.com` sat in `disallowed` for months while remaining reachable.

The gap is not an oversight in the kill path. playtimed detects domains by reading the active window title over KWin's D-Bus interface. That tells it which tab has focus, in one window, at poll time. It gives no handle on a tab, no navigation event, and no view of background tabs. The three things playtimed could do with a domain — notify, kill the browser, or nothing — are respectively toothless, indiscriminate (one blocked tab costs every open tab, including the permitted ones), and dishonest given what the CLI prints.

Meanwhile Chrome, Chromium, Edge and Firefox all read root-owned managed policy files that enforce URL rules at navigation time, across every tab and window, before the page loads, with no polling. On brick this was verified by hand: a `URLBlocklist`/`URLAllowlist` pair in `/etc/opt/chrome/policies/managed/` restricted Chrome to `ixl.com` and survived restart, with no way for a non-root user to remove it.

Two sources of truth for the same policy is the problem worth avoiding. Hand-editing the JSON while `playtimed patterns list` shows a contradictory `disallowed` state is how the two drift.

## Decision

playtimed generates the browser's managed policy file from its own `browser_domain` patterns. The database stays the single source of truth; the browser does the enforcing.

### Generation rules

| Daemon mode | Generated policy |
|---|---|
| `normal` | `URLBlocklist` from every `disallowed` domain |
| `strict` | `URLBlocklist: ["*"]` plus `URLAllowlist` from every `active` and `ignored` domain |
| `passthrough` | No policy file; any previously generated file is removed |

Strict mode's browser policy mirrors its process policy: an allowlist, with `discovered` outside it. This is the same admission rule ADR-003's sibling fix applied to `strict_admits()` — a state nobody has ruled on does not grant access.

### Ownership boundary

playtimed writes exactly one file per browser family, named `playtimed.json`, and touches no other file in the policy directory. An administrator who wants rules playtimed does not model — extension blocklists, incognito availability, download restrictions — writes a second file alongside it. Chrome merges every file in the directory; a conflict resolves to the more restrictive value. playtimed neither reads nor overwrites files it did not create.

### Invocation

Generation runs when the daemon starts, on SIGHUP, and on any CLI command that changes a browser domain pattern's state or the daemon mode. `playtimed browser-policy` prints what would be written; `playtimed browser-policy --sync` writes it.

The daemon already runs as root, so no new privilege is required. A generation failure logs and does not abort the poll loop — a browser policy that failed to update is a degraded state, not a reason to stop enforcing process limits.

## Consequences

**The CLI's promise becomes true.** `discover disallow` on a domain produces enforcement, and it is the same enforcement whether the daemon is running or not, because it lives in the browser rather than in a poll loop.

**Enforcement no longer depends on window focus.** A background tab, a second window, and a window on another virtual desktop are all covered. Title-reading remains, but only for what it is good at: attributing time to domains.

**Policy changes need a browser restart.** Chrome reloads managed policy on launch and periodically thereafter, not immediately. A domain disallowed while Chrome is open stays reachable until it reloads. The process layer is instantaneous and the browser layer is not; the CLI says so when it writes.

**Firefox needs a second generator.** Its policy file is `policies.json` under the distribution directory with a different schema (`WebsiteFilter`). The generator is per-browser-family, matching the existing `BrowserWorker` split.

**A non-Chrome browser installed later is unrestricted until playtimed models it.** Managed policy is per-browser; there is no system-wide equivalent. Restricting which browsers can be installed is a package-management concern, outside playtimed.

**Time tracking and access control now have different vocabularies for the same row.** `active` means "counts toward the limit" for a process and "permitted" for a domain under strict mode. This overload is inherited from ADR-001 and is not resolved here.

## Alternatives Considered

**Notify only.** Claude sends a notification when a disallowed domain has focus. Consistent with the project's personality and cheap to build, but a notification is not a limit. The trust situation that motivated strict mode is precisely the one where advisory signals have already failed.

**Kill the browser process.** Blunt in a way that punishes the permitted use alongside the blocked one — closing every tab to close one. It also races the user's relaunch, producing a visible fight rather than a rule.

**A local proxy or DNS sinkhole.** Enforces across every browser at once and does not need per-browser modelling. It also requires TLS interception to see paths, breaks certificate pinning, and turns a screen-time daemon into a man-in-the-middle on a child's traffic. ADR-001's constraint — track where time goes, not what is said — rules this out.

**Chrome extension.** Full navigation-event access via `declarativeNetRequest`, and removable by the user in three clicks unless force-installed by managed policy. If managed policy is required anyway, the extension is a dependency the policy alone does not need.

**Leave it advisory and document it.** Honest and nearly free: make the CLI say the state is informational. Rejected because the data is already in the right shape, the enforcement mechanism already exists, and the only missing piece is a file writer.
