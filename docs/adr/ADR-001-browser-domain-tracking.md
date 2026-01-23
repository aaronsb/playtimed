# ADR-001: Browser Domain Tracking

Status: Proposed
Date: 2026-01-22
Deciders: @aaron, @claude

## Context

playtimed monitors process activity to track gaming time. However, modern browsers represent a "computer within a computer" - a single `chrome` process can host hundreds of different activities:

- Educational sites (IXL - homeschool LMS)
- Browser games (coolmathgames, poki, etc.)
- Social/entertainment (Discord, YouTube)
- Random browsing

The current system discovers "chrome" as a single process with 10+ hours of runtime, which is not actionable. We need visibility into *which sites* accumulate time, using the same discovery/categorization workflow as processes.

**Specific problem:** Anders uses IXL for homeschool, but "tab flips" away quickly, never accumulating the focused time needed for actual learning. We want to track educational site time separately from gaming/entertainment.

**Design constraint:** This should not become surveillance. We track *domains* (where time goes), not *content* (what he says/does). The goal is accountability and habit visibility, not monitoring conversations.

## Decision

Extend the existing pattern discovery system to treat browser domains as a parallel entity type alongside processes.

### Data Model

Extend `process_patterns` table:

```sql
ALTER TABLE process_patterns ADD COLUMN pattern_type TEXT DEFAULT 'process';
-- Values: 'process', 'browser_domain'

ALTER TABLE process_patterns ADD COLUMN browser TEXT;
-- Values: 'chrome', 'chromium', 'firefox', NULL (for processes)
```

A browser domain pattern looks like:
```
id: 15
pattern: "ixl.com"
pattern_type: "browser_domain"
browser: "chrome"
name: "IXL"
category: "educational"
owner: "anders"
monitor_state: "active"
```

### Detection Mechanism

#### KDE Wayland (brick's environment)

Window titles available via KWin's D-Bus interface:

```bash
# Query all windows via KRunner interface
qdbus6 --literal org.kde.KWin /WindowsRunner org.kde.krunner1.Match ''
```

Returns structured data including window titles:
```
"(3) Discord | #𝖦𝖾𝗇𝖾𝗋𝖺𝗅💬 | ✨The Utopia✨ - Google Chrome"
"YouTube Music - Google Chrome"
```

**D-Bus details:**
- Service: `org.kde.KWin`
- Path: `/WindowsRunner`
- Interface: `org.kde.krunner1`
- Method: `Match('')` - empty string returns all windows
- Requires user's session bus: `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus`

**Python implementation:** Use `dbus-python` to call this interface directly, avoiding subprocess overhead.

#### Chrome title pattern

Chrome window titles follow: `<page title> - Google Chrome`

Examples:
- `(3) Discord | #general | Server - Google Chrome` → discord.com
- `YouTube Music - Google Chrome` → youtube.com
- `IXL | Dashboard - Google Chrome` → ixl.com

#### Domain extraction

1. **Title parsing** - Extract domain from window title using heuristics:
   - Known site signatures: "Discord |" → discord.com, "YouTube" → youtube.com
   - Title suffix patterns: "| Site Name"
2. **Fallback** - Query Chrome's History DB (copy to avoid lock) for recent URLs if title parsing fails

**Site signature lookup table:**
```python
SITE_SIGNATURES = {
    'Discord': 'discord.com',
    'YouTube Music': 'music.youtube.com',
    'YouTube': 'youtube.com',
    'IXL': 'ixl.com',
    'Google Search': 'google.com',
    # Add as discovered
}
```

### Time Tracking

Same logic as processes:
- Each poll cycle, enumerate open browser tabs (via window titles)
- For each domain detected, record it was "seen"
- Accumulate runtime for domains that persist across polls
- Apply same discovery thresholds:
  - `sample_window_seconds`: Domain must be seen within this window
  - `min_samples`: Domain must be seen N times to be discovered

### Discovery Flow

```
Poll cycle:
  1. Detect chrome/chromium/firefox running for user
  2. Get window titles for browser windows
  3. Extract domains from titles
  4. For each domain:
     - If known pattern (active/ignored): track time, skip discovery
     - If unknown: add to discovery candidates
     - If threshold met: create discovered pattern

CLI output:
  $ playtimed discover list

  👀 Discovered (awaiting review)

  ID   Type            Owner   Name                 Runtime   Last Seen
  ────────────────────────────────────────────────────────────────────
  7    process         anders  chrome               10h51m    22:35
  15   browser:chrome  anders  discord.com          3h20m     22:35
  16   browser:chrome  anders  ixl.com              45m       22:08
  17   browser:chrome  anders  coolmathgames.com    2h15m     21:30
```

