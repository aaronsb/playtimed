#!/usr/bin/env python3
"""
playtimed - Claude-powered screen time daemon

A parental control daemon with personality.
"""

import argparse
import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import psutil
import yaml

from .db import ActivityDB, get_connection
from .router import MessageRouter, MessageContext, get_router
from .browser import BrowserMonitor

# Default paths
DEFAULT_CONFIG = "/etc/playtimed/config.yaml"
DEFAULT_STATE_DIR = "/var/lib/playtimed"
DEFAULT_DB_PATH = "/var/lib/playtimed/playtimed.db"
USER_STATE_DIR = Path.home() / ".local/share/playtimed"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger("playtimed")


# Terminal colors (disabled if not a tty)
class Colors:
    """ANSI color codes for terminal output."""
    _enabled = sys.stdout.isatty()

    RESET = '\033[0m' if _enabled else ''
    BOLD = '\033[1m' if _enabled else ''
    DIM = '\033[2m' if _enabled else ''

    RED = '\033[31m' if _enabled else ''
    GREEN = '\033[32m' if _enabled else ''
    YELLOW = '\033[33m' if _enabled else ''
    BLUE = '\033[34m' if _enabled else ''
    MAGENTA = '\033[35m' if _enabled else ''
    CYAN = '\033[36m' if _enabled else ''
    WHITE = '\033[37m' if _enabled else ''

    # Semantic colors
    @classmethod
    def ok(cls, text): return f"{cls.GREEN}{text}{cls.RESET}"
    @classmethod
    def warn(cls, text): return f"{cls.YELLOW}{text}{cls.RESET}"
    @classmethod
    def error(cls, text): return f"{cls.RED}{text}{cls.RESET}"
    @classmethod
    def info(cls, text): return f"{cls.CYAN}{text}{cls.RESET}"
    @classmethod
    def dim(cls, text): return f"{cls.DIM}{text}{cls.RESET}"
    @classmethod
    def bold(cls, text): return f"{cls.BOLD}{text}{cls.RESET}"
    @classmethod
    def header(cls, text): return f"{cls.BOLD}{cls.CYAN}{text}{cls.RESET}"


def print_table(headers: list[str], rows: list[list[str]], col_widths: list[int] = None):
    """Print a formatted table with headers."""
    if not col_widths:
        col_widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0)) + 2
                      for i, h in enumerate(headers)]

    # Header
    header_line = ''.join(f"{Colors.bold(h):<{w}}" for h, w in zip(headers, col_widths))
    print(header_line)
    print(Colors.dim('─' * sum(col_widths)))

    # Rows
    for row in rows:
        print(''.join(f"{str(c):<{w}}" for c, w in zip(row, col_widths)))


@dataclass
class AppSession:
    """Tracks a single app usage session."""
    app: str
    start: str
    end: Optional[str] = None
    duration: int = 0  # seconds


@dataclass
class UserState:
    """Daily state for a monitored user."""
    date: str
    total_time: int = 0  # seconds
    gaming_time: int = 0  # seconds
    sessions: list = field(default_factory=list)
    warnings_sent: dict = field(default_factory=dict)
    last_updated: str = ""

    @classmethod
    def load(cls, path: Path) -> "UserState":
        """Load state from file, or create fresh if missing/stale."""
        today = date.today().isoformat()

        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                if data.get("date") == today:
                    return cls(**data)
            except (json.JSONDecodeError, TypeError) as e:
                log.warning(f"Corrupted state file, starting fresh: {e}")

        return cls(date=today)

    def save(self, path: Path):
        """Persist state to file."""
        self.last_updated = datetime.now().isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            json.dump(asdict(self), f, indent=2)


@dataclass
class ProcessMatch:
    """A matched monitored process."""
    pid: int
    name: str
    category: str  # 'gaming', 'launcher'
    cmdline: str
    cpu_percent: float = 0.0
    session_id: Optional[int] = None  # DB session tracking
    low_cpu_count: int = 0  # consecutive scans below CPU threshold (hysteresis)


class NotificationBackend:
    """Base class for notification backends."""

    def send(self, title: str, message: str, urgency: str = "normal"):
        raise NotImplementedError


class KDENotification(NotificationBackend):
    """KDE/freedesktop notifications via notify-send."""

    def __init__(self, user: str):
        self.user = user

    def send(self, title: str, message: str, urgency: str = "normal"):
        import subprocess

        # Get user's display environment
        env = self._get_user_env()
        if not env:
            log.warning(f"Could not get display env for {self.user}")
            return

        cmd = [
            "sudo", "-u", self.user,
            "notify-send",
            "--urgency", urgency,
            "--app-name", "Claude",
            "--icon", "dialog-information",
            title,
            message
        ]

        try:
            subprocess.run(cmd, env=env, timeout=5, capture_output=True)
            log.debug(f"Sent notification: {title}")
        except subprocess.TimeoutExpired:
            log.warning("Notification send timed out")
        except Exception as e:
            log.error(f"Failed to send notification: {e}")

    def _get_user_env(self) -> Optional[dict]:
        """Get environment variables needed for GUI from user's session."""
        env = os.environ.copy()

        # Find user's session
        for proc in psutil.process_iter(['pid', 'username', 'environ']):
            try:
                if proc.info['username'] == self.user:
                    penv = proc.info.get('environ') or {}
                    if 'DISPLAY' in penv or 'WAYLAND_DISPLAY' in penv:
                        env.update({
                            k: v for k, v in penv.items()
                            if k in ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR')
                        })
                        return env
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return env


class MessageTemplates:
    """Claude personality message templates."""

    GREET = [
        "Good {time_of_day}, {name}. I'm Claude - your dad asked me to help out around here. "
        "You have {total_remaining} of screen time today, including {gaming_remaining} for games. "
        "What are we doing today?",
    ]

    GAME_START = [
        "Ah, {app}! Good choice. Your gaming timer starts now. "
        "You have {gaming_remaining} of gaming time left. Have fun!",

        "{app}! Starting your timer. {gaming_remaining} remaining today. Enjoy!",
    ]

    WARNING_30 = [
        "Hey! 30 minutes of gaming time left for today. "
        "Might want to start thinking about a good stopping point.",
    ]

    WARNING_10 = [
        "10 minutes of gaming time left. I'll need to close {app} soon. "
        "Wrap up what you're doing!",
    ]

    WARNING_5 = [
        "5 minutes! {app} closes in 5 minutes. Seriously, save now.",
    ]

    WARNING_1 = [
        "One minute left. {app} is closing in 60 seconds. "
        "I really hope you saved.",
    ]

    TIME_UP = [
        "That's your gaming time for today. {app} will close in 30 seconds. "
        "You still have {total_remaining} of screen time for other stuff. "
        "Need help with homework? I'm actually pretty good at that.",
    ]

    BLOCKED = [
        "Nice try! Gaming time is done for today. "
        "See you tomorrow. Maybe go outside? I hear the graphics are incredible.",

        "Still no. Gaming's done for today. "
        "Your dad says hi, by the way.",
    ]

    KILLED = [
        "Time's up. {app} has been closed. "
        "You did good today - see you tomorrow!",
    ]

    @classmethod
    def get(cls, key: str, **kwargs) -> str:
        """Get a formatted message."""
        import random
        templates = getattr(cls, key.upper(), ["Message not found."])
        template = random.choice(templates)
        return template.format(**kwargs)


def require_root(command: str):
    """Exit with error if not running as root."""
    if os.geteuid() != 0:
        print(f"Error: '{command}' requires root privileges.", file=sys.stderr)
        print(f"Try: sudo playtimed {command}", file=sys.stderr)
        sys.exit(1)


def format_duration(seconds: int) -> str:
    """Format seconds as human-readable duration."""
    if seconds < 60:
        return f"{seconds} seconds"

    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"

    hours = minutes // 60
    mins = minutes % 60
    if mins == 0:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{hours}h {mins}m"


