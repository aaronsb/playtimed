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

from .db import ActivityDB

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

    def __init__(self, config_path: str):
        self.config = self._load_config(config_path)
        self.running = True
        self.state: dict[str, UserState] = {}
        self.active_games: dict[str, dict[int, ProcessMatch]] = {}  # user -> {pid -> match}
        self.notifiers: dict[str, NotificationBackend] = {}

        # Discovery tracking: {(user, proc_name): {'samples': [...], 'first_seen': time}}
        self.discovery_candidates: dict[tuple[str, str], dict] = {}

        # Initialize database
        db_path = self.config.get("daemon", {}).get("db_path", DEFAULT_DB_PATH)
        self.db = ActivityDB(db_path)
        log.info(f"Database initialized at {db_path}")

        # Load discovery config
        self.discovery_config = self.db.get_discovery_config()

        # Set up signal handlers
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

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
        """Find running processes matching active gaming patterns."""
        matches = []

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
                    cpu_threshold = pdef.get("cpu_threshold", 5.0)

                    try:
                        cpu = proc.cpu_percent(interval=0.1)
                    except psutil.NoSuchProcess:
                        continue

                    if cpu >= cpu_threshold:
                        matches.append(ProcessMatch(
                            pid=proc.info['pid'],
                            name=pdef.get("name", proc_name),
                            category="gaming",
                            cmdline=cmdline[:100],
                            cpu_percent=cpu
                        ))

                        # Track stats for this pattern
                        self.db.record_pid_seen(pdef['id'], proc.info['pid'])

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return matches

    def _scan_all_processes(self, user: str):
        """Scan all processes for a user, handling all pattern states and discovery."""
        poll_interval = self.config["daemon"].get("poll_interval", 30)

        # Get ALL patterns (all states) for matching
        all_patterns = self.db.get_patterns(enabled_only=True, include_all_states=True, owner=user)

        for proc in psutil.process_iter(['pid', 'name', 'username', 'cmdline']):
            try:
                if proc.info['username'] != user:
                    continue

                cmdline = ' '.join(proc.info.get('cmdline') or [])
                proc_name = proc.info.get('name', '')
                pid = proc.info['pid']

                # Skip system/shell processes
                if proc_name in ('bash', 'zsh', 'fish', 'sh', 'python', 'python3',
                                 'systemd', 'dbus-daemon', 'pipewire', 'pulseaudio'):
                    continue

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

                    # Handle disallowed processes
                    if state == 'disallowed':
                        log.info(f"Killing disallowed process: {proc_name} (PID {pid})")
                        self._kill_process(ProcessMatch(
                            pid=pid, name=proc_name, category="disallowed",
                            cmdline=cmdline[:100], cpu_percent=cpu
                        ), user)
                        notifier = self._get_notifier(user)
                        notifier.send("🚫 Blocked",
                                      f"I stopped {proc_name} - it's on the not-allowed list.",
                                      urgency="critical")

                else:
                    # No pattern match - check if this should be discovered
                    self._check_discovery(user, proc_name, cmdline, pid, cpu)

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

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

            # Notify the user
            notifier = self._get_notifier(user)
            notifier.send("👀 New Application Detected",
                          f"I noticed {proc_name} using a lot of CPU. "
                          f"I've added it to my discovery list. "
                          f"Your dad can review it later.",
                          urgency="low")

            # Clean up
            del self.discovery_candidates[key]

    def _is_allowed_time(self, user: str) -> tuple[bool, str]:
        """Check if current time is within allowed hours."""
        user_config = self.config.get("users", {}).get(user, {})
        schedule = user_config.get("schedule", {})

        if not schedule:
            return True, ""

        now = datetime.now()
        day_type = "weekend" if now.weekday() >= 5 else "weekday"
        day_schedule = schedule.get(day_type, {})

        start = day_schedule.get("allowed_start", "00:00")
        end = day_schedule.get("allowed_end", "23:59")

        start_time = datetime.strptime(start, "%H:%M").time()
        end_time = datetime.strptime(end, "%H:%M").time()
        current_time = now.time()

        if start_time <= current_time <= end_time:
            return True, ""

        return False, f"Gaming is only allowed between {start} and {end}"

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

    def _kill_process(self, proc: ProcessMatch, user: str):
        """Terminate a process gracefully, then forcefully."""
        notifier = self._get_notifier(user)

        try:
            p = psutil.Process(proc.pid)

            # SIGTERM first
            log.info(f"Sending SIGTERM to {proc.name} (PID {proc.pid})")
            p.terminate()

            # Wait for graceful exit
            try:
                p.wait(timeout=30)
                log.info(f"{proc.name} exited gracefully")
            except psutil.TimeoutExpired:
                # SIGKILL
                log.info(f"Sending SIGKILL to {proc.name} (PID {proc.pid})")
                p.kill()

            notifier.send("🎮 Time's Up",
                         MessageTemplates.get("KILLED", app=proc.name))

        except psutil.NoSuchProcess:
            log.debug(f"Process {proc.pid} already gone")
        except psutil.AccessDenied:
            log.error(f"Access denied killing PID {proc.pid}")

    def _process_user(self, user: str):
        """Process monitoring for a single user."""
        user_config = self.config.get("users", {}).get(user, {})
        if not user_config.get("enabled", True):
            return

        # Run full process scan (discovery, stats, disallowed termination)
        self._scan_all_processes(user)

        state = self._load_user_state(user)
        notifier = self._get_notifier(user)

        # Check time restrictions
        allowed, reason = self._is_allowed_time(user)
        total_remaining, gaming_remaining = self._get_remaining_time(user)

        # Find gaming processes
        current_games = self._find_gaming_processes(user)
        prev_games = self.active_games.get(user, {})

        # Track which PIDs we're seeing
        current_pids = {g.pid for g in current_games}
        prev_pids = set(prev_games.keys())

        # New games started
        for game in current_games:
            if game.pid not in prev_pids:
                log.info(f"Detected new game: {game.name} (PID {game.pid}) for {user}")

                # Log to database
                self.db.log_event(user, "game_detected", app=game.name,
                                  category="gaming", pid=game.pid)

                if not allowed:
                    notifier.send("🚫 Not Now", reason, urgency="critical")
                    self.db.log_event(user, "blocked_schedule", app=game.name,
                                      details=reason, pid=game.pid)
                    self._kill_process(game, user)
                    continue

                if gaming_remaining <= 0:
                    notifier.send("🚫 Time's Up",
                                 MessageTemplates.get("BLOCKED"),
                                 urgency="critical")
                    self.db.log_event(user, "blocked_quota", app=game.name, pid=game.pid)
                    self._kill_process(game, user)
                    continue

                # Allowed - start session tracking
                session_id = self.db.start_session(user, game.name, "gaming", game.pid)
                game.session_id = session_id
                self.db.increment_session_count(user)
                self.db.log_event(user, "game_start", app=game.name, pid=game.pid)

                # Send notification
                notifier.send("🎮 Game On",
                             MessageTemplates.get("GAME_START",
                                                 app=game.name,
                                                 gaming_remaining=format_duration(gaming_remaining)))

        # Update active games
        self.active_games[user] = {g.pid: g for g in current_games}

        # Track time if games are running
        poll_interval = self.config["daemon"].get("poll_interval", 30)
        if current_games:
            state.gaming_time += poll_interval
            state.total_time += poll_interval

            # Refresh remaining time
            total_remaining, gaming_remaining = self._get_remaining_time(user)

            # Send warnings
            for game in current_games:
                self._send_warning_if_needed(user, gaming_remaining, game.name)

            # Enforce time limit
            if gaming_remaining <= 0:
                for game in current_games:
                    notifier.send("⏰ Time's Up",
                                 MessageTemplates.get("TIME_UP",
                                                     app=game.name,
                                                     total_remaining=format_duration(total_remaining)),
                                 urgency="critical")
                    time.sleep(30)  # Grace period
                    self._kill_process(game, user)

        self._save_user_state(user)

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

        # Get users from database, fall back to config
        users = self.db.get_all_monitored_users()
        if not users:
            users = list(self.config.get("users", {}).keys())

        if not users:
            log.warning("No users configured, nothing to monitor")
        else:
            log.info(f"Monitoring users: {', '.join(users)}")

        while self.running:
            for user in users:
                try:
                    self._process_user(user)
                except Exception as e:
                    log.error(f"Error processing user {user}: {e}", exc_info=True)

            time.sleep(poll_interval)

        # Save all state on exit
        for user in users:
            self._save_user_state(user)

        log.info("playtimed shutdown complete")


