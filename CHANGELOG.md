# Changelog

All notable changes to playtimed will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] - 2026-09-04

### Added
- **Window schedules** (ADR-004) — a user's allowance is a set of windows, each covering a contiguous range of hours on a set of days and carrying a mode (`restricted` or `open`) and an optional budget. Windows tile the week: for every day they partition hours 0-24 with no gaps and no overlaps, so no hour falls under two rules or none. An hour left unruled is filled as `restricted`, because the two readings of an unruled hour are the lockdown and its absence
- Budgets are scoped to their window. "One hour of games between three and six, then an uncapped evening" is now expressible; under per-day limits the cap followed the user into the evening that was specified as uncapped
- Each window chooses what its budget meters: `gaming` counts game processes and gaming-category browser domains, `all` counts every tracked second. The default is `gaming`, so productive and educational time stays free
- The daemon derives its enforcement mode from the window covering the current hour and re-evaluates every poll, since the thing that changes it is the clock. A transition regenerates the browser policy through the path SIGHUP already used, so ADR-003 enforcement follows the schedule with no new mechanism
- `playtimed windows show|set|preset`. `windows set` takes a spec — `mon-fri 0-15 restricted; mon-fri 15-18 open:60` — and refuses a set that does not tile, leaving the previous schedule in force
- Window spend is summed from `hourly_activity` over the hours a window covers rather than kept in a counter, so it cannot drift from the activity it summarizes and has no reset to get wrong

### Changed
- `playtimed status` reports the window in force, what has been spent inside it, and what remains, in place of a per-day gaming and total figure
- `playtimed schedule` and `schedule view` render windows — the command shows what is actually enforced
- `schedule export` emits a spec string that `windows set` accepts, making an export a restorable backup rather than a description
- `daemon_config.mode` keeps one job as a manual override. `passthrough` outranks the schedule, which is what suspending enforcement has to mean; `normal` and `strict` are superseded by the window each poll
- When monitored users hold different modes at the same moment, the most restrictive wins. Chrome's managed policy is machine-wide (ADR-003) and can only express one answer for the machine

### Removed
- `daily_total` is no longer an enforcement input. Under a window schedule it bound in the middle of a window specified as uncapped, which made "no time cap" false as written. Total time is still recorded in `daily_summary`; nothing caps it. A total cap is now expressed as a window budget with `meters = all`
- `schedule set`, `schedule edit`, and `schedule import` refuse and name their replacement. They wrote a structure enforcement no longer reads, which would have produced a schedule that displays one thing and enforces another
- The curses grid editor is removed rather than ported. It edits one cell per hour over `{0,1}`, and a window carries a mode, a budget, and a meter that no single cell can hold
- `_get_remaining_time` and `_send_warning_if_needed`, both dead before this release and both computing the per-day model it retires

### Migration
- Existing schedules convert automatically. Each day's grid collapses into maximal runs, a run of `1` becoming an `open` window, and that day's `daily_limits` value lands on its first `open` window — where a per-day budget was first spendable
- The legacy `schedule` and `daily_limits` columns are left in place and stop being read, so a downgrade to 0.5.x finds its configuration intact
- Migration skips a user who already has windows, so operator edits survive every later pass

## [0.5.4] - 2026-09-02

### Fixed
- **Strict mode admitted unreviewed discoveries** — the mode advertises "whitelist only", but the poll loop only reached the warn-then-kill path when a process matched no pattern at all. A process matching a pattern in `discovered` state — a row discovery created automatically the first time it crossed the CPU threshold — ran unchallenged. On a host that had been in `normal` mode for a while, that covered most of what a user actually launches, so switching to `strict` changed nothing for them. Admission is now an explicit allowlist of `active` and `ignored`
- **`discover disallow` on a browser domain did nothing** — the CLI wrote `disallowed` to the row and printed "will be terminated on detection", but the kill path operates on PIDs and browser domains are handled in a separate block that only records runtime. A domain could sit disallowed indefinitely and remain reachable. ADR-001 listed this as a stretch goal; it was never built

