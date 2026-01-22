# playtimed - Project Context for Claude Code

## Project Status

**Phase: MVP Complete** - Core daemon functional, ready for deployment testing.

### What's Built
- [x] Process monitoring daemon with CPU-based activity detection
- [x] SQLite database for metrics, patterns, and config
- [x] KDE notification backend with Claude personality
- [x] CLI for status, user management, pattern management, maintenance
- [x] Automatic DB retention (30 days events, 90 days sessions, forever summaries)
- [x] Install/uninstall scripts with isolated venv

### What's Not Built Yet
- [ ] Clippy frontend (KDE Plasma widget idea)
- [ ] Web dashboard for parent monitoring
- [ ] Login-time greeting notification
- [ ] Session end detection (game closed naturally vs killed)

## The Vibe

This is a dad project. Aaron is a sysadmin with 25+ years experience. His son Anders has a computer (hostname: `brick`) running Arch Linux with KDE and a Windows 95 theme. Anders plays too much Minecraft. Trust has been broken enough times that it's time for "you don't understand what it means when your dad is a sysadmin who also uses Claude Code daily" tough love.

This isn't covert surveillance. Anders knows the deal:
- Dad has root access (passwordless sudo)
- If Anders disables dad's account or removes sudo rights, the computer goes away entirely
- The rules are transparent and enforceable

## The Concept

Instead of boring parental control software, we're building something with personality. The daemon (`playtimed`) monitors and enforces limits, but it communicates as "Claude" - an AI that dad installed to live on the computer and help manage screen time.

**The killer feature idea:** Anders uses a Windows 95 theme. We could integrate **Clippy** (via clippy.js or a native Qt port) as the visual notification layer.

## Technical Stack

**Target machine (brick):**
- Arch Linux
- KDE Plasma with Windows 95 theme
- Minecraft Java edition

**Daemon:**
- Python 3.10+
- psutil for process monitoring
- SQLite for all persistent data
- Isolated venv at `/opt/playtimed`

**Files:**
```
/opt/playtimed/           # Installation
  venv/                 # Isolated Python environment
  src/                  # Source copy for debugging

/etc/playtimed/
  config.yaml           # Basic daemon config (poll interval, paths)

/var/lib/playtimed/
  playtimed.db            # SQLite database (patterns, limits, events, sessions)

/usr/local/bin/playtimed  # CLI wrapper
```

## Installation

```bash
# On brick (as root)
cd /path/to/playtimed
./scripts/install.sh

# Configure
playtimed user add anders --gaming-limit 120 --daily-total 180

# Start
systemctl enable --now playtimed
```

## CLI Reference

```bash
# User can check their own status
playtimed status

# Admin commands
playtimed user list
playtimed user add anders --gaming-limit 120 --weekday-start 16:00 --weekday-end 21:00
playtimed patterns list
playtimed patterns add "factorio" "Factorio" gaming --cpu-threshold 10
playtimed maintenance --events-days 14
```

## Personality Guidelines

The "Claude" personality should be:

1. **Friendly but firm** - Not a dictator, not a pushover
2. **Self-aware** - Knows it's an AI, knows dad installed it
3. **Helpful** - Offers to help with homework, suggests alternatives
4. **Slightly cheeky** - Has a sense of humor about the situation
5. **Not creepy** - Transparent about what it monitors

## Key Design Decisions

1. **SQLite over config files** - Patterns and user limits live in DB, not YAML. Easier to update without restart, prepares for future UI.

2. **CPU threshold for activity detection** - Steam can sit idle forever. Only processes using >X% CPU tick the clock.

3. **Launchers vs games** - Separate categories. Launchers detected but don't count time.

4. **Append-only events + daily summaries** - Events auto-purge after 30 days, but daily summaries kept forever for long-term trends.

5. **Isolated venv** - No system Python pollution. Clean install/uninstall.

## Side Quests

### KDE Plasma Widget Clippy

Port clippy.js to a native KDE Plasma widget:
- QML-based, uses AnimatedSprite for sprite sheets
- Receives messages from playtimed via D-Bus
- Lives on desktop or panel
- Peak integration with the W95 theme

## Remember

This is supposed to be fun (for dad at least). The goal is helping Anders develop better habits, not creating an adversarial surveillance state. Keep the personality warm, the enforcement fair, and the Clippy animations plentiful.

When Anders inevitably complains: "You can always come talk to me about adjusting the rules. Or you could touch grass. Either works."
