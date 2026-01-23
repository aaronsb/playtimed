# playtimed Example Code

These examples were developed during the 2026-01-22 session while designing
the browser domain tracking feature (ADR-001).

## Files

### browser_domain_detection.py
Demonstrates how to detect browser windows and extract domains on KDE Wayland
using D-Bus.

**Key discovery:** KWin exposes window titles via:
- Service: `org.kde.KWin`
- Path: `/WindowsRunner`
- Interface: `org.kde.krunner1.Match('')`

This works from the daemon (running as root) by connecting to the user's
session bus at `/run/user/<uid>/bus`.

### chrome_history_query.py
Demonstrates querying Chrome's History SQLite database as a fallback for
domain detection when window titles don't match known signatures.

**Note:** Chrome locks its History DB while running. Must copy the file first.

## Current State (2026-01-22)

### What's Working
- v0.2.2 deployed to brick via AUR
- Process monitoring and discovery working
- Notifications reach Anders' desktop via `runuser` + `notify-send`
- 65 tests passing

### What's Designed (ADR-001)
- Browser domain tracking via window titles
- Same discovery workflow as processes
- Categories: gaming, educational, social, ignored
- Site signature lookup table for title → domain mapping

### Next Steps
1. Implement `src/playtimed/browser.py` module
2. Add `pattern_type` column to database
3. Hook browser monitor into main scan loop
4. CLI display for browser domains in `discover list`

## Testing Commands

```bash
# Test window detection on brick
ssh aaron@brick "sudo -u anders python3 /path/to/browser_domain_detection.py"

# Query Chrome history
ssh aaron@brick "sudo cp /home/anders/.config/google-chrome/Default/History /tmp/h.db && sqlite3 /tmp/h.db 'SELECT url, title FROM urls ORDER BY last_visit_time DESC LIMIT 10'"

# Check current playtimed status
ssh aaron@brick "sudo playtimed status && sudo playtimed discover list"
```