### Categorization

Same workflow as processes:
```bash
# Educational - tracked but doesn't count against gaming
playtimed discover promote 16 educational

# Gaming - counts against gaming limit
playtimed discover promote 17 gaming

# Ignore - not tracked (e.g., discord, youtube if you don't care)
playtimed discover ignore 15

# Disallow - browser navigating here triggers warning (stretch goal)
playtimed discover disallow 18
```

### Categories

| Category | Counts against gaming? | Counts against total? | Example |
|----------|------------------------|----------------------|---------|
| gaming | Yes | Yes | coolmathgames.com |
| educational | No | No | ixl.com |
| social | No | Yes (optional) | discord.com |
| ignored | No | No | google.com |

### Module Structure

```
src/playtimed/
  browser.py          # NEW: Browser monitoring module
    - get_browser_windows(user) -> list[WindowInfo]
    - extract_domain(title) -> Optional[str]
    - BrowserMonitor class

  main.py             # Extend _scan_all_processes to call browser monitor
  db.py               # Add pattern_type column, browser column
```

## Consequences

### Positive

- Reuses existing discovery/categorization workflow - no new concepts to learn
- Gives visibility into browser time breakdown without surveillance
- Educational time can be tracked and encouraged
- Browser games count against gaming limit
- Database stays clean - only significant domains get patterns
- Same threshold logic prevents noise from quick visits

### Negative

- Window title parsing is heuristic - some sites may be misidentified
- Requires xdotool/kdotool dependency for window enumeration
- Cannot detect incognito/private windows (by design - no History DB access)
- Multiple browser windows/profiles add complexity

### Neutral

- "chrome" process pattern becomes less useful once domain tracking works (can be ignored)
- May need site-specific title parsing rules for tricky cases
- Future: could extend to Firefox, other browsers with same pattern

## Alternatives Considered

### 1. Chrome Extension

Install a browser extension that reports active tab to playtimed via local socket/file.

**Rejected because:**
- More invasive (requires installing extension)
- Anders could disable it
- Requires maintaining extension code
- Different approach than process monitoring

### 2. Full History Tracking

Query Chrome's History DB periodically, track all visited URLs.

**Rejected because:**
- Creates surveillance concern (every URL logged)
- Database bloat (hundreds of domains)
- Doesn't map to "time spent" well (visit count ≠ duration)
- History includes incognito if synced, privacy issue

### 3. Network-level Monitoring

Use DNS logs or proxy to track domain access.

**Rejected because:**
- Heavy infrastructure requirement
- Affects whole network, not just Anders
- Overkill for the problem
- Privacy/trust concerns

### 4. Separate Browser Time Budget

Just track total browser time, don't break down by domain.

**Rejected because:**
- Doesn't solve the IXL problem (can't distinguish educational from gaming)
- "chrome 10h" is not actionable information
- Misses browser games counting as gaming

## Implementation Plan

1. **Phase 1: Window title extraction**
   - Implement `get_browser_windows()` using kdotool/xdotool
   - Test on brick with Anders' Chrome session
   - Validate domain extraction accuracy

2. **Phase 2: Database schema**
   - Add `pattern_type`, `browser` columns
   - Migrate existing patterns (all become `pattern_type='process'`)
   - Update CLI display to show type

3. **Phase 3: Integration**
   - Hook browser monitor into main scan loop
   - Apply discovery thresholds
   - Track runtime per domain

4. **Phase 4: Categorization**
   - Add `educational` category
   - Update time accounting (educational doesn't count against limits)
   - CLI commands for promoting browser domains

## Open Questions

1. ~~**Wayland vs X11** - brick runs KDE Wayland. Need to verify kdotool works or find alternative.~~
   **RESOLVED:** KWin D-Bus interface works on Wayland. Use `org.kde.KWin /WindowsRunner` with `org.kde.krunner1.Match('')`.

2. **Multiple windows** - If same domain is open in 3 tabs, count time once or 3x? (Probably once - deduplicate per poll cycle)

3. **Domain normalization** - `www.youtube.com` vs `youtube.com` vs `music.youtube.com`? Probably normalize to base domain, with option to track subdomains separately.

4. **Private/Incognito** - Window titles still visible, but no History DB. Track what we can see, accept blind spots.

5. **Site signature maintenance** - How to handle unknown sites? Options:
   - Fall back to History DB lookup
   - Log unknown title for manual mapping
   - Use generic "unknown" domain until mapped
