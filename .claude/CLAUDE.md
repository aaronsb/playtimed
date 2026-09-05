# playtimed - Project Context for Claude Code

## Project Status

**Phase: MVP Complete** - Core daemon functional, ready for deployment testing.

### What's Built
- [x] Process monitoring daemon with CPU-based activity detection
- [x] SQLite database for metrics, patterns, and config
- [x] Three enforcement modes: `normal`, `passthrough`, `strict` (allowlist only)
- [x] Process discovery workflow — promote, ignore, disallow
- [x] Window schedules — hours tile the week, each window carrying a mode
      and an optional budget scoped to that window (ADR-004)
- [x] Browser domain tracking for Chrome and Firefox via window titles (ADR-001)
- [x] Browser managed-policy generation — the browser enforces URL rules from
      the domains in the database (ADR-003)
- [x] Session end detection — `natural`, `enforced`, `logout`, `orphaned`
- [x] KDE notification backend with Claude personality
- [x] CLI for status, schedules, users, patterns, discovery, policy, maintenance
- [x] Automatic DB retention (30 days events, 90 days sessions, forever summaries)
- [x] Packaged for Arch: AUR and the `[aaronsb]` repo, published by arch-repo

### What's Not Built Yet
- [ ] Clippy frontend (KDE Plasma widget idea)
- [ ] Web dashboard for parent monitoring
- [ ] Login-time greeting notification
- [ ] Decomposing `main.py`, still a ~2900-line monolith (ADR-002, proposed)
- [ ] A curses editor for windows — the old grid editor was removed with
      the grid it edited, and `windows set` takes a spec string instead

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
/usr/bin/playtimed        # CLI and daemon entry point (package)
/usr/bin/playtimed-notify # Notification helper

/etc/playtimed/
  config.yaml             # Basic daemon config (poll interval, paths)

/var/lib/playtimed/
  playtimed.db            # SQLite database (patterns, limits, events, sessions)

/etc/opt/chrome/policies/managed/
  playtimed.json          # Generated URL rules — playtimed owns this file only
/etc/firefox/policies/
  policies.json           # playtimed owns the policies.WebsiteFilter key

/opt/playtimed/           # Only when installed from source via scripts/install.sh
```

## Installation

```bash
# From the [aaronsb] repo or the AUR
pacman -S playtimed        # or: yay -S playtimed

# Configure
playtimed user add anders
playtimed windows preset anders school

# Start
systemctl enable --now playtimed
```

`scripts/install.sh` still exists and installs an isolated venv under
`/opt/playtimed` instead. It is the from-source path; the package is what
brick runs.

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

# Schedules are windows: '<days> <start>-<end> <mode>[:budget][/all]',
# semicolon-separated. Hours are 24-hour and the end is exclusive.
playtimed windows show anders
playtimed windows set anders 'mon-fri 0-15 restricted; mon-fri 15-18 open:60; \
                              mon-fri 18-22 open; mon-fri 22-24 restricted'

# Unlisted hours fill as restricted, and a set that does not tile is refused.
playtimed windows preset anders school
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

### Tagging needs a display or a primed agent

`tag.gpgsign` is true, so `git tag -a` signs, and signing needs a pinentry that
can reach the operator. A Claude Code session has no tty, so what decides the
outcome is whether a graphical pinentry can run:

| Session | Result |
|---|---|
| Local, `DISPLAY` or `WAYLAND_DISPLAY` set | Signs — pinentry prompts on the desktop |
| Over SSH, no display | Fails: `gpg: signing failed: Inappropriate ioctl for device` |

A `!` command does not rescue the SSH case; it runs in the same tty-less place.

`gpg-agent.conf` sets `default-cache-ttl 7776000`, so entering the passphrase
once caches it for 90 days and gpg stops launching pinentry at all. When
working over SSH, prime the agent from a shell that has a tty — suspend the
session with Ctrl+Z, run `echo priming | gpg --clearsign > /dev/null`, then
`fg` — and tagging works from inside the session until the cache expires.

Do not work around this by creating an unsigned tag. Every `v*` tag here is
signed, and a pushed tag is awkward to replace once arch-repo has read it.

### `PKGBUILD-git` is not published

This repository carries one, but no `playtimed-git` package exists on the AUR
and none is being created. Leave it unonboarded; adding it means adding an AUR
package to maintain, which is a decision rather than a tidy-up.

The full contract: https://github.com/aaronsb/arch-repo/blob/main/docs/packaging-contract.md