class ClaudeDaemon:
    """Main daemon class."""

    # Critical system processes to never kill (not games, would break the system)
    SYSTEM_PROCESSES = {
        'systemd', 'dbus-daemon', 'pipewire', 'pulseaudio', 'wireplumber',
        'kwin', 'kwin_wayland', 'kwin_x11', 'plasmashell', 'kded5', 'kded6',
        'Xorg', 'Xwayland', 'gnome-shell', 'mutter',
        'sddm', 'gdm', 'gdm-session', 'lightdm', 'login', 'agetty',
        'sudo', 'su', 'ssh', 'sshd', 'notify-send', 'dbus-launch',
        'polkitd', 'upowerd', 'thermald', 'acpid',
    }

    # Shell processes - not games
    SHELL_PROCESSES = {'bash', 'zsh', 'fish', 'sh', 'dash', 'csh', 'tcsh'}

    def __init__(self, config_path: str):
        self.config = self._load_config(config_path)
        self.running = True
        self.state: dict[str, UserState] = {}
        self.active_games: dict[str, dict[int, ProcessMatch]] = {}  # user -> {pid -> match}
        self.notifiers: dict[str, NotificationBackend] = {}

        # Discovery tracking: {(user, proc_name): {'samples': [...], 'first_seen': time}}
        self.discovery_candidates: dict[tuple[str, str], dict] = {}

        # Strict mode pending kills: {pid: {'name': str, 'warned_at': time, 'user': str}}
        self.strict_pending: dict[int, dict] = {}

        # Browser monitors per user: {user: BrowserMonitor}
        self.browser_monitors: dict[str, BrowserMonitor] = {}

        # Our own PID (never kill ourselves!)
        self.our_pid = os.getpid()

        # Initialize database
        db_path = self.config.get("daemon", {}).get("db_path", DEFAULT_DB_PATH)
        self.db = ActivityDB(db_path)
        log.info(f"Database initialized at {db_path}")

        # Initialize message router
        self.router = MessageRouter(self.db)

        # Load configs
        self.discovery_config = self.db.get_discovery_config()
        self.daemon_config = self.db.get_daemon_config()
        self.mode = self.daemon_config['mode']
        log.info(f"Daemon mode: {self.mode}")

        # Set up signal handlers
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGHUP, self._handle_reload)  # Reload config on SIGHUP

        # Monitored users (reloaded periodically)
        self.users: list[str] = []

    def _is_excluded_process(self, proc_name: str, cmdline: str, pid: int) -> bool:
        """Check if a process should never be monitored/killed."""
        # Never kill ourselves (by PID - unforgeable)
        if pid == self.our_pid:
            return True

        # Never kill our parent (the Python interpreter running us)
        try:
            if psutil.Process(pid).ppid() == self.our_pid:
                return True
        except psutil.NoSuchProcess:
            pass

        # System processes - these would break the system
        if proc_name in self.SYSTEM_PROCESSES:
            return True

        # Shell processes - not games
        if proc_name in self.SHELL_PROCESSES:
            return True

        # Check if it's ACTUALLY playtimed (not just named playtimed)
        # Must be Python running playtimed.main, not a renamed binary
        if 'playtimed' in proc_name.lower():
            # Verify it's really us: Python + playtimed.main in cmdline
            if 'python' in cmdline.lower() and 'playtimed.main' in cmdline:
                return True
            # If something is just named "playtimed" but isn't Python running our module,
            # it's suspicious - DO NOT exclude it (could be renamed game)
            log.warning(f"Process {proc_name} (PID {pid}) claims to be playtimed "
                        f"but doesn't look legitimate: {cmdline[:100]}")
            return False

        return False

    def _handle_reload(self, signum, frame):
        """Handle SIGHUP - reload all config."""
        log.info("Received SIGHUP, reloading configuration...")
        self._reload_config()

    def _reload_config(self):
        """Reload daemon config from database (mode, users, discovery settings)."""
        # Reload daemon mode
        old_mode = self.mode
        self.daemon_config = self.db.get_daemon_config()
        self.mode = self.daemon_config['mode']

        # Reload discovery config
        self.discovery_config = self.db.get_discovery_config()

        # Reload user list
        old_users = set(self.users)
        self.users = self.db.get_all_monitored_users()
        new_users = set(self.users)

        # Log user changes
        added = new_users - old_users
        removed = old_users - new_users
        if added:
            log.info(f"Added users: {', '.join(added)}")
        if removed:
            log.info(f"Removed users: {', '.join(removed)}")

        if old_mode != self.mode:
            log.info(f"Mode changed: {old_mode} -> {self.mode}")
            # Notify all users about mode change via router
            self.router.mode_change(self.mode)

    def _load_config(self, path: str) -> dict:
        """Load configuration from YAML file."""
        config_path = Path(path)
        if not config_path.exists():
            log.warning(f"Config not found at {path}, using defaults")
            return self._default_config()

        with open(config_path) as f:
            return yaml.safe_load(f)

    def _default_config(self) -> dict:
        """Return default configuration."""
        return {
            "daemon": {
                "poll_interval": 30,
                "state_dir": str(USER_STATE_DIR),
                "reset_hour": 4,
            },
            "users": {},
            "processes": {
                "gaming": [
                    {"pattern": r"java.*minecraft", "name": "Minecraft"},
                    {"pattern": r"minecraft-launcher", "name": "Minecraft"},
                    {"pattern": r"steam", "name": "Steam"},
                    {"pattern": r"\.exe$", "name": "Windows Game"},
                ]
            }
        }

    def _handle_signal(self, signum, frame):
        """Handle shutdown signals."""
        log.info(f"Received signal {signum}, shutting down...")
        self.running = False

    def _get_state_path(self, user: str) -> Path:
        """Get state file path for user."""
        state_dir = Path(self.config["daemon"].get("state_dir", DEFAULT_STATE_DIR))
        return state_dir / f"{user}.json"

    def _load_user_state(self, user: str) -> UserState:
        """Load or create state for user."""
        if user not in self.state:
            path = self._get_state_path(user)
            self.state[user] = UserState.load(path)
        return self.state[user]

    def _save_user_state(self, user: str):
        """Save state for user."""
        if user in self.state:
            path = self._get_state_path(user)
            self.state[user].save(path)

    def _get_notifier(self, user: str) -> NotificationBackend:
        """Get notification backend for user."""
        if user not in self.notifiers:
            self.notifiers[user] = KDENotification(user)
        return self.notifiers[user]

    def _get_user_uid(self, user: str) -> Optional[int]:
        """Get UID for a username."""
        import pwd
        try:
            return pwd.getpwnam(user).pw_uid
        except KeyError:
            log.warning(f"User {user} not found in passwd")
            return None

    def _get_browser_monitor(self, user: str) -> Optional[BrowserMonitor]:
        """Get or create browser monitor for user."""
        if user not in self.browser_monitors:
            uid = self._get_user_uid(user)
            if uid is None:
                return None
            self.browser_monitors[user] = BrowserMonitor(self.db, user, uid)
        return self.browser_monitors[user]

    def _match_process_to_pattern(self, proc_name: str, cmdline: str,
                                     patterns: list[dict]) -> Optional[dict]:
        """Try to match a process against a list of patterns."""
        for pdef in patterns:
            pattern = pdef.get("pattern", "")
            try:
                if re.search(pattern, cmdline, re.IGNORECASE) or \
                   re.search(pattern, proc_name, re.IGNORECASE):
                    return pdef
            except re.error:
                log.warning(f"Invalid regex pattern: {pattern}")
        return None

    def _find_gaming_processes(self, user: str) -> list[ProcessMatch]:
        """Find running processes matching active gaming patterns.

        Uses hysteresis: CPU threshold gates initial detection, but once a
        game PID is tracked it stays active until the process actually exits.
        This prevents flickering when games idle briefly between CPU bursts.
        """
        matches = []
        prev_games = self.active_games.get(user, {})

        # Get active patterns from database
        launcher_patterns = self.db.get_patterns(category="launcher", owner=user)
        gaming_patterns = self.db.get_patterns(category="gaming", owner=user)

        for proc in psutil.process_iter(['pid', 'name', 'username', 'cmdline']):
            try:
                if proc.info['username'] != user:
                    continue

                cmdline = ' '.join(proc.info.get('cmdline') or [])
                proc_name = proc.info.get('name', '')

                # Skip launchers
                if self._match_process_to_pattern(proc_name, cmdline, launcher_patterns):
                    continue

                # Check gaming patterns
                pdef = self._match_process_to_pattern(proc_name, cmdline, gaming_patterns)
                if pdef:
                    pid = proc.info['pid']
                    already_tracked = pid in prev_games

                    try:
                        cpu = proc.cpu_percent(interval=0.1)
                    except psutil.NoSuchProcess:
                        continue

                    cpu_threshold = pdef.get("cpu_threshold", 5.0)
                    above_threshold = cpu >= cpu_threshold

                    # Hysteresis: once tracked, stay tracked for a cooldown
                    # period (3 scans ~90s) to prevent flicker exploits
                    if above_threshold or already_tracked:
                        match = ProcessMatch(
                            pid=pid,
                            name=pdef.get("name", proc_name),
                            category="gaming",
                            cmdline=cmdline[:100],
                            cpu_percent=cpu
                        )
                        # Preserve session_id from previous tracking
                        if already_tracked:
                            prev = prev_games[pid]
                            if prev.session_id:
                                match.session_id = prev.session_id
                            # Track consecutive low-CPU scans
                            if above_threshold:
                                match.low_cpu_count = 0
                            else:
                                match.low_cpu_count = prev.low_cpu_count + 1
                                if match.low_cpu_count >= 3:
                                    # Cooldown expired — drop this PID
                                    continue
                        matches.append(match)

                        # Track stats for this pattern
                        self.db.record_pid_seen(pdef['id'], pid)

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return matches

    def _scan_all_processes(self, user: str):
        """Scan all processes for a user, handling all pattern states and discovery."""
        poll_interval = self.config["daemon"].get("poll_interval", 30)
        grace_seconds = self.daemon_config.get('strict_grace_seconds', 30)

        # Get ALL patterns (all states) for matching
        all_patterns = self.db.get_patterns(enabled_only=True, include_all_states=True, owner=user)

        # Track which PIDs are still running (for strict mode cleanup)
        seen_pids = set()

        for proc in psutil.process_iter(['pid', 'name', 'username', 'cmdline']):
            try:
                if proc.info['username'] != user:
                    continue

                cmdline = ' '.join(proc.info.get('cmdline') or [])
                proc_name = proc.info.get('name', '')
                pid = proc.info['pid']

                # Skip excluded processes (ourselves, system processes)
                if self._is_excluded_process(proc_name, cmdline, pid):
                    continue

                seen_pids.add(pid)

                try:
                    cpu = proc.cpu_percent(interval=0.1)
                except psutil.NoSuchProcess:
                    continue

                # Try to match against known patterns
                matched_pattern = self._match_process_to_pattern(proc_name, cmdline, all_patterns)

                if matched_pattern:
                    state = matched_pattern.get('monitor_state', 'active')
                    pattern_id = matched_pattern['id']

                    # Record stats for ANY matched pattern
                    self.db.record_pid_seen(pattern_id, pid)
                    if cpu >= matched_pattern.get('cpu_threshold', 5.0):
                        self.db.add_runtime(pattern_id, poll_interval)

                    # Auto-discover specific games from catchall patterns (.exe$)
                    if (matched_pattern.get('owner') is None and
                            matched_pattern.get('pattern') == r'\.exe$' and
                            proc_name.lower().endswith('.exe')):
                        self._discover_from_catchall(
                            user, proc_name, cmdline, pid,
                            matched_pattern)

                    # Handle disallowed processes (unless passthrough mode)
                    if state == 'disallowed' and self.mode != 'passthrough':
                        log.info(f"Killing disallowed process: {proc_name} (PID {pid})")
                        self._kill_process(ProcessMatch(
                            pid=pid, name=proc_name, category="disallowed",
                            cmdline=cmdline[:100], cpu_percent=cpu
                        ), user, notify=False)
                        self.router.blocked_launch(user, proc_name)

                    # Remove from strict pending if it's a known pattern (active/ignored)
                    if pid in self.strict_pending and state in ('active', 'ignored'):
                        del self.strict_pending[pid]

                else:
                    # No pattern match
                    if self.mode == 'strict' and cpu >= self.discovery_config.get('cpu_threshold', 25):
                        # Strict mode: unknown high-CPU process - warn then kill
                        self._handle_strict_unknown(user, proc_name, cmdline, pid, cpu, grace_seconds)
                    else:
                        # Normal/passthrough: check if this should be discovered
                        self._check_discovery(user, proc_name, cmdline, pid, cpu)

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Clean up strict_pending for processes that are no longer running
        dead_pids = [pid for pid in self.strict_pending if pid not in seen_pids]
        for pid in dead_pids:
            del self.strict_pending[pid]

        # Browser domain scanning
        browser_monitor = self._get_browser_monitor(user)
        if browser_monitor:
            try:
                browser_domains = browser_monitor.scan()
                for domain, info in browser_domains.items():
                    pattern = info.get('pattern')
                    if pattern:
                        # Track runtime for all browser domains (like process patterns)
                        self.db.add_runtime(pattern['id'], poll_interval)

                        # Notify about newly discovered domains
                        if info.get('is_new'):
                            self.router.discovery(user, domain)
            except Exception as e:
                log.debug(f"Browser scan failed for {user}: {e}")

    def _handle_strict_unknown(self, user: str, proc_name: str, cmdline: str,
                                pid: int, cpu: float, grace_seconds: int):
        """Handle unknown processes in strict mode - warn then kill after grace period."""
        now = time.time()

        if pid not in self.strict_pending:
            # First time seeing this - warn and start countdown
            self.strict_pending[pid] = {
                'name': proc_name,
                'warned_at': now,
                'user': user,
                'cmdline': cmdline,
            }
            log.warning(f"[STRICT] Unknown process {proc_name} (PID {pid}) - warning sent, "
                        f"will terminate in {grace_seconds}s")
            # Send warning via router (uses 'blocked_launch' intention for unknown apps)
            ctx = MessageContext(user=user, process=proc_name, grace_seconds=grace_seconds)
            self.router.send('strict_warning', ctx)
            # Also discover it so admin can review
            self._check_discovery(user, proc_name, cmdline, pid, cpu)
        else:
            # Already warned - check if grace period expired
            warned_at = self.strict_pending[pid]['warned_at']
            elapsed = now - warned_at

            if elapsed >= grace_seconds:
                log.info(f"[STRICT] Grace period expired for {proc_name} (PID {pid}) - terminating")
                self._kill_process(ProcessMatch(
                    pid=pid, name=proc_name, category="unknown",
                    cmdline=cmdline[:100], cpu_percent=cpu
                ), user, notify=False)
                self.router.enforcement(user, proc_name)
                del self.strict_pending[pid]

    def _check_discovery(self, user: str, proc_name: str, cmdline: str, pid: int, cpu: float):
        """Check if an unmatched process should be flagged for discovery."""
        if not self.discovery_config.get('enabled', True):
            return

        cpu_threshold = self.discovery_config.get('cpu_threshold', 25)
        sample_window = self.discovery_config.get('sample_window_seconds', 30)
        min_samples = self.discovery_config.get('min_samples', 3)

        if cpu < cpu_threshold:
            return

        key = (user, proc_name)
        now = time.time()

        if key not in self.discovery_candidates:
            self.discovery_candidates[key] = {
                'samples': [],
                'first_seen': now,
                'cmdline': cmdline,
                'pid': pid
            }

        candidate = self.discovery_candidates[key]
        candidate['samples'].append({'time': now, 'cpu': cpu})

        # Remove old samples outside the window
        candidate['samples'] = [
            s for s in candidate['samples']
            if now - s['time'] <= sample_window
        ]

        # Check if we have enough samples to flag
        if len(candidate['samples']) >= min_samples:
            # Check if already in database
            existing = self.db.get_pattern_by_name_and_owner(proc_name, user)
            if existing:
                # Already known (maybe ignored), just update stats
                self.db.record_pid_seen(existing['id'], pid)
                del self.discovery_candidates[key]
                return

            # New discovery!
            avg_cpu = sum(s['cpu'] for s in candidate['samples']) / len(candidate['samples'])
            log.info(f"Discovered new process: {proc_name} (avg CPU: {avg_cpu:.1f}%) for {user}")

            # Create pattern using process name as the regex
            pattern_id = self.db.discover_pattern(
                pattern=re.escape(proc_name),
                name=proc_name,
                owner=user,
                cmdline=cmdline[:200],
                cpu_threshold=5.0
            )

            # Record the PID
            self.db.record_pid_seen(pattern_id, pid)

            # Notify the user via router
            self.router.discovery(user, proc_name)

            # Clean up
            del self.discovery_candidates[key]

    def _discover_from_catchall(self, user: str, proc_name: str, cmdline: str,
                                pid: int, catchall_pattern: dict):
        """Auto-discover a specific game from a catchall pattern like .exe$.

        Similar to browser domain detection — the catchall is the "container"
        and individual exe names are discovered within it.
        """
        # Check if we already have a specific pattern for this exe
        existing = self.db.get_pattern_by_name_and_owner(proc_name, user)
        if existing:
            return

        # Clean up display name: "FalloutNV.exe" -> "FalloutNV"
        display_name = proc_name
        if display_name.lower().endswith('.exe'):
            display_name = display_name[:-4]

        # Create a specific pattern that matches this exact exe
        pattern_regex = re.escape(proc_name)
        category = catchall_pattern.get('category', 'gaming')
        cpu_threshold = catchall_pattern.get('cpu_threshold', 10.0)

        pattern_id = self.db.discover_pattern(
            pattern=pattern_regex,
            name=display_name,
            owner=user,
            cmdline=cmdline[:200],
            cpu_threshold=cpu_threshold,
            category=category,
            state='active',
        )

        self.db.record_pid_seen(pattern_id, pid)
        log.info(f"Auto-discovered Proton game: {display_name} ({proc_name}) for {user}")

    def _is_allowed_time(self, user: str) -> tuple[bool, str]:
        """Check if current time is within allowed hours (from schedule)."""
        schedule = self.db.get_schedule(user)
        now = datetime.now()
        idx = (now.weekday() * 24) + now.hour

        if schedule[idx] == '1':
            return True, ""

        return False, f"Gaming is not allowed at this time ({now.strftime('%a %H:00')})"

    def _get_remaining_time(self, user: str) -> tuple[int, int]:
        """Get remaining total and gaming time in seconds."""
        user_config = self.config.get("users", {}).get(user, {})
        limits = user_config.get("limits", {})

        daily_total = limits.get("daily_total", 180) * 60  # to seconds
        gaming_limit = limits.get("gaming", 120) * 60

        state = self._load_user_state(user)

        total_remaining = max(0, daily_total - state.total_time)
        gaming_remaining = max(0, gaming_limit - state.gaming_time)

        return total_remaining, gaming_remaining

    def _send_warning_if_needed(self, user: str, gaming_remaining: int, app: str):
        """Send warning notifications based on remaining time."""
        state = self._load_user_state(user)
        notifier = self._get_notifier(user)

        warnings = state.warnings_sent.setdefault("gaming", [])
        remaining_mins = gaming_remaining // 60

        warning_thresholds = [(30, "WARNING_30"), (10, "WARNING_10"),
                             (5, "WARNING_5"), (1, "WARNING_1")]

        for threshold, msg_key in warning_thresholds:
            if remaining_mins <= threshold and threshold not in warnings:
                message = MessageTemplates.get(msg_key, app=app)
                notifier.send("⏰ Time Check", message,
                            urgency="critical" if threshold <= 5 else "normal")
                warnings.append(threshold)
                log.info(f"Sent {threshold}min warning to {user}")
                break

    def _kill_process(self, proc: ProcessMatch, user: str, notify: bool = True,
                       reason: str = "KILLED"):
        """Terminate a process and its children gracefully, then forcefully.

        In passthrough mode, this is a no-op (just logs).
        """
        # Passthrough mode - don't actually kill anything
        if self.mode == 'passthrough':
            log.info(f"[PASSTHROUGH] Would kill {proc.name} (PID {proc.pid}) but mode is passthrough")
            return

        notifier = self._get_notifier(user)

        try:
            p = psutil.Process(proc.pid)

            # Get children BEFORE killing parent (they might get orphaned)
            children = []
            try:
                children = p.children(recursive=True)
            except psutil.NoSuchProcess:
                pass

            # SIGTERM to main process first
            log.info(f"Sending SIGTERM to {proc.name} (PID {proc.pid})")
            p.terminate()

            # Also terminate children
            for child in children:
                try:
                    if not self._is_excluded_process(child.name(), ' '.join(child.cmdline() or []), child.pid):
                        log.info(f"Sending SIGTERM to child {child.name()} (PID {child.pid})")
                        child.terminate()
                except psutil.NoSuchProcess:
                    pass

            # Wait for graceful exit
            try:
                p.wait(timeout=10)
                log.info(f"{proc.name} exited gracefully")
            except psutil.TimeoutExpired:
                # SIGKILL the main process
                log.info(f"Sending SIGKILL to {proc.name} (PID {proc.pid})")
                try:
                    p.kill()
                except psutil.NoSuchProcess:
                    pass

            # SIGKILL any remaining children
            for child in children:
                try:
                    if child.is_running():
                        log.info(f"Sending SIGKILL to child {child.name()} (PID {child.pid})")
                        child.kill()
                except psutil.NoSuchProcess:
                    pass

            # Log the termination event
            self.db.log_event(user, "terminated", app=proc.name,
                              pid=proc.pid, details=reason)

            if notify:
                notifier.send("🎮 Time's Up",
                             MessageTemplates.get(reason, app=proc.name))

        except psutil.NoSuchProcess:
            log.debug(f"Process {proc.pid} already gone")
        except psutil.AccessDenied:
            log.error(f"Access denied killing PID {proc.pid}")

    def _process_user(self, user: str):
        """Process monitoring for a single user using state machine approach."""
        # Check if user is enabled in DB
        limits = self.db.get_user_limits(user)
        if not limits or not limits.get('enabled', 1):
            return

        poll_interval = self.config["daemon"].get("poll_interval", 30)
        now = datetime.now()
        now_iso = now.isoformat()

        # Run full process scan (discovery, stats, disallowed termination)
        self._scan_all_processes(user)

        # Load state from database (or create if new day)
        db_state = self.db.get_user_state(user)
        was_gaming_active = db_state['gaming_active'] if db_state else 0
        last_poll_at = db_state.get('last_poll_at') if db_state else None

        # Find gaming processes
        current_games = self._find_gaming_processes(user)
        prev_games = self.active_games.get(user, {})
        gaming_active = 1 if current_games else 0

        # Track which PIDs we're seeing
        current_pids = {g.pid for g in current_games}
        prev_pids = set(prev_games.keys())

        # Calculate elapsed time using timestamps (not poll interval)
        elapsed_seconds = 0
        if last_poll_at and was_gaming_active:
            try:
                last_poll_time = datetime.fromisoformat(last_poll_at)
                elapsed_seconds = (now - last_poll_time).total_seconds()
                # Cap elapsed time to handle suspend/resume
                max_elapsed = poll_interval * 2
                if elapsed_seconds > max_elapsed:
                    log.debug(f"Large gap detected ({elapsed_seconds:.0f}s), capping at {max_elapsed}s")
                    elapsed_seconds = max_elapsed
            except (ValueError, TypeError):
                elapsed_seconds = poll_interval

        # Get current time usage
        total_used, gaming_used = self.db.get_time_used_today(user)

        # Add elapsed time if was gaming
        if elapsed_seconds > 0 and was_gaming_active:
            gaming_used += int(elapsed_seconds)
            total_used += int(elapsed_seconds)

        # Calculate remaining time (per-day limit)
        today_limits = self.db.get_daily_limits(user)
        gaming_limit = today_limits[datetime.now().weekday()] * 60  # seconds
        gaming_remaining = max(0, gaming_limit - gaming_used)
        gaming_remaining_mins = gaming_remaining // 60

        # Check time restrictions
        allowed, outside_reason = self._is_allowed_time(user)

        # Get warning flags
        warned_30 = db_state.get('warned_30', 0) if db_state else 0
        warned_15 = db_state.get('warned_15', 0) if db_state else 0
        warned_5 = db_state.get('warned_5', 0) if db_state else 0

        # Track kills this cycle for daily summary
        kills_this_cycle = 0

        # Process new game starts
        for game in current_games:
            if game.pid not in prev_pids:
                log.info(f"Detected new game: {game.name} (PID {game.pid}) for {user}")

                # Log to database
                self.db.log_event(user, "game_detected", app=game.name,
                                  category="gaming", pid=game.pid)

                if not allowed:
                    self.router.outside_hours(user, limits.get('weekday_start', ''),
                                              limits.get('weekday_end', ''))
                    self.db.log_event(user, "blocked_schedule", app=game.name,
                                      details=outside_reason, pid=game.pid)
                    self._kill_process(game, user, notify=False)
                    kills_this_cycle += 1
                    continue

                if gaming_remaining <= 0:
                    self.router.blocked_launch(user, game.name)
                    self.db.log_event(user, "blocked_quota", app=game.name, pid=game.pid)
                    self._kill_process(game, user, notify=False)
                    kills_this_cycle += 1
                    continue

                # Allowed - start session tracking
                session_id = self.db.start_session(user, game.name, "gaming", game.pid)
                game.session_id = session_id
                self.db.increment_session_count(user)
                self.db.log_event(user, "game_start", app=game.name, pid=game.pid)

                # Send notification via router
                self.router.process_started(user, game.name, gaming_remaining_mins)

        # Detect ended games and close their sessions
        ended_pids = prev_pids - current_pids
        for ended_pid in ended_pids:
            ended_game = prev_games[ended_pid]
            if ended_game.session_id:
                self.db.end_session(session_id=ended_game.session_id, reason="natural")
                log.info(f"Session ended (natural): {ended_game.name} (PID {ended_pid}) for {user}")
            self.db.log_event(user, "game_end", app=ended_game.name, pid=ended_pid)

        # Update active games
        self.active_games[user] = {g.pid: g for g in current_games}

        # Send warnings if gaming (flags prevent duplicates)
        if gaming_active and gaming_remaining > 0:
            if gaming_remaining_mins <= 30 and not warned_30:
                self.router.time_warning(user, 30, limits.get('gaming_limit', 120))
                warned_30 = 1

            if gaming_remaining_mins <= 15 and not warned_15:
                self.router.time_warning(user, 15, limits.get('gaming_limit', 120))
                warned_15 = 1

            if gaming_remaining_mins <= 5 and not warned_5:
                self.router.time_warning(user, 5, limits.get('gaming_limit', 120))
                warned_5 = 1

        # Enforce time limit
        if gaming_active and gaming_remaining <= 0:
            self.router.time_expired(user, limits.get('gaming_limit', 120))

            # Grace period (in-line for now, could be state-based)
            grace_seconds = self.daemon_config.get('strict_grace_seconds', 30)
            self.router.grace_period(user, grace_seconds)
            time.sleep(grace_seconds)

            for game in current_games:
                if game.session_id:
                    self.db.end_session(session_id=game.session_id, reason="enforced")
                    log.info(f"Session ended (enforced): {game.name} (PID {game.pid}) for {user}")
                self._kill_process(game, user, notify=False)
                self.router.enforcement(user, game.name)
                kills_this_cycle += 1

        # Update database state
        self.db.update_daily_summary(user,
                                      gaming_seconds=int(elapsed_seconds) if was_gaming_active else 0,
                                      total_seconds=int(elapsed_seconds) if was_gaming_active else 0,
                                      enforcements=kills_this_cycle)
        self.db.update_hourly_activity(user,
                                        gaming_seconds=int(elapsed_seconds) if was_gaming_active else 0,
                                        total_seconds=int(elapsed_seconds) if was_gaming_active else 0)

        self.db.update_user_state(user,
                                   gaming_active=gaming_active,
                                   gaming_time=gaming_used,
                                   last_poll_at=now_iso,
                                   warned_30=warned_30,
                                   warned_15=warned_15,
                                   warned_5=warned_5)

    def run(self):
        """Main daemon loop."""
        log.info("playtimed starting up")

        # Seed default patterns if database is empty
        self.db.seed_default_patterns()

        # Run maintenance on startup
        log.info("Running database maintenance...")
        maint = self.db.maintenance(events_days=30, sessions_days=90)
        self.db.cleanup_seen_pids(days=7)  # Clean old PID records
        log.info(f"Maintenance complete: deleted {maint['deleted']}, "
                 f"DB size: {maint['after']['file_size_mb']:.2f} MB")

        poll_interval = self.config["daemon"].get("poll_interval", 30)

        # Initial user load
        self.users = self.db.get_all_monitored_users()
        if not self.users:
            self.users = list(self.config.get("users", {}).keys())

        if not self.users:
            log.warning("No users configured, nothing to monitor")
        else:
            log.info(f"Monitoring users: {', '.join(self.users)}")

        loop_count = 0
        while self.running:
            # Reload config every 10 loops (mode, users, discovery settings)
            loop_count += 1
            if loop_count % 10 == 0:
                self._reload_config()

            for user in self.users:
                try:
                    self._process_user(user)
                except Exception as e:
                    log.error(f"Error processing user {user}: {e}", exc_info=True)

            time.sleep(poll_interval)

        # Save all state on exit
        for user in self.users:
            self._save_user_state(user)

        log.info("playtimed shutdown complete")


