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

## Releasing

`aaronsb/arch-repo` publishes this project. It reads `./PKGBUILD` from the
default branch, builds it in a clean container, lints with namcap, signs, and
pushes to the AUR (`playtimed`) and the `[aaronsb]` pacman repository.

```bash
make check                   # ruff, pytest, and the version this repo would release
make package                 # clean-chroot build + namcap; fails on a namcap error
make release                 # then tag, push, and cut the GitHub release
```

Nothing here talks to the AUR. There is no `aur` target and no publish script:
two writers to one AUR ref is how a PKGBUILD and its `.SRCINFO` drift apart.

### Fields arch-repo owns

It overwrites all four before publishing, so a value set here is only wrong
until it does. Do not maintain them, and do not commit a `.SRCINFO`.

| Field | Where it really comes from |
|---|---|
| `pkgver` | the newest published GitHub release |
| `pkgrel` | arch-repo's count of how many times it packaged that release |
| `sha256sums` | computed from the release artifact |
| `.SRCINFO` | regenerated at publish |

This project's own version lives in `pyproject.toml`, and `make version`
compares it against the tag.

### A packaging fix needs no release

Change the recipe on the default branch and push. arch-repo compares the
rendered recipe against what it last published and ships the difference as a
`pkgrel` bump — `0.5.3-1` becomes `0.5.3-2`, resetting to `-1` at the next real
release. Do not cut a version for a change to packaging alone.

### Check before you tag

`make package` builds the recipe in a clean chroot and runs namcap. It builds
from `HEAD` rather than the published archive, so it works before the release it
precedes, and it fails on a namcap error — namcap exits 0 whether or not it
found one.

### `PKGBUILD-git` is not published

This repository carries one, but no `playtimed-git` package exists on the AUR
and none is being created. Leave it unonboarded; adding it means adding an AUR
package to maintain, which is a decision rather than a tidy-up.

The full contract: https://github.com/aaronsb/arch-repo/blob/main/docs/packaging-contract.md
