# Changelog

All notable changes to playtimed will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