def _get_user_status_row(db, user: str) -> dict:
    """Get status data for a single user."""
    total_used, gaming_used = db.get_time_used_today(user)

    limits = db.get_user_limits(user)
    today_limits = db.get_daily_limits(user)
    gaming_limit = today_limits[datetime.now().weekday()] * 60
    total_limit = (limits['daily_total'] * 60) if limits else 180 * 60

    gaming_remaining = max(0, gaming_limit - gaming_used)
    total_remaining = max(0, total_limit - total_used)

    # Calculate percentage used
    gaming_pct = int((gaming_used / gaming_limit * 100)) if gaming_limit else 0
    total_pct = int((total_used / total_limit * 100)) if total_limit else 0

    return {
        'user': user,
        'gaming_used': format_duration(gaming_used),
        'gaming_remaining': format_duration(gaming_remaining),
        'gaming_pct': gaming_pct,
        'total_used': format_duration(total_used),
        'total_remaining': format_duration(total_remaining),
        'total_pct': total_pct,
    }


def _progress_bar(pct: int, width: int = 10) -> str:
    """Create a colored progress bar."""
    filled = int(width * pct / 100)
    empty = width - filled

    # Color based on usage
    if pct >= 90:
        color = Colors.RED
    elif pct >= 70:
        color = Colors.YELLOW
    else:
        color = Colors.GREEN

    bar = f"{color}{'█' * filled}{Colors.RESET}{Colors.DIM}{'░' * empty}{Colors.RESET}"
    return f"[{bar}]"


