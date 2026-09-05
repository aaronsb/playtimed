"""Window schedules — when a user may spend time, and how much (ADR-004).

A window covers a contiguous range of hours on a set of days and carries a
mode and an optional budget. Windows tile the week: for every day, the windows
covering it partition hours 0-24 with no gaps and no overlaps, so no hour can
fall under two rules or none.

Nothing here touches the database. `db.py` stores these; the daemon asks
`window_for()` what the current hour is governed by.
"""

from dataclasses import dataclass, replace

DAY_NAMES = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')

RESTRICTED = 'restricted'
OPEN = 'open'
MODES = frozenset({RESTRICTED, OPEN})

METER_GAMING = 'gaming'
METER_ALL = 'all'
METERS = frozenset({METER_GAMING, METER_ALL})

WEEKDAYS = '1111100'
WEEKENDS = '0000011'
EVERY_DAY = '1111111'

#: The mode a window maps onto for the daemon and for ADR-003 policy generation.
DAEMON_MODE = {RESTRICTED: 'strict', OPEN: 'normal'}


@dataclass(frozen=True)
class Window:
    """One rule, covering ``start_hour`` up to but not including ``end_hour``.

    ``days`` is seven characters over ``{0,1}``, Monday first. ``budget_minutes``
    of None means uncapped. ``meters`` selects which activity the budget counts:
    ``gaming`` counts game processes and gaming-category browser domains,
    ``all`` counts every tracked second.
    """

    days: str
    start_hour: int
    end_hour: int
    mode: str
    budget_minutes: int | None = None
    meters: str = METER_GAMING
    id: int | None = None

    def applies_on(self, day: int) -> bool:
        return self.days[day] == '1'

    def covers(self, day: int, hour: int) -> bool:
        return self.applies_on(day) and self.start_hour <= hour < self.end_hour

    @property
    def hours(self) -> range:
        return range(self.start_hour, self.end_hour)

    @property
    def is_capped(self) -> bool:
        return self.budget_minutes is not None

    def label(self) -> str:
        """Human-readable range, e.g. ``3pm-6pm``."""
        return f"{_fmt(self.start_hour)}-{_fmt(self.end_hour)}"


def _fmt(hour: int) -> str:
    if hour in (0, 24):
        return '12am'
    if hour == 12:
        return '12pm'
    suffix = 'am' if hour < 12 else 'pm'
    return f"{hour % 12}{suffix}"


class ScheduleError(ValueError):
    """A window set that does not tile the week."""


def validate(windows: list[Window]) -> list[str]:
    """Return a list of problems; empty means the set tiles every day cleanly."""
    problems = []

    for w in windows:
        if w.mode not in MODES:
            problems.append(f"unknown mode {w.mode!r}")
        if w.meters not in METERS:
            problems.append(f"unknown meters {w.meters!r}")
        if len(w.days) != 7 or set(w.days) - {'0', '1'}:
            problems.append(f"days must be 7 chars of 0/1, got {w.days!r}")
        if not 0 <= w.start_hour < w.end_hour <= 24:
            problems.append(
                f"hours must satisfy 0 <= start < end <= 24, got "
                f"{w.start_hour}..{w.end_hour}")
        if w.budget_minutes is not None and w.budget_minutes < 0:
            problems.append(f"negative budget {w.budget_minutes}")

    if problems:
        return problems

    for day in range(7):
        todays = sorted((w for w in windows if w.applies_on(day)),
                        key=lambda w: w.start_hour)
        cursor = 0
        for w in todays:
            if w.start_hour > cursor:
                problems.append(
                    f"{DAY_NAMES[day]}: no window covers "
                    f"{_fmt(cursor)}-{_fmt(w.start_hour)}")
            elif w.start_hour < cursor:
                problems.append(
                    f"{DAY_NAMES[day]}: windows overlap at {_fmt(w.start_hour)}")
            cursor = max(cursor, w.end_hour)
        if cursor < 24:
            problems.append(
                f"{DAY_NAMES[day]}: no window covers {_fmt(cursor)}-12am")

    return problems


def fill_gaps(windows: list[Window]) -> list[Window]:
    """Cover any unruled hours with restricted windows.

    An hour with no rule has two readings, deny and allow, which are the
    lockdown and its absence. Filling with `restricted` makes the omission
    safe rather than silently permissive.
    """
    filled = list(windows)

    for day in range(7):
        todays = sorted((w for w in filled if w.applies_on(day)),
                        key=lambda w: w.start_hour)
        cursor = 0
        gaps = []
        for w in todays:
            if w.start_hour > cursor:
                gaps.append((cursor, w.start_hour))
            cursor = max(cursor, w.end_hour)
        if cursor < 24:
            gaps.append((cursor, 24))

        for start, end in gaps:
            days = ''.join('1' if d == day else '0' for d in range(7))
            filled.append(Window(days, start, end, RESTRICTED))

    return _merge_identical(filled)


