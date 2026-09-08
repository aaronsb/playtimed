# ADR-005: Allowances — a Rationed Exception Inside a Restricted Window

Status: Accepted
Date: 2026-09-08
Deciders: @aaron, @claude

## Context

ADR-004 gave a restricted window one rule: the always-available floor and
nothing else. On brick that floor is IXL, and school hours are IXL only. The
requirement that does not fit is a small, renewing exception inside that
lockdown:

> During the IXL-only time, Discord is available — web or otherwise — for
> five minutes per hour.

Discord on brick is two patterns: the desktop application, a `process` row,
and `discord.com`, a `browser_domain` row. Both are `active` and categorised
`productive`, which is what strict mode admits without limit, so at the time
of writing Discord is reachable all day during the hours the schedule calls
IXL only. Nothing in the model can say "a little, then not".

Three of the existing structures come close and each fails in a different way.
A window budget (ADR-004) meters a category across the whole window and is
refused on a restricted window, and "five minutes per hour" is per clock hour
and per thing. `disallowed` removes Discord outright, which is the old rule and
not the new one. Category is a description of what a pattern is, and here one
`productive` pattern needs a rule the other `productive` patterns do not.

The two patterns also have to share the five minutes. "Web or otherwise"
means one ration, spent by whichever of them is in front of him, so the rule
cannot hang off either pattern alone.

## Decision

A restricted window may carry **allowances**: named rations of minutes per
clock hour. A pattern draws on an allowance by carrying its name. While the
owner's window is restricted, a pattern carrying an allowance is admitted until
the hour's ration is spent, then closed, then withheld until the hour turns.

```sql
ALTER TABLE schedule_windows ADD COLUMN allowances TEXT NOT NULL DEFAULT '{}';
    -- JSON {name: minutes per hour}
ALTER TABLE process_patterns ADD COLUMN allowance TEXT;
    -- the allowance this pattern draws on, or NULL

CREATE TABLE allowance_activity (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user      TEXT NOT NULL,
    allowance TEXT NOT NULL,
    date      TEXT NOT NULL,
    hour      INTEGER NOT NULL,
    seconds   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user, allowance, date, hour)
);
```

In the spec grammar an allowance is a trailing `name=minutes` token on a
restricted clause:

```
playtimed windows set anders 'mon-fri 0-16 restricted discord=5; ...'
playtimed patterns allowance 86 discord     # the desktop app
playtimed patterns allowance 13 discord     # discord.com
```

### The window grants, the pattern draws

The split follows ADR-004: a pattern says *what* something is, and a window
says *when and how much*. An allowance granted on the weekday windows and not
the weekend ones is exactly the "weekday policy" as stated, and a global
ration on the pattern would have followed Discord into every restricted hour
of the week, which is the same error ADR-004 removed from per-day budgets.

A name rather than a pattern id is what lets two patterns share one ration.
It also survives a pattern being deleted and rediscovered, which on brick
happens.

### A pattern that carries an allowance is rationed everywhere it is restricted

Carrying an allowance changes what `active` means for that pattern inside a
restricted window. Where the window grants the name, the pattern gets the
ration; where it does not, the pattern is shut out like anything else the
window does not admit. The alternative — falling back to unlimited `productive`
admission in a window that grants nothing — would make forgetting a token on
one clause silently reopen Discord for that window, and the failure that
reopens the lockdown is the one to design out.

Open windows are untouched. Everything an allowance could grant is already
admitted there, and `parse_spec` refuses the token on an open clause.

### Presence is what is metered

Every poll in which anything carrying the allowance is present — a process at
any CPU, or a tab — charges one poll interval to `(user, allowance, date,
hour)`, once, however many processes and tabs that is. Discord is half a dozen
Electron processes and possibly a tab; charging per process would spend five
minutes in under one.

Presence rather than CPU because presence is what enforcement removes. A
window budget can afford the CPU gate because a game either runs hot or is not
being played; a chat window idles at two percent while being used.

### The ration renews with the clock

Spend is keyed on the clock hour, so at the top of every hour it reads zero
without a reset. This is the same reasoning ADR-004 gave for deriving window
consumption from `hourly_activity`: a counter with a reset is a second copy of
the clock, and a second way to be wrong.

### Two enforcement paths, one state

The process path warns when the ration is reached, waits the strict-mode
grace period, and closes every process carrying the allowance. A relaunch in
the same hour is closed on sight, with a notification saying when it comes
back, so the grace period is not a renewable thirty seconds.

The browser path goes through ADR-003. A domain whose allowance is spent, or
not granted in a restricted window, is **withheld**: moved from the allowlist
to the blocklist in the generated policy. The daemon recomputes the withheld
set every poll and rewrites the policy only when it changes, which happens
twice an hour at most — when the ration runs out, and when the hour turns.

## Consequences

**The weekday policy is expressible and inspectable.** `playtimed windows show`
names the ration on the windows that grant it, and `playtimed status` reports
what is left of it this hour.

**A spent web ration blocks navigation, not an open tab.** Managed policy
applies at navigation time (ADR-003). A `discord.com` tab that was open when
the ration ran out stays open until the browser reloads it. The desktop
application, which is where nearly all of brick's Discord time goes, is closed
outright. The notification says the minutes are up, and the audit log records
it; the tab itself is beyond what a policy file can reach.

**The last poll is generous.** Spend is detected at poll granularity and the
close follows a grace period, so five minutes is nearer six in practice. The
error is in the child's favour and is bounded by two poll intervals.

**Machine-wide policy meets per-user allowances the way it met per-user
modes.** A `discord.com` row with no owner is withheld if any monitored user
would withhold it. Two children on one machine share the tighter ration, which
is the ADR-004 rule applied one level down.

**Autostart burns the ration.** Discord set to open at login during a
restricted hour spends its five minutes in the tray and is then closed. This
is the policy working as stated, and the fix — turn autostart off — is his.

**A new intention joins the router.** `allowance_expired` and
`allowance_blocked` have templates and fallbacks. Hosts seeded before this
release get the templates through migration, which now inserts defaults for
any intention that has none rather than only when the table is empty.

**`blocked_allowance` events carry the audit trail.** Every close made under
this rule is logged with the allowance name and whether it was spent or
ungranted, alongside the `terminated` event the kill path already writes.

## Alternatives Considered

**A per-pattern ration, no window involvement.** `playtimed patterns leash 86
5`, meaning five minutes per hour in any restricted window. One command and no
grammar change. Rejected because it applies to every restricted hour of the
week where the requirement names weekdays, and because two patterns cannot
share it.

**Model it as a window budget.** Permit budgets on restricted windows and let
`meters` name a pattern. Rejected because a budget spans the window and this
ration spans the hour, and because a `meters` value that is sometimes a
category and sometimes a pattern name overloads a field that already carries
enough.

**Meter by CPU like everything else.** Consistent with `add_runtime`.
Rejected because Discord in use sits under the threshold, so the five minutes
would be five minutes of voice calls and unlimited text.

**Close the browser when the web ration is spent.** The only way to end an
open tab. Rejected for the reason ADR-003 gave: it costs every permitted tab,
IXL included, to close one, during the hours IXL is the point.

**Notify and do nothing.** Cheap, and in keeping with the personality.
Rejected because the situation that produced the requirement is the one where
advisory limits have already been tried.