def cmd_status(args):
    """Show status for user(s)."""
    user = getattr(args, 'user', None)
    db_path = getattr(args, 'db', DEFAULT_DB_PATH)

    try:
        db = ActivityDB(db_path)
    except Exception:
        print(f"Error: Cannot access database at {db_path}", file=sys.stderr)
        print("Try: sudo playtimed status", file=sys.stderr)
        sys.exit(1)

    if user:
        users = [user]
    else:
        users = db.get_all_monitored_users()
        if not users:
            print("No monitored users configured.")
            print(f"Add users with: {Colors.info('sudo playtimed user add <username>')}")
            return

    print(Colors.header(f"📊 Screen Time Status") + f" - {date.today().isoformat()}")
    print()
    print(f"{Colors.bold('User'):<20} {Colors.bold('Gaming'):<12} {'Progress':<14} {Colors.bold('Total'):<12} {'Progress':<14}")
    print(Colors.dim("─" * 70))

    for u in users:
        row = _get_user_status_row(db, u)
        gaming_bar = _progress_bar(row['gaming_pct'])
        total_bar = _progress_bar(row['total_pct'])

        # Color percentage based on usage
        g_pct = row['gaming_pct']
        t_pct = row['total_pct']
        g_pct_str = Colors.error(f"{g_pct:>3}%") if g_pct >= 90 else Colors.warn(f"{g_pct:>3}%") if g_pct >= 70 else f"{g_pct:>3}%"
        t_pct_str = Colors.error(f"{t_pct:>3}%") if t_pct >= 90 else Colors.warn(f"{t_pct:>3}%") if t_pct >= 70 else f"{t_pct:>3}%"

        print(f"{Colors.bold(row['user']):<20} {row['gaming_used']:<12} {gaming_bar} {g_pct_str}  {row['total_used']:<12} {total_bar} {t_pct_str}")

    print()
    print(Colors.dim("Remaining:"))
    for u in users:
        row = _get_user_status_row(db, u)
        print(f"  {row['user']}: Gaming {Colors.ok(row['gaming_remaining'])}, Total {Colors.ok(row['total_remaining'])}")


def cmd_mode(args):
    """View or set daemon mode."""
    try:
        db = ActivityDB(args.db)
    except Exception:
        print(f"Error: Cannot access database at {args.db}", file=sys.stderr)
        print("Try: sudo playtimed mode", file=sys.stderr)
        sys.exit(1)

    if args.set_mode:
        require_root(f"mode {args.set_mode}")
        try:
            db.set_daemon_mode(args.set_mode)
            print(f"Mode set to: {Colors.bold(args.set_mode)}")
            print(Colors.dim("Daemon will pick up change within ~5 minutes, or restart it."))
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        config = db.get_daemon_config()
        mode = config['mode']
        grace = config['strict_grace_seconds']

        mode_descriptions = {
            'normal': 'Monitor and enforce limits for known gaming patterns',
            'passthrough': 'Monitor only - no enforcement, no blocking',
            'strict': f'Whitelist only - unknown apps terminated after {grace}s warning',
        }

        print(Colors.header("Daemon Mode"))
        print()

        for m, desc in mode_descriptions.items():
            if m == mode:
                print(f"  {Colors.ok('●')} {Colors.bold(m):<14} {desc}")
            else:
                print(f"  {Colors.dim('○')} {m:<14} {Colors.dim(desc)}")

        print()
        print(f"Set mode: {Colors.info('sudo playtimed mode <normal|passthrough|strict>')}")


def cmd_maintenance(args):
    """Run database maintenance."""
    require_root("maintenance")

    db = ActivityDB(args.db)

    print("Running maintenance...")
    result = db.maintenance(
        events_days=args.events_days,
        sessions_days=args.sessions_days
    )

    print(f"\nBefore:")
    print(f"  Size: {result['before']['file_size_mb']:.2f} MB")
    print(f"  Events: {result['before']['events_count']}")
    print(f"  Sessions: {result['before']['sessions_count']}")

    print(f"\nDeleted:")
    for table, count in result['deleted'].items():
        print(f"  {table}: {count} rows")

    print(f"\nAfter:")
    print(f"  Size: {result['after']['file_size_mb']:.2f} MB")
    print(f"  Events: {result['after']['events_count']}")
    print(f"  Sessions: {result['after']['sessions_count']}")


def cmd_history(args):
    """Show daily screen time history."""
    db_path = getattr(args, 'db', DEFAULT_DB_PATH)
    try:
        db = ActivityDB(db_path)
    except Exception:
        print(f"Error: Cannot access database at {db_path}", file=sys.stderr)
        sys.exit(1)

    user = getattr(args, 'user', None)
    days = getattr(args, 'days', 7) or 7

    if user:
        users = [user]
    else:
        users = db.get_all_monitored_users()
        if not users:
            print("No monitored users configured.")
            return

    for u in users:
        summaries = db.get_history(u, days)
        if not summaries:
            print(f"No history for {u}")
            continue

        limits = db.get_user_limits(u) or {}
        gaming_limit = limits.get('gaming_limit', 0)

        print(Colors.header(f"Screen Time History: {u}") + f" (last {days} days)")
        print()

        headers = ["Date", "Day", "Gaming", "Total", "Sessions", "Warns", "Kills"]
        rows = []
        for s in summaries:
            day_name = datetime.fromisoformat(s['date']).strftime("%a") if s.get('date') else ""
            gaming_mins = s.get('gaming_time', 0) // 60
            total_mins = s.get('total_time', 0) // 60

            # Color gaming time red if over limit
            gaming_str = format_duration(s.get('gaming_time', 0))
            if gaming_limit and gaming_mins > gaming_limit:
                gaming_str = Colors.error(gaming_str)
            elif gaming_limit and gaming_mins > gaming_limit * 0.8:
                gaming_str = Colors.warn(gaming_str)

            rows.append([
                s['date'],
                day_name,
                gaming_str,
                format_duration(s.get('total_time', 0)),
                str(s.get('session_count', 0)),
                str(s.get('warnings_sent', 0)),
                str(s.get('enforcements', 0)),
            ])

        print_table(headers, rows)
        print()


def cmd_audit(args):
    """Show process termination history."""
    db_path = getattr(args, 'db', DEFAULT_DB_PATH)
    try:
        db = ActivityDB(db_path)
    except Exception:
        print(f"Error: Cannot access database at {db_path}", file=sys.stderr)
        sys.exit(1)

    user = getattr(args, 'user', None)
    days = getattr(args, 'days', 30) or 30

    with get_connection(db.db_path) as conn:
        if user:
            rows = conn.execute("""
                SELECT timestamp, user, app, details, pid
                FROM events WHERE event_type = 'terminated'
                AND user = ?
                AND timestamp >= datetime('now', ?)
                ORDER BY timestamp DESC
            """, (user, f'-{days} days')).fetchall()
        else:
            rows = conn.execute("""
                SELECT timestamp, user, app, details, pid
                FROM events WHERE event_type = 'terminated'
                AND timestamp >= datetime('now', ?)
                ORDER BY timestamp DESC
            """, (f'-{days} days',)).fetchall()

    if not rows:
        print(f"No terminations in the last {days} days.")
        return

    print(Colors.header(f"Termination Audit") + f" (last {days} days)")
    print()

    headers = ["Time", "User", "App", "Reason", "PID"]
    table_rows = []
    for r in rows:
        ts = r['timestamp'][:16].replace('T', ' ')
        table_rows.append([
            ts,
            r['user'],
            r['app'] or '?',
            r['details'] or '?',
            str(r['pid'] or ''),
        ])

    print_table(headers, table_rows)
    print(f"\n  Total: {Colors.bold(str(len(rows)))} terminations")


