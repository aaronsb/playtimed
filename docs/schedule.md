# Schedule Management

Per-hour, per-day control over when gaming is allowed. The schedule is a 168-slot grid (7 days x 24 hours) where each slot is either allowed or blocked.

## View Schedules

Show all users' schedules (or a specific user):

```
$ playtimed schedule
━━━ anders ━━━
  Gaming limit: 120 min/day

       00  01  02  03  04  05  06  07  08  09  10  11  12  13  14  15  16  17  18  19  20  21  22  23
       ┌───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┐
  Mon  │░░░│░░░│░░░│░░░│░░░│░░░│░░░│░░░│░░░│░░░│░░░│░░░│░░░│░░░│░░░│░░░│▓▓▓│▓▓▓│▓▓▓│▓▓▓│▓▓▓│▓▓▓│░░░│░░░│
       ├───┼───┼───┼───┼───┼───┼───┼───┼───┼───┼───┼───┼───┼───┼───┼───┼───┼───┼───┼───┼───┼───┼───┼───┤
  ...
  Fri  │░░░│░░░│░░░│░░░│░░░│░░░│░░░│░░░│░░░│░░░│░░░│░░░│░░░│░░░│░░░│░░░│▓▓▓│▓▓▓│▓▓▓│▓▓▓│▓▓▓│▓▓▓│░░░│░░░│
       ╞═══╪═══╪═══╪═══╪═══╪═══╪═══╪═══╪═══╪═══╪═══╪═══╪═══╪═══╪═══╪═══╪═══╪═══╪═══╪═══╪═══╪═══╪═══╪═══╡
  Sat  ║░░░║░░░║░░░║░░░║░░░║░░░║░░░║░░░║░░░║▓▓▓║▓▓▓║▓▓▓║▓▓▓║▓▓▓║▓▓▓║▓▓▓║▓▓▓║▓▓▓║▓▓▓║▓▓▓║▓▓▓║▓▓▓║▓▓▓║░░░║
       ╠═══╬═══╬═══╬═══╬═══╬═══╬═══╬═══╬═══╬═══╬═══╬═══╬═══╬═══╬═══╬═══╬═══╬═══╬═══╬═══╬═══╬═══╬═══╬═══╣
  Sun  ║░░░║░░░║░░░║░░░║░░░║░░░║░░░║░░░║░░░║▓▓▓║▓▓▓║▓▓▓║▓▓▓║▓▓▓║▓▓▓║▓▓▓║▓▓▓║▓▓▓║▓▓▓║▓▓▓║▓▓▓║▓▓▓║▓▓▓║░░░║
       ╚═══╩═══╩═══╩═══╩═══╩═══╩═══╩═══╩═══╩═══╩═══╩═══╩═══╩═══╩═══╩═══╩═══╩═══╩═══╩═══╩═══╩═══╩═══╩═══╝

  ▓▓▓ allowed  ░░░ blocked
```

- `▓▓▓` = gaming allowed (green in terminal)
- `░░░` = gaming blocked
- Single-line borders for weekdays, double-line for weekends

```
playtimed schedule              # all users
playtimed schedule anders       # specific user
```

## Set Schedule (CLI)

Batch-edit slots with range syntax:

```
playtimed schedule set <user> <spec>[,<spec>...]
```

Spec format: `<days> <hours> <+|->` where `..` creates ranges.

```bash
# Single slot
playtimed schedule set anders mon 16 +

# Weekday range
playtimed schedule set anders mon..fri 16..21 +

# Weekend range
playtimed schedule set anders sat..sun 09..22 +

# Combined (comma-separated)
playtimed schedule set anders mon..fri 16..21 +,sat..sun 09..22 +

# Clear everything
playtimed schedule set anders mon..sun all -
```

Output confirms changes and shows the updated grid:

```
$ playtimed schedule set anders mon..fri 16..21 +,sat..sun 09..22 +
Updated 58 slots.

       00  01  02  ...
       ┌───┬───┬───┬─── ...
  Mon  │░░░│░░░│░░░│ ...
```

## Interactive Editor

A curses-based TUI for painting schedules visually:

```
playtimed schedule edit anders
```

### Controls

| Key | Action |
|-----|--------|
| Arrow keys | Navigate the grid |
| Enter | Toggle current cell (allowed/blocked) |
| Space | Cycle paint mode: off -> paint allow -> paint block |
| q | Save and quit |
| ESC | Cancel (discard changes) |

### Paint Mode

Space cycles through three modes shown in the status line:

1. **Single toggle** (default) -- Enter toggles individual cells, arrows just move
2. **PAINT ALLOW** -- arrow keys fill cells as allowed (▓) as you move
3. **PAINT BLOCK** -- arrow keys fill cells as blocked (░) as you move

This makes it fast to fill large regions by entering paint mode and sweeping with arrows.

## Export / Import

Round-trip schedules as JSON for backup, transfer between machines, or version control.

### Export

```bash
playtimed schedule export            # all users
playtimed schedule export anders     # specific user
```

```json
{
  "anders": {
    "schedule": "000000000000000011111100...111111111111110",
    "gaming_limit": 120,
    "daily_total": 180
  }
}
```

The `schedule` field is a 168-character string of `0` (blocked) and `1` (allowed), ordered as Monday hour 0-23, Tuesday hour 0-23, ..., Sunday hour 0-23. Index formula: `(day * 24) + hour` where day 0 = Monday.

`gaming_limit` and `daily_total` are included as metadata context but are not used on import.

### Import

```bash
playtimed schedule export > schedules.json
# edit schedules.json...
playtimed schedule import schedules.json
```

Validation checks before writing:
- Valid JSON with username keys
- Each entry has a `schedule` key
- Schedule is exactly 168 characters of `0`/`1`
- User exists in the database

All checks must pass before any schedule is written.

## Data Model

The schedule is stored as a `schedule` TEXT column in the `user_limits` table. When the column is NULL (pre-migration), the schedule is auto-generated from the legacy `weekday_start`/`weekday_end`/`weekend_start`/`weekend_end` columns.

The daemon reads the schedule in `_is_allowed_time()`:

```python
schedule = self.db.get_schedule(user)
idx = (now.weekday() * 24) + now.hour
if schedule[idx] == '1':
    return True
```