### Added
- **Browser managed-policy generation** (ADR-003) — playtimed writes Chrome-family and Firefox policy files from its own `browser_domain` patterns, so the browser enforces at navigation time across every tab and window rather than playtimed reacting to whichever tab has focus. `normal` mode generates a blocklist from disallowed domains; `strict` generates an allowlist of permitted domains with everything else blocked; `passthrough` withdraws playtimed's rules
- `playtimed browser-policy` shows what would be written, `--sync` writes it. Generation also runs at daemon startup, on SIGHUP, and after any CLI command that changes a domain's state or the daemon mode
- For Chrome-family browsers playtimed writes exactly one file, `playtimed.json`, and never reads or overwrites others in the merged policy directory. Firefox has no such directory, so there playtimed owns the `policies.WebsiteFilter` key inside the shared `policies.json` and leaves the rest of the document intact
- An `active` gaming-category domain is written into the blocklist rather than the allowlist. `active` means "counted against the limit", and playtimed cannot close a tab when a domain's budget expires — so on a host that had been tracking YouTube time, reading `active` as "permitted" would have generated an allowlist of exactly the sites a lockdown exists to block

### Changed
- `ActivityDB.get_patterns()` now returns only `pattern_type = 'process'` rows by default. A `browser_domain` pattern holds a hostname, but process matching is an unanchored regex search over process name and command line, so a domain row in that scan meant any process whose cmdline contained the string was treated as that pattern — and a disallowed one was killed. Pass `pattern_type=None` for every type
- `get_browser_patterns()` now honours the `enabled` column, which it previously ignored
- The systemd unit grants write access to each browser's configuration root under `/etc`. `ProtectSystem=strict` made all of `/etc` read-only, so daemon-side policy writes would have failed `EROFS` and been logged as a warning while the feature appeared to work

### Security
- **Browser database copies used `tempfile.mktemp()`** — the workers copy Chrome's `History` and Firefox's `places.sqlite` somewhere unlocked before reading them, and `mktemp` returns a path without creating it. The daemon runs as root, the source is a file the monitored user owns and controls the contents of, and `/tmp` is world-writable, so that user could plant a symlink at the predicted path and have root write bytes of their choosing to a path of their choosing. `PrivateTmp=true` in the unit blocks it for the packaged daemon; `playtimed run` started by hand is not covered. Both workers now call one helper in `browser/base.py`, built on `mkstemp`, which creates the file itself with mode 0600. The helper removes the file if the copy raises: the caller has no name for it until the helper returns, so nothing else can — and a directory left at the profile path satisfies the caller's `.exists()` guard, making the copy fail on every poll
- The content goes through the descriptor `mkstemp` returned rather than being written back by path. A write by name follows a symlink at the destination and carries no `O_EXCL`, so reopening would have reopened the same question, leaving only `/tmp`'s sticky bit between root and a user-directed write
- The copy is a descriptor write rather than `shutil.copy2`. Browsers leave those profile files world-readable, and `copy2` would preserve the source mode — widening the 0600 back out and exposing the user's browsing history to every local account for as long as the copy exists

### Changed
- `make check` passes. It never had: ruff's default rule selection grows between minor releases, so the gate went red on code no commit touched, reaching 244 findings. ruff is now pinned in the dev extra, three rule groups are switched off with the reason recorded beside each (naive datetimes, which are deliberate for a daemon whose budgets are per local calendar day; blind excepts, which are all log-and-continue probes; and the iteration idiom for entries that vanish mid-scan), and the rest are fixed
- `playtimed message list` prints the urgency of each template. The colour was computed and discarded

### Note
Chrome reloads managed policy on launch and periodically thereafter, not immediately. A domain disallowed while the browser is open stays reachable until it reloads — the process layer is instantaneous and the browser layer is not.

## [0.5.3] - 2026-07-27

### Fixed
- **A global catch-all outranked user-scoped patterns** — process matching tested every gaming pattern before any launcher pattern, so the global `\.exe$` catch-all claimed Wine helper processes that were already classified as launchers. On a Wine-heavy host this tracked `explorer.exe`, `winedevice.exe`, `xalia.exe`, `svchost.exe` and friends as "Proton Game", one session apiece — a single afternoon of one game produced 37 helper sessions, burying the real game in the session log

### Changed
- Gaming and launcher patterns are now matched as one ordered set via `rank_patterns()`, ranked by specificity first and category second: a user-scoped pattern outranks a global one whatever its category, and within the same scope gaming still outranks launcher so a game misfiled as a launcher keeps counting

