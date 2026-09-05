# ADR-004: Window Schedules with Per-Window Budgets

Status: Proposed
Date: 2026-09-04
Deciders: @aaron, @claude

## Context

playtimed expresses a user's allowance in two structures that were designed for
one question each, and it now has to answer a third.

`user_limits.schedule` is a 168-character string over `{0,1}`, one character per
hour of the week, where `1` means gaming is permitted in that hour. It answers
*when*. `user_limits.daily_limits` is seven integers, one per weekday, giving a
gaming budget in minutes. It answers *how much*. `daemon_config.mode` is a single
global row holding `normal`, `strict`, or `passthrough`. It answers *how tightly*,
for the whole machine, for all time.

The requirement that does not fit is a budget scoped to part of a day:

> weekdays 8am–3pm is IXL only; 3–6pm is a maximum of one hour of games or
> anything else; 6–10pm is any application with no time cap; after 10pm, IXL only.
> Weekends, 8am–6pm is any application capped at six hours, and 6–11pm is any
> application uncapped.

Three things break at once. A per-day budget cannot express "one hour between
three and six" — set `daily_limits` to 60 and the cap follows him into the
uncapped evening, so the 6–10pm window silently becomes "whatever is left of the
hour." A binary schedule cannot distinguish a capped open window from an uncapped
one, because both are `1`. And a single global `mode` cannot be IXL-only at noon
and open at seven.

The workaround available today is to change `mode` by hand twice a day, or by
cron, which puts the schedule in a crontab where nothing else about the user's
allowance lives, and leaves `playtimed status` unable to say what the rules are.

An adjacent contradiction disappears once budgets are window-scoped.
`user_limits.daily_total` caps total screen time per day, independent of gaming.
Under the requirement above it binds in the middle of a window that was specified
as uncapped, so "no time cap" would be false as written.

## Decision

A user's allowance is a set of **windows**. Each window covers a contiguous range
of hours on a set of days and carries a mode and an optional budget. Windows
replace `schedule`, `daily_limits`, and `daily_total` as the source of truth for
when and how much.

```sql
CREATE TABLE schedule_windows (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user           TEXT NOT NULL,
    days           TEXT NOT NULL,     -- 7 chars, Mon..Sun, '1' = window applies
    start_hour     INTEGER NOT NULL,  -- 0..23, inclusive
    end_hour       INTEGER NOT NULL,  -- 1..24, exclusive
    mode           TEXT NOT NULL,     -- 'restricted' | 'open'
    budget_minutes INTEGER,           -- NULL = uncapped
    meters         TEXT NOT NULL DEFAULT 'gaming',  -- 'gaming' | 'all'
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
```

### Windows tile the week

For every user and every day, the windows covering that day partition hours 0–24
exactly: no gaps, no overlaps. The invariant is checked on write and repaired by
filling any uncovered range with a `restricted` window.

A gap would otherwise be an hour with no rule, and the two available readings of
an unruled hour — deny and allow — are the lockdown and its absence. Tiling makes
the question unanswerable rather than answered by accident.

### Mode is derived, not stored

The daemon computes its enforcement mode each poll from the window covering the
current hour: `restricted` yields `strict`, `open` yields `normal`.
`daemon_config.mode` keeps one job, as a manual override that outranks the
schedule, which is what `passthrough` already is and what an administrator
suspending enforcement for an evening needs.

A mode change fires the path `_reload_config()` already runs — log the
transition, notify through the router, and regenerate the browser policy per
ADR-003. Scheduled transitions and SIGHUP become the same event.

### Budgets are consumed within their window

A window's spend is the sum of `hourly_activity` rows for that user, for today,
over the hours the window covers — `gaming_seconds` when `meters` is `gaming`,
`total_seconds` when it is `all`. Exhaustion blocks for the remainder of that
window only, and the next window starts at zero.

No column records window consumption. `hourly_activity` already stores the per-hour
totals a window is a range over, so a derived sum cannot drift from the activity
it summarizes.

### What a budget meters