def cmd_status(args):
    """Show status for current user."""
    user = getattr(args, 'user', None) or os.environ.get("USER", "unknown")
    db_path = getattr(args, 'db', DEFAULT_DB_PATH)

    try:
        db = ActivityDB(db_path)
    except Exception:
        print(f"Error: Cannot access database at {db_path}", file=sys.stderr)
        print("Try: sudo playtimed status", file=sys.stderr)
        sys.exit(1)

    # Get time used from database
    total_used, gaming_used = db.get_time_used_today(user)

    # Get limits from database
    limits = db.get_user_limits(user)
    if limits:
        gaming_limit = limits['gaming_limit'] * 60
        total_limit = limits['daily_total'] * 60
    else:
        gaming_limit = 120 * 60  # default 2 hours
        total_limit = 180 * 60  # default 3 hours

    gaming_remaining = max(0, gaming_limit - gaming_used)
    total_remaining = max(0, total_limit - total_used)

    print(f"📊 Screen Time Status for {user}")
    print(f"   Date: {date.today().isoformat()}")
    print()
    print(f"   Gaming: {format_duration(gaming_used)} used")
    print(f"           {format_duration(gaming_remaining)} remaining")
    print()
    print(f"   Total:  {format_duration(total_used)} used")
    print(f"           {format_duration(total_remaining)} remaining")


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

        print(f"{'ID':<4} {'State':<12} {'Category':<10} {'Owner':<10} {'Name':<18} {'Pattern':<25} {'Runs':<5} {'Runtime':<8}")
        print("-" * 105)
        for p in patterns:
            state = p.get('monitor_state', 'active')
            category = p.get('category') or '-'
            owner = p.get('owner') or '*'
            runs = p.get('unique_pid_count', 0)
            runtime = format_runtime(p.get('total_runtime_seconds', 0))
            enabled = "" if p['enabled'] else " (disabled)"
            print(f"{p['id']:<4} {state:<12} {category:<10} {owner:<10} {p['name']:<18} {p['pattern'][:24]:<25} {runs:<5} {runtime:<8}{enabled}")

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
            print("No discovered patterns awaiting review.")
            print("\nRun the daemon to discover high-CPU processes automatically.")
            return

        print("Discovered processes (awaiting review):\n")
        print(f"{'ID':<4} {'Owner':<10} {'Name':<25} {'Runs':<5} {'Runtime':<10} {'Last Seen':<20}")
        print("-" * 80)
        for p in discovered:
            owner = p.get('owner') or '*'
            runs = p.get('unique_pid_count', 0)
            runtime = format_runtime(p.get('total_runtime_seconds', 0))
            last_seen = p.get('last_seen', '')[:19] if p.get('last_seen') else '-'
            print(f"{p['id']:<4} {owner:<10} {p['name']:<25} {runs:<5} {runtime:<10} {last_seen:<20}")

        print("\nUse 'playtimed discover promote <id> <category>' to monitor")
        print("Use 'playtimed discover ignore <id>' to ignore")
        print("Use 'playtimed discover disallow <id>' to block")

    elif args.action == "promote":
        db.set_pattern_state(args.id, 'active', category=args.category)
        print(f"Promoted pattern {args.id} to active monitoring (category: {args.category})")

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
        users = db.get_all_monitored_users()
        for user in users:
            limits = db.get_user_limits(user)
            print(f"\n{user}:")
            print(f"  Daily total: {limits['daily_total']} min")
            print(f"  Gaming limit: {limits['gaming_limit']} min")
            print(f"  Weekday: {limits['weekday_start']} - {limits['weekday_end']}")
            print(f"  Weekend: {limits['weekend_start']} - {limits['weekend_end']}")

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

    # Pattern management
    pattern_parser = subparsers.add_parser("patterns", help="Manage process patterns")
    pattern_sub = pattern_parser.add_subparsers(dest="action")

    pattern_sub.add_parser("list", help="List all patterns")

    add_pat = pattern_sub.add_parser("add", help="Add a pattern")
    add_pat.add_argument("pattern", help="Regex pattern")
    add_pat.add_argument("name", help="Display name")
    add_pat.add_argument("category", choices=["gaming", "launcher", "productive"])
    add_pat.add_argument("--cpu-threshold", type=float, help="Min CPU%% to count")
    add_pat.add_argument("--notes", help="Notes about this pattern")

    dis_pat = pattern_sub.add_parser("disable", help="Disable a pattern")
    dis_pat.add_argument("id", type=int, help="Pattern ID")

    en_pat = pattern_sub.add_parser("enable", help="Enable a pattern")
    en_pat.add_argument("id", type=int, help="Pattern ID")

    del_pat = pattern_sub.add_parser("delete", help="Delete a pattern")
    del_pat.add_argument("id", type=int, help="Pattern ID")

    # Discovery management
    discover_parser = subparsers.add_parser("discover", help="Manage process discovery")
    discover_sub = discover_parser.add_subparsers(dest="action")

    discover_sub.add_parser("list", help="List discovered processes awaiting review")

    promote_disc = discover_sub.add_parser("promote", help="Promote to active monitoring")
    promote_disc.add_argument("id", type=int, help="Pattern ID")
    promote_disc.add_argument("category", choices=["gaming", "launcher", "productive"],
                              help="Category for monitoring")

    ignore_disc = discover_sub.add_parser("ignore", help="Mark as ignored")
    ignore_disc.add_argument("id", type=int, help="Pattern ID")

    disallow_disc = discover_sub.add_parser("disallow", help="Mark as disallowed (terminate on sight)")
    disallow_disc.add_argument("id", type=int, help="Pattern ID")

    config_disc = discover_sub.add_parser("config", help="View/set discovery configuration")
    config_disc.add_argument("key", nargs="?", help="Config key to set")
    config_disc.add_argument("value", nargs="?", help="Value to set")

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
    elif args.command == "maintenance":
        cmd_maintenance(args)
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
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