### Note
Reclassifying Wine helpers as launchers only takes effect on this release. On 0.5.2 and earlier the same reclassification made things worse, because the helpers then fell through to the `\.exe$` catch-all and were counted under a misleading name.

## [0.5.2] - 2026-07-27

### Fixed
- **Status total percentage read a dead column** — `playtimed status` computed the Total progress bar from the legacy `daily_total` column while Gaming used the current per-day limits, so a stale value could render nonsense (789% against a real 100%). Both now measure against `daily_limits`, completing the migration started in 0.5.1
- **History coloured every row against a flat limit** — `playtimed history` compared each day's gaming time to the legacy `gaming_limit` column instead of that day's own budget, marking weekday rows red against a weekend allowance. Each row is now coloured against its own day's limit
- **Warns column always showed 0** — `playtimed history` read the `warnings_sent` counter, which the daemon never increments; warnings are recorded as the `warned_30`/`warned_15`/`warned_5` flags. The column now derives its count from those flags
- **Sessions leaked on unclean daemon exit** — active sessions are tracked in memory only, so a crash, reboot, or SIGKILL left rows with no `end_time` while the next poll opened fresh ones for the same processes. Open sessions are now closed on shutdown, and any left behind by a previous run are reconciled at startup

### Added
- `ActivityDB.get_open_sessions()`, `close_session()`, and `get_last_poll_at()` supporting session reconciliation
- Sessions closed at an unverified upper bound record a NULL duration and display as `unknown` rather than being credited with playtime that may never have happened — they drop out of per-app aggregates instead of inflating them
- `shutdown` and `orphaned` end reasons distinguish a clean daemon stop from a session recovered after an unclean exit

## [0.5.1] - 2026-02-17

### Fixed
- **Schedule editor crashed on small terminals** — added a terminal size check before rendering the 7×24 grid
- **Launcher misclassification** — high-CPU launcher processes are flagged as possibly misclassified games
- **Gaming pattern priority** — a gaming pattern now takes precedence over a launcher pattern when both match a process

### Changed
- **Schedule string is the sole source of truth** — removed the legacy scheduling columns in favour of the 168-character schedule string and `daily_limits`

## [0.5.0] - 2026-02-08

### Added
- **Per-day gaming limits** — each weekday carries its own budget via `daily_limits` instead of one flat number
- **Termination audit** — `playtimed audit` reports process terminations over the last 30 days
- **Creative category** — process patterns can be categorised as creative work, tracked separately from gaming

## [0.4.0] - 2026-02-08

### Added
- **Per-hour schedule grid** — 168-character schedule string (7 days × 24 hours) controlling when gaming is permitted
- **Interactive schedule editor** — `playtimed schedule` renders an editable 7×24 grid
- **Activity heatmap** — `playtimed heatmap` visualises usage by hour and day
- **CPU hysteresis** — smooths activity detection so brief CPU dips don't end a session prematurely

## [0.3.4] - 2026-02-07

### Added
- **Domain Exclusion Filter** — Shared `is_excluded_domain()` in base class filters CDN/infrastructure domains (googlevideo.com, gstatic.com, cloudfront.net, accounts.google.com, etc.) from all browser session file readers

## [0.3.3] - 2026-02-07

### Added
- **Firefox Session File Reading** — Reads `recovery.jsonlz4` to get all open Firefox tabs including background tabs, matching Chrome's session file capability. Requires `python-lz4` (optional dependency)

## [0.3.2] - 2026-02-07

### Fixed
- **Browser Domain Detection** — Chrome session file results no longer block Firefox window title detection. Previously `get_active_domains()` returned early when Chrome data was available, making Firefox domains invisible to the daemon

## [0.3.1] - 2026-02-07

### Added
- **Firefox Domain Tracking** — Firefox browsing now resolves domains via places.sqlite history lookup, same as Chrome. Detects sites like discord.com that were previously invisible when accessed through Firefox
- **ADR-002: Modular Worker Architecture** — Architecture decision record for decomposing the monolith into detection workers, enforcement kernel, communication workers, and reporting modules