`meters` defaults to `gaming`, which counts game processes and the browser domains
already classified as gaming — the YouTube and Reddit rows that ADR-001 tracks.
Productive and educational time is free.

The requirement says "an hour of any games or things," which reads as all non-school
activity. Metering everything would spend the hour on art, music, and coding tools,
which are the applications this project has been most reluctant to restrict. The
narrower default is the recoverable error: a window can be switched to `all`
without a schema or code change, and the setting is per-window rather than global,
so a capped afternoon and an uncapped evening need not agree.

### The always-available floor

Educational domains are permitted in every window. In a `restricted` window ADR-003
writes them into `URLAllowlist`; in an `open` window nothing blocks them. IXL is
reachable at every hour of the week as a consequence of the existing generation
rules, with no special case.

### Machine-wide policy against per-user windows

Windows are per-user. Chrome's managed policy is machine-wide (ADR-003, *Scope*),
so when several monitored users hold different modes at the same moment, the
browser can only enforce one. The generated policy takes the most restrictive mode
among monitored users and logs which user determined it.

This is the honest failure: two children on one machine share the tighter schedule.
Storing windows globally instead would trade a visible limitation for an invisible
one, since the process-side enforcement they also drive is genuinely per-user.

### Migration

Existing rows are converted rather than discarded. Each day's `schedule` string
collapses into maximal runs of equal characters; a run of `1` becomes an `open`
window and a run of `0` a `restricted` one. That day's `daily_limits` value becomes
the `budget_minutes` of its first `open` window, which is where a per-day budget
was first spendable. `daily_total` has no window equivalent and is dropped.

The old columns are left in place and stop being read, so a downgrade to 0.5.x
finds its configuration intact.

## Consequences

**The rules become inspectable.** `playtimed status` can state the current window,
its mode, and what remains of its budget. Under a cron-driven `mode` flip, the
daemon could not have known any of it.

**Two clocks now stop a game, and they say different things.** A window boundary
ends play because time passed; a budget ends it because time was spent. The
messages differ, and a boundary is knowable in advance while an exhaustion is not.
Warning before a boundary is possible and worth doing; the same warning for a
budget is a projection.

**Hour granularity is now load-bearing.** Windows begin and end on the hour because
`hourly_activity` buckets by hour, and a half-hour boundary would split a bucket no
existing row can divide. Every requirement above lands on an hour boundary.

**A budget's last minute is approximate.** Consumption within the current hour is
whatever the poll loop has accrued so far, so exhaustion is detected within a poll
interval rather than at the second.

**`daily_total` is gone as an enforcement input.** Total screen time is still
recorded in `daily_summary`; nothing caps it. A user who wants a total cap needs it
expressed as a window budget with `meters = 'all'`.

**A third state is added to a vocabulary ADR-003 already called strained.**
`monitor_state` says whether a pattern is tracked, and a window now says whether
tracked time is spendable. The two are orthogonal, and a pattern that is `active`
in a `restricted` window is tracked, permitted to exist, and unreachable in the
browser at the same time.

## Alternatives Considered

**Extend the schedule alphabet.** Add `2` to the 168-character string for
"permitted and metered," inferring windows from maximal runs of equal characters.
No new table, and the existing grid renderer nearly works. Rejected because window
identity becomes an artifact of adjacency: two intentionally distinct windows that
happen to share a character silently merge into one budget, and splitting them
requires inserting an hour that differs.

**Per-window budget columns on `user_limits`.** A fixed set of named windows —
morning, afternoon, evening — each with its own budget. Simple to query and
impossible to outgrow gracefully; the weekend shape here already needs different
boundaries than the weekday shape.

**Cron-driven `playtimed mode` flips.** Zero code. Rejected because the schedule
then lives in a crontab, `playtimed status` cannot report the rules it is enforcing,
and a missed cron leaves the machine in the wrong mode with nothing to reconcile
against.

**Track window consumption in its own column.** A counter per window, incremented
by the poll loop and reset at the boundary. Rejected as a second copy of what
`hourly_activity` already holds, with the reset as a new way to be wrong.
