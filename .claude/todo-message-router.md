# Message Router Implementation

Branch: `feature/message-router`
Design: `docs/message-router-design.md`

## Phase 0: Refactor Existing Code
- [x] Create `NotificationBackend` protocol in `notify.py`
- [x] Wrap existing `Notifier` as `FreedesktopBackend`
- [x] Add `LogOnlyBackend` for fallback
- [x] Create `NotificationDispatcher` with backend chain
- [x] Add placeholder for `ClippyBackend`

## Phase 1: Database & Templates
- [x] Add migration function for new schema
- [x] Create `message_templates` table
- [x] Create `message_log` table
- [x] Seed default templates (3 variants per intention)
- [x] Add state columns to `daily_summary`
- [x] Add `message_log` cleanup to maintenance

## Phase 2: Message Router
- [x] Create `router.py` module
- [x] Implement template selection (random variant)
- [x] Implement variable rendering
- [x] Connect router to `NotificationDispatcher`
- [x] Add delivery logging

## Phase 3: State Machine Integration
- [x] Add state tracking to `_process_user()`
- [x] Implement state transitions
- [x] Add warning flags (warned_30, warned_15, warned_5)
- [x] Update to timestamp-based time tracking
- [x] Handle suspend/resume (cap elapsed time)

## Phase 4: CLI & Testing
- [x] `playtimed message test <intention>` command
- [x] `playtimed message list` command
- [x] `playtimed message add` command
- [x] Tests for router logic
- [ ] Tests for state machine transitions (future)

## Notes
- Single user per session (multiple users possible, not simultaneous)
- Backend priority: Clippy → Freedesktop → Log-only
- Warnings are events during AVAILABLE state, not separate state