def _merge_identical(windows: list[Window]) -> list[Window]:
    """Collapse windows that differ only in which days they apply to."""
    merged: dict[tuple, list[str]] = {}
    for w in windows:
        key = (w.start_hour, w.end_hour, w.mode, w.budget_minutes, w.meters)
        merged.setdefault(key, []).append(w.days)

    out = []
    for (start, end, mode, budget, meters), day_masks in merged.items():
        days = ''.join(
            '1' if any(m[d] == '1' for m in day_masks) else '0'
            for d in range(7))
        out.append(Window(days, start, end, mode, budget, meters))

    return sorted(out, key=lambda w: (w.start_hour, w.days))


def window_for(windows: list[Window], day: int, hour: int) -> Window | None:
    """The window governing this hour, or None when the set has a gap."""
    for w in windows:
        if w.covers(day, hour):
            return w
    return None


def next_boundary(windows: list[Window], day: int, hour: int) -> int | None:
    """The hour at which the current window ends, or None if unruled.

    Returns 24 for a window running to midnight, which is the boundary even
    though the next window belongs to the following day.
    """
    current = window_for(windows, day, hour)
    return current.end_hour if current else None


def for_user_default() -> list[Window]:
    """The shape brick runs: school hours restricted, metered afternoon, open evening."""
    return [
        Window(WEEKDAYS, 0, 15, RESTRICTED),
        Window(WEEKDAYS, 15, 18, OPEN, budget_minutes=60),
        Window(WEEKDAYS, 18, 22, OPEN),
        Window(WEEKDAYS, 22, 24, RESTRICTED),
        Window(WEEKENDS, 0, 8, RESTRICTED),
        Window(WEEKENDS, 8, 18, OPEN, budget_minutes=360),
        Window(WEEKENDS, 18, 23, OPEN),
        Window(WEEKENDS, 23, 24, RESTRICTED),
    ]


def from_legacy(schedule: str, daily_limits: list[int]) -> list[Window]:
    """Convert a 168-char schedule and seven per-day budgets into windows.

    Each day's string collapses into maximal runs of equal characters; a run of
    '1' becomes an open window, a run of '0' a restricted one. That day's budget
    lands on its first open window, which is where a per-day budget was first
    spendable.
    """
    windows = []

    for day in range(7):
        row = schedule[day * 24:(day + 1) * 24]
        budget_placed = False
        start = 0

        for hour in range(1, 25):
            if hour < 24 and row[hour] == row[start]:
                continue

            days = ''.join('1' if d == day else '0' for d in range(7))
            if row[start] == '1':
                budget = None if budget_placed else daily_limits[day]
                budget_placed = True
                windows.append(Window(days, start, hour, OPEN, budget))
            else:
                windows.append(Window(days, start, hour, RESTRICTED))
            start = hour

    return _merge_identical(windows)


def describe(windows: list[Window], day: int) -> list[str]:
    """One line per window on this day, for `playtimed schedule`."""
    lines = []
    for w in sorted((w for w in windows if w.applies_on(day)),
                    key=lambda w: w.start_hour):
        if w.mode == RESTRICTED:
            detail = 'restricted'
        elif w.is_capped:
            unit = 'all activity' if w.meters == METER_ALL else 'gaming'
            detail = f"open, {w.budget_minutes} min {unit}"
        else:
            detail = 'open, uncapped'
        lines.append(f"{w.label():<12} {detail}")
    return lines


def rebase(windows: list[Window], **changes) -> list[Window]:
    """Apply the same field change to every window (used by CLI bulk edits)."""
    return [replace(w, **changes) for w in windows]