def cmd_sessions(args):
    """Show individual game sessions."""
    db_path = getattr(args, 'db', DEFAULT_DB_PATH)
    try:
        db = ActivityDB(db_path)
    except Exception:
        print(f"Error: Cannot access database at {db_path}", file=sys.stderr)
        sys.exit(1)

    user = args.user
    if not user:
        users = db.get_all_monitored_users()
        user = users[0] if users else None
    if not user:
        print("No monitored users configured.")
        return

    day = getattr(args, 'date', None)
    days = getattr(args, 'days', None)

    if day:
        sessions = db.get_sessions_for_day(user, day)
        label = day
    elif days:
        sessions = db.get_sessions_range(user, days)
        label = f"last {days} days"
    else:
        sessions = db.get_sessions_for_day(user)
        label = "today"

    if not sessions:
        print(f"No sessions for {user} ({label})")
        return

    print(Colors.header(f"Sessions: {user}") + f" ({label})")
    print()

    headers = ["Date", "App", "Start", "Duration", "End"]
    rows = []
    for s in sessions:
        start = s.get('start_time', '')
        # Parse ISO timestamp to extract date and HH:MM
        try:
            st = datetime.fromisoformat(start)
            start_time = st.strftime("%H:%M")
            start_date = st.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            start_time = start
            start_date = ""

        duration = s.get('duration')
        dur_str = format_duration(duration) if duration else Colors.dim("running")

        reason = s.get('end_reason', '')
        reason_map = {'natural': Colors.ok('exit'), 'enforced': Colors.error('killed'),
                      'unknown': Colors.dim('?')}
        reason_str = reason_map.get(reason, Colors.dim(reason or '-'))

        rows.append([start_date, s.get('app', '?'), start_time, dur_str, reason_str])

    print_table(headers, rows)
    print()


def cmd_report(args):
    """Show weekly summary report."""
    db_path = getattr(args, 'db', DEFAULT_DB_PATH)
    try:
        db = ActivityDB(db_path)
    except Exception:
        print(f"Error: Cannot access database at {db_path}", file=sys.stderr)
        sys.exit(1)

    user = getattr(args, 'user', None)
    days = getattr(args, 'days', 7) or 7

    if user:
        users = [user]
    else:
        users = db.get_all_monitored_users()
        if not users:
            print("No monitored users configured.")
            return

    for u in users:
        summaries = db.get_history(u, days)
        top_apps = db.get_top_apps(u, days)

        if not summaries:
            print(f"No data for {u}")
            continue

        total_gaming = sum(s.get('gaming_time', 0) for s in summaries)
        total_screen = sum(s.get('total_time', 0) for s in summaries)
        total_sessions = sum(s.get('session_count', 0) for s in summaries)
        total_enforcements = sum(s.get('enforcements', 0) for s in summaries)
        active_days = len([s for s in summaries if s.get('gaming_time', 0) > 0])
        avg_gaming = total_gaming // active_days if active_days else 0

        print(Colors.header(f"Report: {u}") + f" (last {days} days)")
        print()
        print(f"  Total gaming:     {Colors.bold(format_duration(total_gaming))}")
        print(f"  Total screen:     {format_duration(total_screen)}")
        print(f"  Active days:      {active_days}/{len(summaries)}")
        print(f"  Avg gaming/day:   {format_duration(avg_gaming)}")
        print(f"  Sessions:         {total_sessions}")
        if total_enforcements:
            print(f"  Enforcements:     {Colors.error(str(total_enforcements))}")
        print()

        if top_apps:
            print(f"  {Colors.bold('Top Apps:')}")
            for app in top_apps:
                dur = format_duration(app['total_duration']) if app['total_duration'] else '?'
                print(f"    {app['app']:<25} {app['session_count']:>3} sessions  {dur:>10}")
            print()


def cmd_heatmap(args):
    """Show activity heatmap by day and hour."""
    db_path = getattr(args, 'db', DEFAULT_DB_PATH)
    try:
        db = ActivityDB(db_path)
    except Exception:
        print(f"Error: Cannot access database at {db_path}", file=sys.stderr)
        sys.exit(1)

    user = getattr(args, 'user', None)
    days = getattr(args, 'days', 7) or 7

    if user:
        users = [user]
    else:
        users = db.get_all_monitored_users()
        if not users:
            print("No monitored users configured.")
            return

    # Intensity blocks and colors
    def heat_cell(minutes):
        if minutes == 0:
            return Colors.dim(" · ")
        elif minutes <= 15:
            return Colors.GREEN + "░░░" + Colors.RESET
        elif minutes <= 30:
            return Colors.YELLOW + "▒▒▒" + Colors.RESET
        elif minutes <= 45:
            return Colors.RED + "▓▓▓" + Colors.RESET
        else:
            return Colors.RED + Colors.BOLD + "███" + Colors.RESET

    for u in users:
        hourly = db.get_hourly_activity(u, days)
        if not hourly:
            print(f"No hourly data for {u} (data starts collecting after upgrade)")
            continue

        # Build grid: {date_str: {hour: gaming_seconds}}
        grid = {}
        for row in hourly:
            d = row['date']
            if d not in grid:
                grid[d] = {}
            grid[d][row['hour']] = row['gaming_seconds']

        # Sort dates
        sorted_dates = sorted(grid.keys())
        day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

        print(Colors.header(f"Heatmap: {u}") + f" (last {days} days)")
        print()

        # Header row
        header = "       "
        for h in range(24):
            header += f"{h:02d}  "
        print(Colors.dim(header))

        # Top border
        print("       ┌" + "───┬" * 23 + "───┐")

        # Data rows
        for i, d in enumerate(sorted_dates):
            dt = date.fromisoformat(d)
            day_label = day_names[dt.weekday()]
            row_str = f"  {day_label}  │"
            for h in range(24):
                secs = grid[d].get(h, 0)
                mins = secs // 60
                row_str += heat_cell(mins) + "│"
            print(row_str)

            if i < len(sorted_dates) - 1:
                print("       ├" + "───┼" * 23 + "───┤")
            else:
                print("       └" + "───┴" * 23 + "───┘")

        print()
        print(f"  {Colors.dim(' · ')} 0m  "
              f"{Colors.GREEN}░░░{Colors.RESET} 1-15m  "
              f"{Colors.YELLOW}▒▒▒{Colors.RESET} 16-30m  "
              f"{Colors.RED}▓▓▓{Colors.RESET} 31-45m  "
              f"{Colors.RED}{Colors.BOLD}███{Colors.RESET} 46-60m")
        print()


def cmd_schedule(args):
    """Show schedule grid for a user."""
    db_path = getattr(args, 'db', DEFAULT_DB_PATH)
    try:
        db = ActivityDB(db_path)
    except Exception:
        print(f"Error: Cannot access database at {db_path}", file=sys.stderr)
        sys.exit(1)

    user = getattr(args, 'user', None)
    if user:
        users = [user]
    else:
        users = db.get_all_monitored_users()
        if not users:
            print("No monitored users configured.")
            return

    for i, u in enumerate(users):
        limits = db.get_user_limits(u)
        if not limits:
            print(f"No limits configured for {u}")
            continue

        if i > 0:
            print()

        schedule = db.get_schedule(u)

        daily_limits = db.get_daily_limits(u)

        print(Colors.header(f"━━━ {u} ━━━"))
        print()

        _print_schedule_grid(schedule, daily_limits)


def _print_schedule_grid(schedule: str, daily_limits: list[int] = None):
    """Print a 7×24 schedule grid with CP437 box-drawing characters.

    Renders the schedule as a bordered grid with shade characters and
    an optional daily limit column:

        ┌───┬───┬───┬─── ... ───┐
    Mon │░░░│░░░│▓▓▓│▓▓▓ ... ░░░│  120 min
        ├───┼───┼───┼─── ... ───┤
    Tue │░░░│░░░│▓▓▓│▓▓▓ ... ░░░│  120 min
        ...
        ╞═══╪═══╪═══╪═══ ... ═══╡
    Sat ║░░░║▓▓▓║▓▓▓║▓▓▓ ... ░░░║  180 min
        ╠═══╬═══╬═══╬═══ ... ═══╣
    Sun ║░░░║▓▓▓║▓▓▓║▓▓▓ ... ░░░║  180 min
        ╚═══╩═══╩═══╩═══ ... ═══╝

    Uses single-line borders for weekdays, double-line for weekends.
    ▓▓▓ (dark shade, green) = allowed, ░░░ (light shade, dim) = blocked.
    """
    day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

    # Header row with hours
    header = "       "
    for h in range(24):
        header += f"{h:02d}  "
    if daily_limits:
        header += " Limit"
    print(Colors.dim(header))

    # Top border
    print("       ┌" + "───┬" * 23 + "───┐")

    # Data rows
    for day_idx in range(7):
        is_weekend = day_idx >= 5
        label = day_names[day_idx]
        sep = "║" if is_weekend else "│"

        row_str = f"  {label}  {sep}"
        for h in range(24):
            idx = (day_idx * 24) + h
            if schedule[idx] == '1':
                row_str += Colors.GREEN + "▓▓▓" + Colors.RESET + sep
            else:
                row_str += Colors.dim("░░░") + sep

        if daily_limits:
            mins = daily_limits[day_idx]
            if mins >= 60:
                row_str += f"  {mins // 60}h{mins % 60:02d}m"
            else:
                row_str += f"  {mins}m"
        print(row_str)

        # Row separator
        if day_idx == 4:
            print("       ╞" + "═══╪" * 23 + "═══╡")
        elif day_idx == 5:
            print("       ╠" + "═══╬" * 23 + "═══╣")
        elif day_idx == 6:
            print("       ╚" + "═══╩" * 23 + "═══╝")
        else:
            print("       ├" + "───┼" * 23 + "───┤")

    print()
    print(f"  {Colors.GREEN}▓▓▓{Colors.RESET} allowed  {Colors.dim('░░░')} blocked")
    print()


def _parse_schedule_spec(spec: str) -> list[tuple[int, int, bool]]:
    """Parse a schedule spec into (day, hour, allowed) tuples.

    Spec format: '<days> <hours> <+|->'
    Days: mon, tue, ..., sun, or ranges: mon..fri
    Hours: 00-23, or ranges: 16..21, or 'all'
    Action: + (permit) or - (deny)

    Examples:
        'mon 16 +'           -> [(0, 16, True)]
        'mon..fri 16..21 +'  -> 5 days × 6 hours = 30 tuples
        'sat..sun all -'     -> 2 days × 24 hours = 48 tuples

    Returns list of (day_index, hour, allowed) tuples.
    """
    from playtimed.db import DAYS
    parts = spec.strip().split()
    if len(parts) != 3:
        raise ValueError(f"Invalid spec '{spec}': expected '<days> <hours> <+|->'")

    day_expr, hour_expr, action = parts

    # Parse action
    if action == '+':
        allowed = True
    elif action == '-':
        allowed = False
    else:
        raise ValueError(f"Invalid action '{action}': use + or -")

    # Parse days
    if '..' in day_expr:
        d_start, d_end = day_expr.lower().split('..')
        i_start = DAYS.index(d_start)
        i_end = DAYS.index(d_end)
        days = list(range(i_start, i_end + 1))
    else:
        days = [DAYS.index(day_expr.lower())]

    # Parse hours
    if hour_expr == 'all':
        hours = list(range(24))
    elif '..' in hour_expr:
        h_start, h_end = hour_expr.split('..')
        hours = list(range(int(h_start), int(h_end) + 1))
    else:
        hours = [int(hour_expr)]

    return [(d, h, allowed) for d in days for h in hours]