### Changed
- **Browser Module Refactor** — Moved shared code (SITE_SIGNATURES, signature matching, title cleaning) from Chrome-specific into `BrowserWorker` base class. Both Chrome and Firefox workers now use the common interface
- **`extract_domain_from_title()`** now iterates all registered workers instead of hardcoding Chrome

## [0.3.0] - 2026-02-07

### Added
- **CLI: `playtimed history`** — Daily screen time summaries with colored usage warnings
- **CLI: `playtimed sessions`** — Individual game session details with start time, duration, and end reason
- **CLI: `playtimed report`** — Week-at-a-glance with totals, averages, and top apps breakdown
- **Proton Auto-Discovery** — Windows games via Proton/Wine are now individually identified instead of lumped as "Proton Game". Each .exe gets its own tracked pattern (e.g., "FalloutNV" instead of "Proton Game")

### Fixed
- **Session Duration Tracking** — Sessions now properly record end_time, duration, and end_reason. Previously `db.end_session()` was never called, leaving all sessions with NULL duration
- **Pattern Matching Order** — User-specific patterns now match before global catchalls, so individually discovered games take priority over generic patterns

## [0.2.4] - 2026-01-27

### Fixed
- **Browser Domain Runtime Tracking**: Discovered browser domains now accumulate runtime like process patterns
  - Previously only "active" browser domains tracked time, making discovery review impossible
  - Now all browser patterns (active, discovered, ignored) track runtime for evaluation
- **Systemd Service Capabilities**: Added CAP_SETUID/CAP_SETGID for user notification delivery
  - Fixes "runuser: cannot set groups" error when sending desktop notifications

## [0.2.3] - 2026-01-23

### Added
- **Browser Worker Architecture**: Modular browser detection with Chrome history DB fallback
  - `ChromeWorker` with signature matching and SQLite history lookup
  - `FirefoxWorker` stub for future implementation
  - Resolved domains even when window titles don't match known patterns
- **`--name` Option for Promote**: `discover promote --name "Display Name"` sets friendly pattern names

### Changed
- Browser detection refactored from single module to `browser/` package
- Domain resolution now falls back to Chrome history database when signatures fail

## [0.2.2] - 2026-01-22

### Added
- **Browser Domain Tracking**: Detect websites in Chrome/Firefox via KWin D-Bus window titles
- **Discovery for Browser Domains**: Unknown domains enter discovery queue like processes
- **D-Bus Session Access**: Daemon connects to user session bus for browser detection

## [0.2.1] - 2026-01-22

### Fixed
- **User-Targeted Notifications**: Daemon now sends desktop notifications to the correct user's session bus
  - Connects to `/run/user/<uid>/bus` instead of daemon's non-existent session
  - Notifications now appear on Anders' desktop instead of falling back to logs
  - Per-user backend caching with automatic reconnect on logout/login

## [0.2.0] - 2026-01-21

### Added
- **Message Router**: Centralized notification handling with template selection and variable rendering
- **Message Templates**: 24 default templates with multiple variants per intention for variety
- **NotificationBackend Protocol**: Abstraction layer with priority fallback (Clippy → Freedesktop → Log-only)
- **Database State Machine**: Warning flags (warned_30/15/5) prevent duplicate notifications
- **Timestamp-Based Time Tracking**: Accurate time calculation with suspend/resume handling
- **CLI Commands**: `playtimed message list|test|add` for template management
- **32 New Tests**: Router tests (13) and state machine tests (19), now 65 total

### Changed
- Daemon now uses `MessageRouter` for all notifications instead of inline templates
- State tracking moved from JSON files to SQLite database
- Time tracking uses wall-clock timestamps instead of poll intervals
- Large time gaps (>2x poll interval) are capped to handle laptop suspend

### Fixed
- Warning notifications no longer repeat every poll cycle (flag-based deduplication)
- Time tracking accuracy improved for variable poll timing

## [0.1.0] - 2026-01-20

### Added
- Initial MVP release
- Process monitoring daemon with CPU-based activity detection
- SQLite database for metrics, patterns, and user configuration
- KDE/Freedesktop notification support
- CLI for status, user management, pattern management
- Automatic database retention (30 days events, 90 days sessions)
- Daemon modes: normal, passthrough, strict
- Process discovery for unknown high-CPU applications
- Install/uninstall scripts with isolated venv