def parse_days(token: str) -> str:
    """Turn ``mon-fri``, ``sat,sun``, ``all`` or ``wed`` into a 7-char mask."""
    token = token.strip().lower()
    if token in ('all', 'daily', 'everyday'):
        return EVERY_DAY

    mask = ['0'] * 7
    for part in token.split(','):
        part = part.strip()
        if '-' in part:
            first, last = (p.strip() for p in part.split('-', 1))
            if first not in DAY_NAMES or last not in DAY_NAMES:
                raise ScheduleError(f"unknown day range {part!r}")
            a, b = DAY_NAMES.index(first), DAY_NAMES.index(last)
            span = range(a, b + 1) if a <= b else [*range(a, 7), *range(b + 1)]
            for d in span:
                mask[d] = '1'
        else:
            if part not in DAY_NAMES:
                raise ScheduleError(f"unknown day {part!r}")
            mask[DAY_NAMES.index(part)] = '1'

    if '1' not in mask:
        raise ScheduleError(f"no days selected in {token!r}")
    return ''.join(mask)


def parse_spec(text: str) -> list[Window]:
    """Parse a semicolon-separated window spec.

    Each clause is ``<days> <start>-<end> <mode>[:<budget>][/all]``::

        mon-fri 0-15 restricted; mon-fri 15-18 open:60/gaming;
        mon-fri 18-22 open; sat-sun 8-18 open:360

    Hours are 24-hour and the end is exclusive, so ``22-24`` runs to midnight.
    Uncovered hours are filled with restricted windows rather than left open.
    """
    windows = []

    for clause in text.split(';'):
        clause = clause.strip()
        if not clause:
            continue

        parts = clause.split()
        if len(parts) != 3:
            raise ScheduleError(
                f"expected '<days> <start>-<end> <mode>', got {clause!r}")

        days = parse_days(parts[0])

        if '-' not in parts[1]:
            raise ScheduleError(f"expected '<start>-<end>', got {parts[1]!r}")
        try:
            start, end = (int(h) for h in parts[1].split('-', 1))
        except ValueError:
            raise ScheduleError(f"hours must be integers, got {parts[1]!r}") from None

        rule = parts[2].lower()
        meters = METER_GAMING
        if '/' in rule:
            rule, meters = rule.split('/', 1)
            if meters not in METERS:
                raise ScheduleError(f"unknown meter {meters!r}")

        budget = None
        if ':' in rule:
            rule, raw = rule.split(':', 1)
            try:
                budget = int(raw)
            except ValueError:
                raise ScheduleError(f"budget must be minutes, got {raw!r}") from None

        if rule not in MODES:
            raise ScheduleError(f"unknown mode {rule!r}")
        if rule == RESTRICTED and budget is not None:
            raise ScheduleError("a restricted window has nothing to budget")

        windows.append(Window(days, start, end, rule, budget, meters))

    filled = fill_gaps(windows)
    problems = validate(filled)
    if problems:
        raise ScheduleError('; '.join(problems))
    return filled


def to_spec(windows: list[Window]) -> str:
    """Render windows back into the spec syntax `parse_spec` accepts."""
    clauses = []
    for w in sorted(windows, key=lambda w: (w.days, w.start_hour)):
        rule = w.mode
        if w.is_capped:
            rule += f":{w.budget_minutes}"
            if w.meters != METER_GAMING:
                rule += f"/{w.meters}"
        clauses.append(f"{_days_label(w.days)} {w.start_hour}-{w.end_hour} {rule}")
    return '; '.join(clauses)


def _days_label(mask: str) -> str:
    """Shortest readable label for a day mask."""
    if mask == EVERY_DAY:
        return 'all'
    if mask == WEEKDAYS:
        return 'mon-fri'
    if mask == WEEKENDS:
        return 'sat-sun'

    days = [DAY_NAMES[d] for d in range(7) if mask[d] == '1']
    runs, run = [], [days[0]]
    for name in days[1:]:
        if DAY_NAMES.index(name) == DAY_NAMES.index(run[-1]) + 1:
            run.append(name)
        else:
            runs.append(run)
            run = [name]
    runs.append(run)

    return ','.join(r[0] if len(r) == 1 else f"{r[0]}-{r[-1]}" for r in runs)


def resolve_mode(current: list, configured: str) -> str:
    """The enforcement mode the schedule asks for right now (ADR-004).

    `current` holds one entry per monitored user — the window governing that
    user this hour, or None where they have no schedule. The most restrictive
    window wins, because Chrome's managed policy is machine-wide (ADR-003) and
    can only express one answer for the whole machine.

    `passthrough` is a manual override rather than a schedule state, so it
    outranks every window. With nobody scheduled, the configured mode stands.
    """
    if configured == 'passthrough':
        return 'passthrough'

    scheduled = [w for w in current if w is not None]
    if not scheduled:
        return configured
    if any(w.mode == RESTRICTED for w in scheduled):
        return DAEMON_MODE[RESTRICTED]
    return DAEMON_MODE[OPEN]