def cmd_schedule_set(args):
    """Set schedule slots from CLI specs.

    Examples:
        playtimed schedule set anders mon 16 +
        playtimed schedule set anders mon..fri 16..21 +,sat..sun 09..22 +
        playtimed schedule set anders mon..sun all -
    """
    db_path = getattr(args, 'db', DEFAULT_DB_PATH)
    try:
        db = ActivityDB(db_path)
    except Exception:
        print(f"Error: Cannot access database at {db_path}", file=sys.stderr)
        sys.exit(1)

    user = args.username
    limits = db.get_user_limits(user)
    if not limits:
        print(f"No user '{user}' configured.", file=sys.stderr)
        sys.exit(1)

    schedule = list(db.get_schedule(user))

    # Join remaining args and split on commas
    spec_str = ' '.join(args.spec)
    specs = [s.strip() for s in spec_str.split(',')]

    total_changes = 0
    for spec in specs:
        try:
            changes = _parse_schedule_spec(spec)
            for day, hour, allowed in changes:
                idx = (day * 24) + hour
                schedule[idx] = '1' if allowed else '0'
                total_changes += 1
        except (ValueError, IndexError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    db.set_schedule(user, ''.join(schedule))
    print(f"Updated {total_changes} slots.")
    print()
    _print_schedule_grid(''.join(schedule), db.get_daily_limits(user))


def cmd_schedule_edit(args):
    """Interactive curses-based schedule editor.

    Draws a 7×24 grid with box-drawing characters. Use arrow keys
    to move the cursor (shown as a blinking █ block), spacebar to
    cycle paint mode (off → paint allow → paint block), and q to
    save and quit. In paint mode, arrow keys fill cells as you move.

        ┌───┬───┬───┬─── ... ───┐
    Mon │░░░│ █ │▓▓▓│▓▓▓ ... ░░░│   <- cursor on hour 01
        ├───┼───┼───┼─── ... ───┤
        ...
    """
    import curses
    db_path = getattr(args, 'db', DEFAULT_DB_PATH)
    try:
        db = ActivityDB(db_path)
    except Exception:
        print(f"Error: Cannot access database at {db_path}", file=sys.stderr)
        sys.exit(1)

    user = args.username
    limits = db.get_user_limits(user)
    if not limits:
        print(f"No user '{user}' configured.", file=sys.stderr)
        sys.exit(1)

    schedule = list(db.get_schedule(user))
    daily_limits = db.get_daily_limits(user)
    day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

    def editor(stdscr):
        curses.curs_set(0)
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)   # allowed
        curses.init_pair(2, curses.COLOR_WHITE, -1)    # blocked (dim)
        curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_WHITE)  # cursor
        curses.init_pair(4, curses.COLOR_YELLOW, -1)   # limit highlight

        cur_day = 0
        cur_hour = 0  # 0-23 = schedule hours, 24 = limit column
        painting = None  # None = not painting, '1' = painting allowed, '0' = painting blocked
        limit_input = ""  # digit buffer when editing limit column

        while True:
            stdscr.clear()
            stdscr.addstr(0, 0, f"Schedule Editor: {user}", curses.A_BOLD)
            stdscr.addstr(0, 25, "Arrows:move  Enter:toggle  Space:paint  +/-:limit  q:save  Esc:cancel", curses.A_DIM)
            on_limit = (cur_hour == 24)

            # Header row
            header = "       "
            for h in range(24):
                header += f"{h:02d}  "
            header += "  Limit"
            stdscr.addstr(2, 0, header, curses.A_DIM)

            # Top border
            stdscr.addstr(3, 0, "       ┌" + "───┬" * 23 + "───┐")

            for day_idx in range(7):
                is_weekend = day_idx >= 5
                row_y = 4 + (day_idx * 2)
                sep = "║" if is_weekend else "│"

                stdscr.addstr(row_y, 0, f"  {day_names[day_idx]}  {sep}")
                for h in range(24):
                    idx = (day_idx * 24) + h
                    col_x = 7 + (h * 4) + 1  # after label and first sep
                    is_cursor = (day_idx == cur_day and h == cur_hour)

                    if is_cursor:
                        stdscr.addstr(row_y, col_x, " █ ", curses.color_pair(3) | curses.A_BLINK)
                    elif schedule[idx] == '1':
                        stdscr.addstr(row_y, col_x, "▓▓▓", curses.color_pair(1))
                    else:
                        stdscr.addstr(row_y, col_x, "░░░", curses.A_DIM)
                    stdscr.addstr(row_y, col_x + 3, sep)

                # Daily limit column
                limit_x = 7 + (24 * 4) + 2
                is_limit_cursor = (day_idx == cur_day and on_limit)
                if is_limit_cursor and limit_input:
                    limit_str = f" {limit_input:>4s}▏"
                elif is_limit_cursor:
                    mins = daily_limits[day_idx]
                    limit_str = f"[{mins:>4d}]"
                else:
                    mins = daily_limits[day_idx]
                    limit_str = f" {mins:>4d} "
                attr = curses.color_pair(4) | curses.A_BOLD if is_limit_cursor else curses.A_DIM
                stdscr.addstr(row_y, limit_x, limit_str, attr)

                # Row separator
                sep_y = row_y + 1
                if day_idx == 4:
                    stdscr.addstr(sep_y, 0, "       ╞" + "═══╪" * 23 + "═══╡")
                elif day_idx == 5:
                    stdscr.addstr(sep_y, 0, "       ╠" + "═══╬" * 23 + "═══╣")
                elif day_idx == 6:
                    stdscr.addstr(sep_y, 0, "       ╚" + "═══╩" * 23 + "═══╝")
                else:
                    stdscr.addstr(sep_y, 0, "       ├" + "───┼" * 23 + "───┤")

            # Legend
            info_y = 4 + (7 * 2) + 1
            if on_limit:
                mode_str = "  LIMIT: type minutes, ENTER confirm, +/- by 15"
            elif painting == '1':
                mode_str = "  MODE: PAINT ALLOW ▓"
            elif painting == '0':
                mode_str = "  MODE: PAINT BLOCK ░"
            else:
                mode_str = "  MODE: single toggle"
            stdscr.addstr(info_y, 0, "  ←↑↓→ navigate  ENTER toggle  SPACE paint mode  q save  ESC cancel")
            stdscr.addstr(info_y + 1, 0, mode_str)

            stdscr.refresh()

            key = stdscr.getch()
            if key == ord('q') and not limit_input:
                return True  # save
            elif key == 27:  # ESC
                if limit_input:
                    limit_input = ""  # cancel limit edit
                else:
                    return False  # cancel editor

            # Limit column: digit entry
            elif on_limit and chr(key).isdigit() if 0 <= key <= 255 else False:
                limit_input += chr(key)
                if len(limit_input) > 4:
                    limit_input = limit_input[-4:]
            elif on_limit and key in (curses.KEY_BACKSPACE, 127, 8):
                limit_input = limit_input[:-1]
            elif on_limit and key in (curses.KEY_ENTER, ord('\n'), ord('\r')):
                if limit_input:
                    val = int(limit_input)
                    daily_limits[cur_day] = min(1440, max(0, val))
                    limit_input = ""
            elif on_limit and (key == ord('+') or key == ord('=')):
                limit_input = ""
                daily_limits[cur_day] = min(1440, daily_limits[cur_day] + 15)
            elif on_limit and (key == ord('-') or key == ord('_')):
                limit_input = ""
                daily_limits[cur_day] = max(0, daily_limits[cur_day] - 15)

            # Schedule grid controls
            elif not on_limit and key in (curses.KEY_ENTER, ord('\n'), ord('\r')):
                idx = (cur_day * 24) + cur_hour
                schedule[idx] = '0' if schedule[idx] == '1' else '1'
            elif not on_limit and key == ord(' '):
                if painting is None:
                    painting = '1'
                    schedule[(cur_day * 24) + cur_hour] = '1'
                elif painting == '1':
                    painting = '0'
                    schedule[(cur_day * 24) + cur_hour] = '0'
                else:
                    painting = None

            # Navigation (both grid and limit)
            elif key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_LEFT, curses.KEY_RIGHT):
                # Commit any pending limit input on nav
                if limit_input:
                    val = int(limit_input)
                    daily_limits[cur_day] = min(1440, max(0, val))
                    limit_input = ""
                if key == curses.KEY_UP:
                    cur_day = (cur_day - 1) % 7
                elif key == curses.KEY_DOWN:
                    cur_day = (cur_day + 1) % 7
                elif key == curses.KEY_LEFT:
                    cur_hour = max(0, cur_hour - 1)
                elif key == curses.KEY_RIGHT:
                    cur_hour = min(24, cur_hour + 1)
                # Paint as we move if in a paint mode (grid only)
                if painting is not None and cur_hour < 24:
                    schedule[(cur_day * 24) + cur_hour] = painting
                # Exit paint mode when entering limit column
                if cur_hour == 24:
                    painting = None

    save = curses.wrapper(editor)
    if save:
        db.set_schedule(user, ''.join(schedule))
        db.set_daily_limits(user, daily_limits)
        print("Schedule saved.")
    else:
        print("Cancelled.")


def cmd_schedule_export(args):
    """Export schedules as JSON for backup or transfer."""
    db_path = getattr(args, 'db', DEFAULT_DB_PATH)
    try:
        db = ActivityDB(db_path)
    except Exception:
        print(f"Error: Cannot access database at {db_path}", file=sys.stderr)
        sys.exit(1)

    user = getattr(args, 'username', None)
    if user:
        users = [user]
    else:
        users = db.get_all_monitored_users()

    data = {}
    for u in users:
        limits = db.get_user_limits(u)
        if not limits:
            continue
        data[u] = {
            "schedule": db.get_schedule(u),
            "daily_limits": db.get_daily_limits(u),
        }

    print(json.dumps(data, indent=2))


