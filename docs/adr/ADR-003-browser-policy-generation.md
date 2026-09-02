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
| `strict` | `URLBlocklist: ["*"]` plus `URLAllowlist` from every permitted domain |
| `passthrough` | No rules; a previously generated policy is withdrawn |

Strict mode's browser policy mirrors its process policy: an allowlist, with `discovered` outside it. This is the same admission rule ADR-003's sibling fix applied to `strict_admits()` — a state nobody has ruled on does not grant access.

Strict mode carries one exception. `active` means "tracked, and counted against the limit", which for a process is enforceable — the daemon kills it when the budget runs out. For a domain it is not: playtimed cannot close a tab. An `active` gaming-category domain is therefore a budget with no enforcement behind it, and is written into the blocklist rather than admitted on a promise that cannot be kept. `ignored` remains an unconditional permit, category regardless.

This matters most on exactly the hosts that need locking down. A host that has run in `normal` mode accumulates gaming domains in `active` state — that is what tracking YouTube time looks like. Reading `active` as "permitted" would generate an allowlist containing precisely the sites the lockdown exists to block.

### Ownership boundary

Chrome-family browsers read a policy *directory* and merge every file in it, resolving conflicts to the more restrictive value. There playtimed writes exactly one file, `playtimed.json`, and neither reads nor overwrites any other. An administrator who wants rules playtimed does not model — extension blocklists, incognito availability, download restrictions — writes a second file alongside it.

Firefox has no such directory: every policy lives in one `policies.json`. Owning that file would mean destroying the administrator's Firefox policy on write and deleting it on `passthrough`. So for Firefox the unit of ownership is a key rather than a file — playtimed reads the existing document, replaces `policies.WebsiteFilter`, and writes the rest back untouched. Relaxing to `passthrough` removes that key and leaves the document; the file is deleted only when nothing remains in it.

### Scope

Managed policy is per-browser and machine-wide. There is no per-user equivalent, so the generated policy is built from every owner's domains at once and applies to anyone who logs into the machine — an administrator who browses on the same host included. Separating them requires a separate machine or a browser playtimed does not model.

### Invocation

Generation runs when the daemon starts, on SIGHUP, and on any CLI command that changes a browser domain pattern's state or the daemon mode. `playtimed browser-policy` prints what would be written; `playtimed browser-policy --sync` writes it.

The daemon runs as root, but under `ProtectSystem=strict`, which makes all of `/etc` read-only. Each browser's configuration root is granted back through an optional `ReadWritePaths=-/etc/...` entry in the unit; without them every daemon-side write fails `EROFS` and the generated policy silently stops tracking the database. Writes are skipped when the file already holds the intended content, so the daemon's periodic reload does not rewrite policy every few minutes.

A generation failure logs and does not abort the poll loop — a browser policy that failed to update is a degraded state, not a reason to stop enforcing process limits.

## Consequences

**The CLI's promise becomes true.** `discover disallow` on a domain produces enforcement, and it is the same enforcement whether the daemon is running or not, because it lives in the browser rather than in a poll loop.

**Enforcement no longer depends on window focus.** A background tab, a second window, and a window on another virtual desktop are all covered. Title-reading remains, but only for what it is good at: attributing time to domains.

**Policy changes need a browser restart.** Chrome reloads managed policy on launch and periodically thereafter, not immediately. A domain disallowed while Chrome is open stays reachable until it reloads. The process layer is instantaneous and the browser layer is not; the CLI says so when it writes.

**Firefox is generated from the same inputs through a different renderer.** Its schema is `WebsiteFilter` with `Block`/`Exceptions` over match patterns rather than bare hosts, so a stored hostname carrying a port or credentials renders a pattern Firefox silently drops — failing open. Domains are reduced to a bare host before rendering, and anything that is not a hostname is discarded rather than emitted.

**A non-Chrome browser installed later is unrestricted until playtimed models it.** Managed policy is per-browser; there is no system-wide equivalent. Restricting which browsers can be installed is a package-management concern, outside playtimed.

**Time tracking and access control share one vocabulary that does not quite fit both.** `active` means "counts toward the limit" for a process and "permitted" for a domain, and the gaming-category exception above patches over the gap rather than closing it. The overload is inherited from ADR-001 and is not resolved here.

**Browser domain rows must stay out of process matching.** A `browser_domain` pattern holds a hostname; process matching is an unanchored regex search over process name and command line. A domain row reaching that scan means any process whose cmdline contains the string is treated as that pattern, and a disallowed one is killed — `discover disallow <domain>` producing a dead process, the outcome this ADR rejects. `get_patterns()` now filters to `pattern_type = 'process'` by default.

## Alternatives Considered

**Notify only.** Claude sends a notification when a disallowed domain has focus. Consistent with the project's personality and cheap to build, but a notification is not a limit. The trust situation that motivated strict mode is precisely the one where advisory signals have already failed.

**Kill the browser process.** Blunt in a way that punishes the permitted use alongside the blocked one — closing every tab to close one. It also races the user's relaunch, producing a visible fight rather than a rule.

**A local proxy or DNS sinkhole.** Enforces across every browser at once and does not need per-browser modelling. It also requires TLS interception to see paths, breaks certificate pinning, and turns a screen-time daemon into a man-in-the-middle on a child's traffic. ADR-001's constraint — track where time goes, not what is said — rules this out.

**Chrome extension.** Full navigation-event access via `declarativeNetRequest`, and removable by the user in three clicks unless force-installed by managed policy. If managed policy is required anyway, the extension is a dependency the policy alone does not need.

**Leave it advisory and document it.** Honest and nearly free: make the CLI say the state is informational. Rejected because the data is already in the right shape, the enforcement mechanism already exists, and the only missing piece is a file writer.
