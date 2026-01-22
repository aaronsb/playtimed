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
- [ ] Add migration function for new schema
- [ ] Create `message_templates` table
- [ ] Create `message_log` table
- [ ] Seed default templates (3 variants per intention)
- [ ] Add state columns to `daily_summary`
- [ ] Add `message_log` cleanup to maintenance

## Phase 2: Message Router
- [ ] Create `router.py` module
- [ ] Implement template selection (random variant)
- [ ] Implement variable rendering
- [ ] Connect router to `NotificationDispatcher`
- [ ] Add delivery logging

## Phase 3: State Machine Integration
- [ ] Add state tracking to `_process_user()`
- [ ] Implement state transitions
- [ ] Add warning flags (warned_30, warned_15, warned_5)
- [ ] Update to timestamp-based time tracking
- [ ] Handle suspend/resume (cap elapsed time)

## Phase 4: CLI & Testing
- [ ] `playtimed message test <intention>` command
- [ ] `playtimed message list` command
- [ ] `playtimed message add` command
- [ ] Tests for router logic
- [ ] Tests for state machine transitions

## Notes
- Single user per session (multiple users possible, not simultaneous)
- Backend priority: Clippy → Freedesktop → Log-only
- Warnings are events during AVAILABLE state, not separate state
