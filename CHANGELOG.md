# Changelog

All notable changes to playtimed will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