def cmd_schedule_import(args):
    """Import schedules from JSON file with validation."""
    db_path = getattr(args, 'db', DEFAULT_DB_PATH)
    try:
        db = ActivityDB(db_path)
    except Exception:
        print(f"Error: Cannot access database at {db_path}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(args.file) as f:
            data = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        print(f"Error reading {args.file}: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, dict):
        print("Error: Expected JSON object with usernames as keys.", file=sys.stderr)
        sys.exit(1)

    errors = []
    for user, entry in data.items():
        if not isinstance(entry, dict) or 'schedule' not in entry:
            errors.append(f"  {user}: missing 'schedule' key")
            continue
        sched = entry['schedule']
        if len(sched) != 168:
            errors.append(f"  {user}: schedule length {len(sched)}, expected 168")
            continue
        if not all(c in '01' for c in sched):
            errors.append(f"  {user}: schedule contains invalid characters (expected only 0/1)")
            continue
        if 'daily_limits' in entry:
            dl = entry['daily_limits']
            if not isinstance(dl, list) or len(dl) != 7 or not all(isinstance(x, int) and x >= 0 for x in dl):
                errors.append(f"  {user}: daily_limits must be list of 7 non-negative integers")
                continue
        if not db.get_user_limits(user):
            errors.append(f"  {user}: user not found in database")

    if errors:
        print("Validation errors:", file=sys.stderr)
        for e in errors:
            print(e, file=sys.stderr)
        sys.exit(1)

    for user, entry in data.items():
        db.set_schedule(user, entry['schedule'])
        if 'daily_limits' in entry:
            db.set_daily_limits(user, entry['daily_limits'])
        print(f"Imported schedule for {user}")


def format_runtime(seconds: int) -> str:
    """Format runtime as human-readable."""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m"
    else:
        hours = seconds // 3600
        mins = (seconds % 3600) // 60
        return f"{hours}h{mins}m" if mins else f"{hours}h"


def cmd_patterns(args):
    """List or manage process patterns."""
    if args.action in ("add", "disable", "enable", "delete"):
        require_root(f"patterns {args.action}")
    if args.action == "note" and hasattr(args, 'text') and args.text:
        require_root("patterns note")

    try:
        db = ActivityDB(args.db)
    except Exception:
        print(f"Error: Cannot access database at {args.db}", file=sys.stderr)
        print(f"Try: sudo playtimed patterns {args.action}", file=sys.stderr)
        sys.exit(1)

    if args.action == "list":
        # Show all patterns with full state info
        patterns = db.get_all_patterns()
        if not patterns:
            print("No patterns configured.")
            return

        print(Colors.header("Patterns"))
        print()
        print(f"{Colors.bold('ID'):<6} {Colors.bold('Type'):<16} {Colors.bold('State'):<12} {Colors.bold('Category'):<12} {Colors.bold('Owner'):<10} {Colors.bold('Name'):<20} {Colors.bold('Runtime'):<10}")
        print(Colors.dim("─" * 95))

        state_colors = {
            'active': Colors.GREEN,
            'discovered': Colors.YELLOW,
            'ignored': Colors.DIM,
            'disallowed': Colors.RED,
        }

        for p in patterns:
            state = p.get('monitor_state', 'active')
            category = p.get('category') or '-'
            owner = p.get('owner') or '*'
            runtime = format_runtime(p.get('total_runtime_seconds', 0))
            enabled = "" if p['enabled'] else Colors.dim(" (off)")

            # Format pattern type
            pattern_type = p.get('pattern_type', 'process')
            browser = p.get('browser')
            if pattern_type == 'browser_domain' and browser:
                type_str = f"browser:{browser}"
            else:
                type_str = pattern_type

            state_color = state_colors.get(state, '')
            state_str = f"{state_color}{state}{Colors.RESET}"

            print(f"{p['id']:<6} {type_str:<16} {state_str:<12} {category:<12} {owner:<10} {p['name']:<20} {runtime:<10}{enabled}")

        print()
        print(Colors.dim(f"Pattern details: playtimed patterns show <id>"))

    elif args.action == "add":
        pattern_id = db.add_pattern(
            pattern=args.pattern,
            name=args.name,
            category=args.category,
            cpu_threshold=args.cpu_threshold or 5.0,
            notes=args.notes
        )
        print(f"Added pattern {pattern_id}: {args.name}")

    elif args.action == "disable":
        db.update_pattern(args.id, enabled=0)
        print(f"Disabled pattern {args.id}")

    elif args.action == "enable":
        db.update_pattern(args.id, enabled=1)
        print(f"Enabled pattern {args.id}")

    elif args.action == "delete":
        db.delete_pattern(args.id)
        print(f"Deleted pattern {args.id}")

    elif args.action == "note":
        pattern = db.get_pattern_by_id(args.id)
        if not pattern:
            print(f"Pattern {args.id} not found.", file=sys.stderr)
            sys.exit(1)

        if args.text:
            # Set notes
            db.set_pattern_notes(args.id, args.text)
            print(f"Set notes on pattern {args.id} ({pattern['name']})")
        else:
            # View pattern details including notes
            pattern_type = pattern.get('pattern_type', 'process')
            browser = pattern.get('browser')
            type_str = f"browser:{browser}" if pattern_type == 'browser_domain' and browser else pattern_type

            print(f"{Colors.bold('Pattern')} #{pattern['id']}: {pattern['name']}")
            print(f"  {Colors.dim('Type:')}     {type_str}")
            print(f"  {Colors.dim('Pattern:')}  {pattern['pattern']}")
            print(f"  {Colors.dim('State:')}    {pattern['monitor_state']}")
            print(f"  {Colors.dim('Category:')} {pattern.get('category') or '-'}")
            print(f"  {Colors.dim('Owner:')}    {pattern.get('owner') or '*'}")
            if pattern_type == 'process':
                print(f"  {Colors.dim('CPU:')}      {pattern['cpu_threshold']}%")
                print(f"  {Colors.dim('Runs:')}     {pattern.get('unique_pid_count', 0)}")
            print(f"  {Colors.dim('Runtime:')}  {format_runtime(pattern.get('total_runtime_seconds', 0))}")
            if pattern.get('discovered_cmdline'):
                print(f"  {Colors.dim('Cmdline:')}  {pattern['discovered_cmdline']}")
            print()
            if pattern.get('notes'):
                print(f"{Colors.bold('Notes:')}")
                print(pattern['notes'])
            else:
                print(Colors.dim("No notes set."))


def cmd_discover(args):
    """Manage process discovery."""
    if args.action in ("promote", "ignore", "disallow", "config"):
        require_root(f"discover {args.action}")

    try:
        db = ActivityDB(args.db)
    except Exception:
        print(f"Error: Cannot access database at {args.db}", file=sys.stderr)
        print(f"Try: sudo playtimed discover {args.action}", file=sys.stderr)
        sys.exit(1)

    if args.action == "list":
        # Show discovered patterns awaiting review
        discovered = db.get_patterns_by_state('discovered')
        if not discovered:
            print(Colors.info("No discovered patterns awaiting review."))
            print(f"\nRun the daemon to discover high-CPU processes and browser domains automatically.")
            return

        print(Colors.header("👀 Discovered") + " (awaiting review)")
        print()
        print(f"{Colors.bold('ID'):<6} {Colors.bold('Type'):<16} {Colors.bold('Owner'):<10} {Colors.bold('Name'):<25} {Colors.bold('Runtime'):<10} {Colors.bold('Last Seen'):<20}")
        print(Colors.dim("─" * 90))

        for p in discovered:
            owner = p.get('owner') or '*'
            runtime = format_runtime(p.get('total_runtime_seconds', 0))
            last_seen = p.get('last_seen', '')[:16].replace('T', ' ') if p.get('last_seen') else '-'

            # Format pattern type
            pattern_type = p.get('pattern_type', 'process')
            browser = p.get('browser')
            if pattern_type == 'browser_domain' and browser:
                type_str = f"browser:{browser}"
            else:
                type_str = pattern_type

            print(f"{Colors.warn(p['id']):<6} {type_str:<16} {owner:<10} {Colors.bold(p['name']):<25} {runtime:<10} {Colors.dim(last_seen):<20}")

        print()
        print(Colors.dim("Actions:"))
        print(f"  {Colors.ok('promote')} <id> gaming      - Monitor as gaming (counts against limit)")
        print(f"  {Colors.ok('promote')} <id> educational - Track as educational (IXL, etc.)")
        print(f"  {Colors.dim('ignore')} <id>            - Ignore (not tracked)")
        print(f"  {Colors.error('disallow')} <id>         - Block (terminate on sight)")

    elif args.action == "promote":
        name = getattr(args, 'name', None)
        db.set_pattern_state(args.id, 'active', category=args.category, name=name)
        msg = f"Promoted pattern {args.id} to active monitoring (category: {args.category})"
        if name:
            msg += f" as '{name}'"
        print(msg)

    elif args.action == "ignore":
        db.set_pattern_state(args.id, 'ignored')
        print(f"Marked pattern {args.id} as ignored")

    elif args.action == "disallow":
        db.set_pattern_state(args.id, 'disallowed')
        print(f"Marked pattern {args.id} as disallowed (will be terminated on detection)")

    elif args.action == "config":
        if args.key and args.value:
            db.set_discovery_config(args.key, args.value)
            print(f"Set {args.key} = {args.value}")
        else:
            config = db.get_discovery_config()
            print("Discovery configuration:")
            print(f"  enabled:              {config['enabled']}")
            print(f"  cpu_threshold:        {config['cpu_threshold']}%")
            print(f"  sample_window_seconds: {config['sample_window_seconds']}")
            print(f"  min_samples:          {config['min_samples']}")


def cmd_message(args):
    """Manage and test message templates."""
    if args.action == "add":
        require_root("message add")

    try:
        db = ActivityDB(args.db)
    except Exception:
        print(f"Error: Cannot access database at {args.db}", file=sys.stderr)
        sys.exit(1)

    if args.action == "list":
        templates = db.get_all_templates()
        if not templates:
            print("No templates found.")
            return

        # Group by intention
        by_intention = {}
        for t in templates:
            intention = t['intention']
            if intention not in by_intention:
                by_intention[intention] = []
            by_intention[intention].append(t)

        print(Colors.header("Message Templates"))
        print()

        for intention in sorted(by_intention.keys()):
            variants = by_intention[intention]
            enabled_count = sum(1 for v in variants if v['enabled'])
            print(f"{Colors.bold(intention)} ({enabled_count}/{len(variants)} enabled)")
            for v in variants:
                status = Colors.ok("●") if v['enabled'] else Colors.dim("○")
                urgency = v['urgency']
                urgency_color = Colors.RED if urgency == 'critical' else Colors.YELLOW if urgency == 'normal' else Colors.DIM
                print(f"  {status} [{v['id']}] {v['title']}")
                print(f"      {Colors.dim(v['body'][:60])}{'...' if len(v['body']) > 60 else ''}")
            print()

    elif args.action == "test":
        # Send a test notification for the given intention
        router = MessageRouter(db)
        ctx = MessageContext(
            user=args.user or "test_user",
            process=args.process or "TestGame",
            time_left=args.time_left or 30,
            time_used=60,
            time_limit=120,
            grace_seconds=30,
            mode="normal",
        )
        notification_id, backend = router.send(args.intention, ctx)
        print(f"Sent '{args.intention}' notification via {backend} (id: {notification_id})")

        # Show what was sent
        recent = db.get_recent_messages(limit=1)
        if recent:
            msg = recent[0]
            print(f"\n{Colors.bold('Title:')} {msg['rendered_title']}")
            print(f"{Colors.bold('Body:')} {msg['rendered_body']}")

    elif args.action == "add":
        template_id = db.add_template(
            intention=args.intention,
            title=args.title,
            body=args.body,
            icon=args.icon or 'dialog-information',
            urgency=args.urgency or 'normal',
        )
        print(f"Added template {template_id} for intention '{args.intention}'")


def cmd_user(args):
    """Manage user limits."""
    if args.action in ("add", "disable", "enable"):
        require_root(f"user {args.action}")

    try:
        db = ActivityDB(args.db)
    except Exception:
        print(f"Error: Cannot access database at {args.db}", file=sys.stderr)
        print(f"Try: sudo playtimed user {args.action}", file=sys.stderr)
        sys.exit(1)

    if args.action == "list":
        day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        users = db.get_all_monitored_users()
        for user in users:
            limits = db.get_user_limits(user)
            dl = db.get_daily_limits(user)
            print(f"\n{Colors.bold(user)}:")
            print(f"  Daily limits: {', '.join(f'{day_names[i]} {dl[i]}m' for i in range(7))}")
            print(f"  Use 'playtimed schedule {user}' for full grid")

    elif args.action == "add":
        db.set_user_limits(
            args.username,
            daily_total=args.daily_total or 180,
            gaming_limit=args.gaming_limit or 120,
            weekday_start=args.weekday_start or "16:00",
            weekday_end=args.weekday_end or "21:00",
            weekend_start=args.weekend_start or "09:00",
            weekend_end=args.weekend_end or "22:00"
        )
        print(f"Added/updated limits for {args.username}")

    elif args.action == "edit":
        require_root("user edit")
        user = args.username
        limits = db.get_user_limits(user)
        if not limits:
            print(f"No user '{user}' configured. Use 'user add' first.", file=sys.stderr)
            sys.exit(1)

        print(f"Editing limits for {Colors.bold(user)}")
        print("Use 'playtimed schedule edit' for per-day gaming limits and schedule.")
        print("Press Enter to keep current value, or type new value.\n")

        current = limits.get('daily_total', 180)
        val = input(f"  Daily total screen time (min) [{current}]: ").strip()
        if val:
            try:
                db.set_user_limits(user, daily_total=int(val))
                print(f"\nUpdated {user}: daily_total={val}")
            except ValueError:
                print(f"    Invalid value, keeping {current}")
        else:
            print("\nNo changes.")

    elif args.action == "disable":
        db.set_user_limits(args.username, enabled=0)
        print(f"Disabled monitoring for {args.username}")

    elif args.action == "enable":
        db.set_user_limits(args.username, enabled=1)
        print(f"Enabled monitoring for {args.username}")


def main():
    examples = """
Examples:
  # First-time setup: add a user with gaming limits
  playtimed user add anders --gaming-limit 120 --daily-total 180

  # Set allowed gaming hours (weekdays 4pm-9pm, weekends 9am-10pm)
  playtimed user add anders --weekday-start 16:00 --weekday-end 21:00 \\
                            --weekend-start 09:00 --weekend-end 22:00

  # Check current status
  playtimed status

  # Add a game pattern to monitor
  playtimed patterns add "factorio" "Factorio" gaming --cpu-threshold 10

  # List ALL patterns (active, discovered, ignored, disallowed)
  playtimed patterns list

  # Review discovered high-CPU applications
  playtimed discover list

  # Promote a discovered app to gaming monitoring
  playtimed discover promote 5 gaming

  # Ignore a discovered app (e.g., it's not a game)
  playtimed discover ignore 6

  # Block an app entirely (terminates on detection)
  playtimed discover disallow 7

  # View/adjust discovery settings
  playtimed discover config
  playtimed discover config cpu_threshold 30

  # Run the daemon (usually via systemd)
  playtimed run

  # View pattern details / set notes on a pattern
  playtimed patterns note 5
  playtimed patterns note 5 "This is Minecraft Java edition"
"""
    parser = argparse.ArgumentParser(
        description="Claude-powered screen time daemon",
        epilog=examples,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to database")
    subparsers = parser.add_subparsers(dest="command")

    # Run daemon
    run_parser = subparsers.add_parser("run", help="Run the daemon")
    run_parser.add_argument("-c", "--config", default=DEFAULT_CONFIG,
                           help="Path to config file")

    # Status command
    status_parser = subparsers.add_parser("status", help="Show screen time status")
    status_parser.add_argument("user", nargs="?", help="User to check (default: current)")

    # Maintenance command
    maint_parser = subparsers.add_parser("maintenance", help="Run database maintenance")
    maint_parser.add_argument("--events-days", type=int, default=30,
                              help="Keep events for this many days")
    maint_parser.add_argument("--sessions-days", type=int, default=90,
                              help="Keep sessions for this many days")

    # History command
    history_parser = subparsers.add_parser("history", help="Show daily screen time history")
    history_parser.add_argument("user", nargs="?", help="User to check (default: all)")
    history_parser.add_argument("--days", type=int, default=7, help="Number of days (default: 7)")

    # Sessions command
    sessions_parser = subparsers.add_parser("sessions", help="Show individual game sessions")
    sessions_parser.add_argument("user", nargs="?", help="User to check")
    sessions_parser.add_argument("--date", help="Specific date (YYYY-MM-DD)")
    sessions_parser.add_argument("--days", type=int, help="Last N days")

    # Audit command
    audit_parser = subparsers.add_parser("audit", help="Show process termination history")
    audit_parser.add_argument("user", nargs="?", help="User to check (default: all)")
    audit_parser.add_argument("--days", type=int, default=30, help="Number of days (default: 30)")

    # Report command
    report_parser = subparsers.add_parser("report", help="Show weekly summary report")
    report_parser.add_argument("user", nargs="?", help="User to check (default: all)")
    report_parser.add_argument("--days", type=int, default=7, help="Number of days (default: 7)")

    # Heatmap command
    heatmap_parser = subparsers.add_parser("heatmap", help="Show activity heatmap by day/hour")
    heatmap_parser.add_argument("user", nargs="?", help="User to check (default: all)")
    heatmap_parser.add_argument("--days", type=int, default=7, help="Number of days (default: 7)")

    # Schedule command
    schedule_parser = subparsers.add_parser("schedule", help="View/edit schedule grid")
    schedule_sub = schedule_parser.add_subparsers(dest="action")

    schedule_sub.add_parser("show", help="Show schedule grid (default)")
    schedule_show = schedule_sub.add_parser("view", help="Show schedule grid")
    schedule_show.add_argument("user", nargs="?", help="User to check (default: all)")

    schedule_set = schedule_sub.add_parser("set", help="Set schedule slots")
    schedule_set.add_argument("username", help="Username")
    schedule_set.add_argument("spec", nargs="+", help="Spec: <days> <hours> <+|-> (comma-separated)")

    schedule_edit = schedule_sub.add_parser("edit", help="Interactive schedule editor")
    schedule_edit.add_argument("username", help="Username")

    schedule_export = schedule_sub.add_parser("export", help="Export schedules as JSON")
    schedule_export.add_argument("username", nargs="?", help="User (default: all)")

    schedule_import = schedule_sub.add_parser("import", help="Import schedules from JSON")
    schedule_import.add_argument("file", help="JSON file to import")

    # Allow bare 'schedule' and 'schedule <user>' to show the grid
    schedule_parser.add_argument("user", nargs="?", help="User to check (default: all)")

    # Mode command
    mode_parser = subparsers.add_parser("mode", help="View or set daemon mode")
    mode_parser.add_argument("set_mode", nargs="?", choices=["normal", "passthrough", "strict"],
                             help="Mode to set (normal, passthrough, strict)")

    # Pattern management
    pattern_parser = subparsers.add_parser("patterns", help="Manage process patterns")
    pattern_sub = pattern_parser.add_subparsers(dest="action")

    pattern_sub.add_parser("list", help="List all patterns")

    add_pat = pattern_sub.add_parser("add", help="Add a pattern")
    add_pat.add_argument("pattern", help="Regex pattern")
    add_pat.add_argument("name", help="Display name")
    add_pat.add_argument("category", choices=["gaming", "launcher", "productive", "educational", "creative"])
    add_pat.add_argument("--cpu-threshold", type=float, help="Min CPU%% to count")
    add_pat.add_argument("--notes", help="Notes about this pattern")

    dis_pat = pattern_sub.add_parser("disable", help="Disable a pattern")
    dis_pat.add_argument("id", type=int, help="Pattern ID")

    en_pat = pattern_sub.add_parser("enable", help="Enable a pattern")
    en_pat.add_argument("id", type=int, help="Pattern ID")

    del_pat = pattern_sub.add_parser("delete", help="Delete a pattern")
    del_pat.add_argument("id", type=int, help="Pattern ID")

    note_pat = pattern_sub.add_parser("note", help="View or set notes on a pattern")
    note_pat.add_argument("id", type=int, help="Pattern ID")
    note_pat.add_argument("text", nargs="?", help="Note text (omit to view)")

    # Discovery management
    discover_parser = subparsers.add_parser("discover", help="Manage process discovery")
    discover_sub = discover_parser.add_subparsers(dest="action")

    discover_sub.add_parser("list", help="List discovered processes awaiting review")

    promote_disc = discover_sub.add_parser("promote", help="Promote to active monitoring")
    promote_disc.add_argument("id", type=int, help="Pattern ID")
    promote_disc.add_argument("category", choices=["gaming", "launcher", "productive", "educational", "creative"],
                              help="Category for monitoring")
    promote_disc.add_argument("--name", help="Display name (e.g., 'YouTube' instead of 'youtube.com')")

    ignore_disc = discover_sub.add_parser("ignore", help="Mark as ignored")
    ignore_disc.add_argument("id", type=int, help="Pattern ID")

    disallow_disc = discover_sub.add_parser("disallow", help="Mark as disallowed (terminate on sight)")
    disallow_disc.add_argument("id", type=int, help="Pattern ID")

    config_disc = discover_sub.add_parser("config", help="View/set discovery configuration")
    config_disc.add_argument("key", nargs="?", help="Config key to set")
    config_disc.add_argument("value", nargs="?", help="Value to set")

    # Message management
    message_parser = subparsers.add_parser("message", help="Manage message templates")
    message_sub = message_parser.add_subparsers(dest="action")

    message_sub.add_parser("list", help="List all message templates")

    test_msg = message_sub.add_parser("test", help="Send a test notification")
    test_msg.add_argument("intention", help="Message intention to test (e.g., process_start, time_warning_30)")
    test_msg.add_argument("--user", help="Test user name (default: test_user)")
    test_msg.add_argument("--process", help="Test process name (default: TestGame)")
    test_msg.add_argument("--time-left", type=int, help="Test time left in minutes (default: 30)")

    add_msg = message_sub.add_parser("add", help="Add a custom template")
    add_msg.add_argument("intention", help="Message intention")
    add_msg.add_argument("title", help="Notification title (supports {var} placeholders)")
    add_msg.add_argument("body", help="Notification body (supports {var} placeholders)")
    add_msg.add_argument("--icon", help="Icon name (default: dialog-information)")
    add_msg.add_argument("--urgency", choices=["low", "normal", "critical"], help="Urgency level (default: normal)")

    # User management
    user_parser = subparsers.add_parser("user", help="Manage user limits")
    user_sub = user_parser.add_subparsers(dest="action")

    user_sub.add_parser("list", help="List monitored users")

    add_user = user_sub.add_parser("add", help="Add/update user limits")
    add_user.add_argument("username", help="Username to monitor")
    add_user.add_argument("--daily-total", type=int, help="Daily total minutes")
    add_user.add_argument("--gaming-limit", type=int, help="Gaming limit minutes")
    add_user.add_argument("--weekday-start", help="Weekday start time (HH:MM)")
    add_user.add_argument("--weekday-end", help="Weekday end time (HH:MM)")
    add_user.add_argument("--weekend-start", help="Weekend start time (HH:MM)")
    add_user.add_argument("--weekend-end", help="Weekend end time (HH:MM)")

    edit_user = user_sub.add_parser("edit", help="Interactive user limits editor")
    edit_user.add_argument("username", help="Username")

    dis_user = user_sub.add_parser("disable", help="Disable user monitoring")
    dis_user.add_argument("username", help="Username")

    en_user = user_sub.add_parser("enable", help="Enable user monitoring")
    en_user.add_argument("username", help="Username")

    args = parser.parse_args()

    if args.command == "run":
        require_root("run")
        daemon = ClaudeDaemon(args.config)
        daemon.run()
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "history":
        cmd_history(args)
    elif args.command == "sessions":
        cmd_sessions(args)
    elif args.command == "audit":
        cmd_audit(args)
    elif args.command == "report":
        cmd_report(args)
    elif args.command == "heatmap":
        cmd_heatmap(args)
    elif args.command == "schedule":
        action = getattr(args, 'action', None)
        if action == "set":
            cmd_schedule_set(args)
        elif action == "edit":
            cmd_schedule_edit(args)
        elif action == "export":
            cmd_schedule_export(args)
        elif action == "import":
            cmd_schedule_import(args)
        else:
            cmd_schedule(args)
    elif args.command == "maintenance":
        cmd_maintenance(args)
    elif args.command == "mode":
        cmd_mode(args)
    elif args.command == "patterns":
        if args.action:
            cmd_patterns(args)
        else:
            pattern_parser.print_help()
    elif args.command == "discover":
        if args.action:
            cmd_discover(args)
        else:
            discover_parser.print_help()
    elif args.command == "user":
        if args.action:
            cmd_user(args)
        else:
            user_parser.print_help()
    elif args.command == "message":
        if args.action:
            cmd_message(args)
        else:
            message_parser.print_help()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
