"""
SQLite database for playtimed activity tracking.

Stores structured activity data for long-term metrics and analytics.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = "/var/lib/playtimed/playtimed.db"

# Schedule constants
DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
SCHEDULE_LEN = 168  # 7 days * 24 hours
DEFAULT_SCHEDULE = '0' * SCHEDULE_LEN
DEFAULT_DAILY_LIMITS = '120,120,120,120,120,120,120'  # 7 days, minutes


def parse_daily_limits(s: str) -> list[int]:
    """Parse comma-separated daily limits string into list of 7 ints."""
    if not s:
        return [120] * 7
    parts = s.split(',')
    if len(parts) != 7:
        return [120] * 7
    return [int(x) for x in parts]


def format_daily_limits(limits: list[int]) -> str:
    """Format list of 7 ints as comma-separated string."""
    return ','.join(str(x) for x in limits)


def schedule_from_ranges(wd_start: str, wd_end: str,
                         we_start: str, we_end: str) -> str:
    """Build a 168-char schedule string from start/end time ranges.

    Used as a convenience converter — the schedule string is the source of truth.
    """
    wd_s = int(wd_start.split(':')[0]) if wd_start else 0
    wd_e = int(wd_end.split(':')[0]) if wd_end else 24
    we_s = int(we_start.split(':')[0]) if we_start else 0
    we_e = int(we_end.split(':')[0]) if we_end else 24
    bits = []
    for day in range(7):
        is_weekend = day >= 5
        start, end = (we_s, we_e) if is_weekend else (wd_s, wd_e)
        for hour in range(24):
            bits.append('1' if start <= hour < end else '0')
    return ''.join(bits)


def get_allowed_window(schedule: str, day: int) -> str:
    """Get human-readable allowed hours for a given day from schedule string.

    Returns e.g. "7:00 AM - 9:00 AM, 5:00 PM - 10:00 PM" or "none".
    """
    day_sched = schedule[day * 24:(day + 1) * 24]
    ranges = []
    start = None
    for h in range(25):  # 25 to close trailing range at midnight
        in_range = h < 24 and day_sched[h] == '1'
        if in_range and start is None:
            start = h
        elif not in_range and start is not None:
            ranges.append(f"{_fmt_hour(start)} - {_fmt_hour(h)}")
            start = None
    return ', '.join(ranges) if ranges else 'none'


def _fmt_hour(h: int) -> str:
    """Format hour as 12-hour time (e.g. 17 -> '5:00 PM')."""
    if h == 0 or h == 24:
        return '12:00 AM'
    elif h == 12:
        return '12:00 PM'
    elif h < 12:
        return f'{h}:00 AM'
    else:
        return f'{h - 12}:00 PM'


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """Initialize database schema."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    with get_connection(db_path) as conn:
        conn.executescript("""
            -- Activity events (append-only log)
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                user TEXT NOT NULL,
                event_type TEXT NOT NULL,
                app TEXT,
                category TEXT,
                details TEXT,
                pid INTEGER
            );

            -- Daily summaries (one row per user per day)
            CREATE TABLE IF NOT EXISTS daily_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                user TEXT NOT NULL,
                total_time INTEGER NOT NULL DEFAULT 0,
                gaming_time INTEGER NOT NULL DEFAULT 0,
                session_count INTEGER NOT NULL DEFAULT 0,
                warnings_sent INTEGER NOT NULL DEFAULT 0,
                enforcements INTEGER NOT NULL DEFAULT 0,
                UNIQUE(date, user)
            );

            -- Hourly activity (one row per user per hour per day)
            CREATE TABLE IF NOT EXISTS hourly_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                hour INTEGER NOT NULL,
                user TEXT NOT NULL,
                gaming_seconds INTEGER NOT NULL DEFAULT 0,
                total_seconds INTEGER NOT NULL DEFAULT 0,
                UNIQUE(date, hour, user)
            );

            -- Session tracking (start/end of each app session)
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT NOT NULL,
                app TEXT NOT NULL,
                category TEXT,
                pid INTEGER,
                start_time TEXT NOT NULL,
                end_time TEXT,
                duration INTEGER,
                end_reason TEXT  -- 'natural', 'enforced', 'logout', 'unknown'
            );

            -- Process patterns for detection and discovery
            -- monitor_state: 'active' (counting time), 'discovered' (found, needs review),
            --                'ignored' (don't care), 'disallowed' (terminate on sight)
            -- pattern_type: 'process' (traditional), 'browser_domain' (website tracking)
            CREATE TABLE IF NOT EXISTS process_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT NOT NULL,
                name TEXT NOT NULL,
                category TEXT,  -- 'gaming', 'launcher', 'productive', 'educational', 'creative' (NULL for discovered)
                pattern_type TEXT NOT NULL DEFAULT 'process',  -- 'process' or 'browser_domain'
                browser TEXT,  -- 'chrome', 'chromium', 'firefox' (NULL for processes)
                monitor_state TEXT NOT NULL DEFAULT 'active',
                owner TEXT,  -- user who owns this process (NULL = all users)
                enabled INTEGER NOT NULL DEFAULT 1,
                cpu_threshold REAL DEFAULT 5.0,  -- minimum CPU% to count as active (0 for browser domains)

                -- Discovery metadata
                discovered_cmdline TEXT,  -- original cmdline that led to discovery

                -- Runtime statistics (updated regardless of state)
                unique_pid_count INTEGER DEFAULT 0,  -- approximate invocation count
                total_runtime_seconds INTEGER DEFAULT 0,
                last_seen TEXT,

                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- Track PIDs we've seen for unique counting
            CREATE TABLE IF NOT EXISTS seen_pids (
                pattern_id INTEGER NOT NULL,
                pid INTEGER NOT NULL,
                first_seen TEXT NOT NULL,
                PRIMARY KEY (pattern_id, pid),
                FOREIGN KEY (pattern_id) REFERENCES process_patterns(id) ON DELETE CASCADE
            );

            -- Discovery configuration
            CREATE TABLE IF NOT EXISTS discovery_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                description TEXT
            );

            -- User limits configuration
            CREATE TABLE IF NOT EXISTS user_limits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                daily_total INTEGER NOT NULL DEFAULT 180,  -- minutes
                schedule TEXT NOT NULL DEFAULT '',  -- 168-char string: 7 days * 24 hours, '1'=allowed '0'=blocked
                daily_limits TEXT NOT NULL DEFAULT '120,120,120,120,120,120,120',  -- per-day gaming limits (minutes)
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- Indexes for common queries
            CREATE INDEX IF NOT EXISTS idx_events_user_date
                ON events(user, timestamp);
            CREATE INDEX IF NOT EXISTS idx_daily_user_date
                ON daily_summary(user, date);
            CREATE INDEX IF NOT EXISTS idx_hourly_user_date
                ON hourly_activity(user, date);
            CREATE INDEX IF NOT EXISTS idx_sessions_user_date
                ON sessions(user, start_time);
            CREATE INDEX IF NOT EXISTS idx_patterns_category
                ON process_patterns(category, enabled);
            CREATE INDEX IF NOT EXISTS idx_patterns_state
                ON process_patterns(monitor_state);
            CREATE INDEX IF NOT EXISTS idx_patterns_owner
                ON process_patterns(owner);
            -- NOTE: idx_patterns_type created in migrate_db after column is added

            -- Daemon configuration (mode, etc.)
            CREATE TABLE IF NOT EXISTS daemon_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                description TEXT
            );

            -- Message templates for notifications
            CREATE TABLE IF NOT EXISTS message_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intention TEXT NOT NULL,
                variant INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                icon TEXT DEFAULT 'dialog-information',
                urgency TEXT DEFAULT 'normal',
                enabled INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE(intention, variant)
            );

            CREATE INDEX IF NOT EXISTS idx_templates_intention
                ON message_templates(intention, enabled);

            -- Message delivery log
            CREATE TABLE IF NOT EXISTS message_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                user TEXT NOT NULL,
                intention TEXT NOT NULL,
                template_id INTEGER,
                rendered_title TEXT,
                rendered_body TEXT,
                notification_id INTEGER,
                backend TEXT,
                FOREIGN KEY (template_id) REFERENCES message_templates(id)
            );

            CREATE INDEX IF NOT EXISTS idx_message_log_user_time
                ON message_log(user, timestamp);
        """)

        # Seed default discovery config
        conn.executescript("""
            INSERT OR IGNORE INTO discovery_config (key, value, description) VALUES
                ('enabled', '1', 'Enable automatic process discovery'),
                ('cpu_threshold', '25', 'Minimum CPU% to consider for discovery'),
                ('sample_window_seconds', '120', 'How long to observe before flagging'),
                ('min_samples', '3', 'Minimum samples above threshold to flag');
        """)

        # Seed default daemon config
        conn.executescript("""
            INSERT OR IGNORE INTO daemon_config (key, value, description) VALUES
                ('mode', 'normal', 'Daemon mode: normal, passthrough, strict'),
                ('strict_grace_seconds', '30', 'Grace period before terminating in strict mode');
        """)


def migrate_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """Run database migrations for schema updates."""
    with get_connection(db_path) as conn:
        # Check if process_patterns needs migration (look for monitor_state column)
        cursor = conn.execute("PRAGMA table_info(process_patterns)")
        columns = {row[1] for row in cursor.fetchall()}

        if 'monitor_state' not in columns:
            # Add new columns to process_patterns
            conn.executescript("""
                ALTER TABLE process_patterns ADD COLUMN monitor_state TEXT DEFAULT 'active';
                ALTER TABLE process_patterns ADD COLUMN owner TEXT;
                ALTER TABLE process_patterns ADD COLUMN discovered_cmdline TEXT;
                ALTER TABLE process_patterns ADD COLUMN unique_pid_count INTEGER DEFAULT 0;
                ALTER TABLE process_patterns ADD COLUMN total_runtime_seconds INTEGER DEFAULT 0;
                ALTER TABLE process_patterns ADD COLUMN last_seen TEXT;

                UPDATE process_patterns SET monitor_state = 'active' WHERE monitor_state IS NULL;
            """)

        # Make category nullable if it isn't (for discovered patterns)
        # SQLite doesn't support ALTER COLUMN, but NULL is already allowed by default

        # Create seen_pids table if not exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_pids (
                pattern_id INTEGER NOT NULL,
                pid INTEGER NOT NULL,
                first_seen TEXT NOT NULL,
                PRIMARY KEY (pattern_id, pid),
                FOREIGN KEY (pattern_id) REFERENCES process_patterns(id) ON DELETE CASCADE
            )
        """)

        # Create discovery_config table if not exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discovery_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                description TEXT
            )
        """)

        # Seed default discovery config
        conn.executescript("""
            INSERT OR IGNORE INTO discovery_config (key, value, description) VALUES
                ('enabled', '1', 'Enable automatic process discovery'),
                ('cpu_threshold', '25', 'Minimum CPU% to consider for discovery'),
                ('sample_window_seconds', '120', 'How long to observe before flagging'),
                ('min_samples', '3', 'Minimum samples above threshold to flag');
        """)

        # Add new indexes if they don't exist
        conn.execute("CREATE INDEX IF NOT EXISTS idx_patterns_state ON process_patterns(monitor_state)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_patterns_owner ON process_patterns(owner)")

        # Create daemon_config table if not exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daemon_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                description TEXT
            )
        """)

        # Seed default daemon config
        conn.executescript("""
            INSERT OR IGNORE INTO daemon_config (key, value, description) VALUES
                ('mode', 'normal', 'Daemon mode: normal, passthrough, strict'),
                ('strict_grace_seconds', '30', 'Grace period before terminating in strict mode');
        """)

        # Create message router tables
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS message_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intention TEXT NOT NULL,
                variant INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                icon TEXT DEFAULT 'dialog-information',
                urgency TEXT DEFAULT 'normal',
                enabled INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE(intention, variant)
            );

            CREATE TABLE IF NOT EXISTS message_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                user TEXT NOT NULL,
                intention TEXT NOT NULL,
                template_id INTEGER,
                rendered_title TEXT,
                rendered_body TEXT,
                notification_id INTEGER,
                backend TEXT,
                FOREIGN KEY (template_id) REFERENCES message_templates(id)
            );

            CREATE INDEX IF NOT EXISTS idx_templates_intention
                ON message_templates(intention, enabled);
            CREATE INDEX IF NOT EXISTS idx_message_log_user_time
                ON message_log(user, timestamp);
        """)

        # Add state tracking columns to daily_summary if not present
        cursor = conn.execute("PRAGMA table_info(daily_summary)")
        columns = {row[1] for row in cursor.fetchall()}

        if 'state' not in columns:
            conn.executescript("""
                ALTER TABLE daily_summary ADD COLUMN state TEXT DEFAULT 'available';
                ALTER TABLE daily_summary ADD COLUMN gaming_active INTEGER DEFAULT 0;
                ALTER TABLE daily_summary ADD COLUMN gaming_started_at TEXT;
                ALTER TABLE daily_summary ADD COLUMN last_poll_at TEXT;
                ALTER TABLE daily_summary ADD COLUMN warned_30 INTEGER DEFAULT 0;
                ALTER TABLE daily_summary ADD COLUMN warned_15 INTEGER DEFAULT 0;
                ALTER TABLE daily_summary ADD COLUMN warned_5 INTEGER DEFAULT 0;
            """)

        # Seed default message templates if empty
        count = conn.execute("SELECT COUNT(*) FROM message_templates").fetchone()[0]
        if count == 0:
            _seed_default_templates(conn)

        # Add browser visibility columns (ADR-001)
        cursor = conn.execute("PRAGMA table_info(process_patterns)")
        columns = {row[1] for row in cursor.fetchall()}

        if 'pattern_type' not in columns:
            conn.executescript("""
                ALTER TABLE process_patterns ADD COLUMN pattern_type TEXT DEFAULT 'process';
                ALTER TABLE process_patterns ADD COLUMN browser TEXT;

                UPDATE process_patterns SET pattern_type = 'process' WHERE pattern_type IS NULL;
            """)

            # Add index for pattern_type queries
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_patterns_type
                    ON process_patterns(pattern_type)
            """)

        # Add schedule column to user_limits if not present
        cursor = conn.execute("PRAGMA table_info(user_limits)")
        ul_columns = {row[1] for row in cursor.fetchall()}
        if 'schedule' not in ul_columns:
            conn.execute("ALTER TABLE user_limits ADD COLUMN schedule TEXT")
            # Migrate existing time ranges to schedule strings
            has_legacy = 'weekday_start' in ul_columns
            if has_legacy:
                rows = conn.execute(
                    "SELECT user, weekday_start, weekday_end, weekend_start, weekend_end FROM user_limits"
                ).fetchall()
                for row in rows:
                    sched = schedule_from_ranges(row[1], row[2], row[3], row[4])
                    conn.execute("UPDATE user_limits SET schedule = ? WHERE user = ?", (sched, row[0]))

        # Add daily_limits column to user_limits if not present
        if 'daily_limits' not in ul_columns:
            conn.execute("ALTER TABLE user_limits ADD COLUMN daily_limits TEXT")
            # Migrate: use existing gaming_limit for all 7 days
            has_gaming_limit = 'gaming_limit' in ul_columns
            if has_gaming_limit:
                rows = conn.execute("SELECT user, gaming_limit FROM user_limits").fetchall()
                for row in rows:
                    gl = row[1] or 120
                    dl = ','.join([str(gl)] * 7)
                    conn.execute("UPDATE user_limits SET daily_limits = ? WHERE user = ?", (dl, row[0]))

        # Backfill: ensure all users have schedule and daily_limits populated
        # (handles case where columns exist but values are NULL)
        has_legacy_cols = 'weekday_start' in ul_columns
        if has_legacy_cols:
            null_schedule = conn.execute(
                "SELECT user, weekday_start, weekday_end, weekend_start, weekend_end "
                "FROM user_limits WHERE schedule IS NULL OR schedule = ''"
            ).fetchall()
            for row in null_schedule:
                sched = schedule_from_ranges(
                    row[1] or '16:00', row[2] or '21:00',
                    row[3] or '09:00', row[4] or '22:00')
                conn.execute("UPDATE user_limits SET schedule = ? WHERE user = ?", (sched, row[0]))

        has_gl_col = 'gaming_limit' in ul_columns
        if has_gl_col:
            null_dl = conn.execute(
                "SELECT user, gaming_limit FROM user_limits WHERE daily_limits IS NULL OR daily_limits = ''"
            ).fetchall()
            for row in null_dl:
                gl = row[1] or 120
                dl = ','.join([str(gl)] * 7)
                conn.execute("UPDATE user_limits SET daily_limits = ? WHERE user = ?", (dl, row[0]))

        # Create hourly_activity table if not exists
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS hourly_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                hour INTEGER NOT NULL,
                user TEXT NOT NULL,
                gaming_seconds INTEGER NOT NULL DEFAULT 0,
                total_seconds INTEGER NOT NULL DEFAULT 0,
                UNIQUE(date, hour, user)
            );
            CREATE INDEX IF NOT EXISTS idx_hourly_user_date
                ON hourly_activity(user, date);
        """)


def _seed_default_templates(conn):
    """Seed default message templates."""
    now = datetime.now().isoformat()

    templates = [
        # process_start - when a tracked game begins
        ('process_start', 0, 'Have fun!',
         'Starting up {process}! You have {time_left} minutes of gaming time today.',
         'dialog-information', 'normal'),
        ('process_start', 1, '{process}',
         'Nice choice, {user}. {time_left} minutes available - enjoy!',
         'dialog-information', 'normal'),
        ('process_start', 2, 'Game time',
         '{process} is running. You have {time_left} minutes today.',
         'dialog-information', 'normal'),

        # process_end - when a tracked game exits
        ('process_end', 0, 'Session ended',
         'Done with {process}? You have {time_left} minutes left for today.',
         'dialog-information', 'low'),
        ('process_end', 1, 'See you later',
         '{process} closed. {time_left} minutes remaining if you want to play more.',
         'dialog-information', 'low'),

        # time_warning - approaching limit (30 min)
        ('time_warning_30', 0, 'Time check',
         'Half hour left for today, {user}. Just a heads up!',
         'dialog-information', 'normal'),
        ('time_warning_30', 1, '30 minutes left',
         'You have 30 minutes of gaming time remaining today.',
         'dialog-information', 'normal'),

        # time_warning - 15 min
        ('time_warning_15', 0, '15 minutes left',
         'Getting close, {user}. Maybe start thinking about a save point?',
         'dialog-warning', 'normal'),
        ('time_warning_15', 1, 'Heads up',
         '15 minutes remaining. Good time to wrap up what you are doing.',
         'dialog-warning', 'normal'),

        # time_warning - 5 min
        ('time_warning_5', 0, 'Almost time',
         'Just 5 minutes left, {user}. Time to save and finish up!',
         'dialog-warning', 'normal'),
        ('time_warning_5', 1, '5 minutes!',
         'Wrapping up time - 5 minutes remaining for today.',
         'dialog-warning', 'normal'),

        # time_expired - limit reached
        ('time_expired', 0, 'Time is up',
         'That is your gaming time for today, {user}. Save your game now!',
         'dialog-warning', 'critical'),
        ('time_expired', 1, 'Limit reached',
         'You have used your {time_limit} minutes for today. Time to save!',
         'dialog-warning', 'critical'),

        # grace_period - countdown before enforcement
        ('grace_period', 0, 'Saving time',
         '{grace_seconds} seconds to save your game before I need to close it.',
         'dialog-warning', 'critical'),

        # enforcement - app was terminated
        ('enforcement', 0, 'Game closed',
         'Had to close {process}. Your time is up for today.',
         'dialog-error', 'critical'),
        ('enforcement', 1, 'Session ended',
         '{process} was closed. Tomorrow is another day!',
         'dialog-error', 'critical'),

        # blocked_launch - tried to start game when not allowed
        ('blocked_launch', 0, 'Not right now',
         'Cannot start {process} - you have used your gaming time for today.',
         'dialog-error', 'critical'),
        ('blocked_launch', 1, 'Time is up',
         '{process} is blocked. Your gaming time resets tomorrow.',
         'dialog-error', 'critical'),

        # outside_hours - tried to play outside allowed hours
        ('outside_hours', 0, 'Outside gaming hours',
         'Gaming hours today: {allowed_window}. Come back later!',
         'dialog-information', 'normal'),

        # discovery - new process detected
        ('discovery', 0, 'New app detected',
         'I noticed {process} running. I will keep an eye on it.',
         'dialog-information', 'low'),
        ('discovery', 1, 'Spotted something new',
         '{process} is new to me. If it is a game, it might get added to tracking.',
         'dialog-information', 'low'),
        ('discovery', 2, 'New app spotted',
         'Detected {process}. Remember: do as I say, not as I sudo.',
         'dialog-information', 'low'),

        # day_reset - new day begins
        ('day_reset', 0, 'Good morning!',
         'New day, fresh start! You have {time_limit} minutes of gaming time today.',
         'dialog-information', 'normal'),

        # mode_change - daemon mode changed
        ('mode_change', 0, 'Mode changed',
         'Switching to {mode} mode.',
         'dialog-information', 'normal'),

        # strict_warning - unknown process in strict mode
        ('strict_warning', 0, 'Unknown application',
         'I do not recognize {process}. It will be closed in {grace_seconds} seconds unless approved.',
         'dialog-warning', 'critical'),
    ]

    for intention, variant, title, body, icon, urgency in templates:
        conn.execute("""
            INSERT INTO message_templates (intention, variant, title, body, icon, urgency, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (intention, variant, title, body, icon, urgency, now))


@contextmanager
def get_connection(db_path: str = DEFAULT_DB_PATH):
    """Context manager for database connections."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class ActivityDB:
    """Database interface for activity tracking."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        init_db(db_path)
        migrate_db(db_path)

    def log_event(self, user: str, event_type: str, app: str = None,
                  category: str = None, details: str = None, pid: int = None):
        """Log an activity event."""
        with get_connection(self.db_path) as conn:
            conn.execute("""
                INSERT INTO events (timestamp, user, event_type, app, category, details, pid)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (datetime.now().isoformat(), user, event_type, app, category, details, pid))

    def start_session(self, user: str, app: str, category: str = None,
                      pid: int = None) -> int:
        """Record session start, return session ID."""
        with get_connection(self.db_path) as conn:
            cursor = conn.execute("""
                INSERT INTO sessions (user, app, category, pid, start_time)
                VALUES (?, ?, ?, ?, ?)
            """, (user, app, category, pid, datetime.now().isoformat()))
            return cursor.lastrowid

    def end_session(self, session_id: int = None, pid: int = None,
                    user: str = None, reason: str = "unknown"):
        """Record session end by session_id or by pid+user."""
        end_time = datetime.now().isoformat()

        with get_connection(self.db_path) as conn:
            # Find the session
            if session_id:
                row = conn.execute(
                    "SELECT id, start_time FROM sessions WHERE id = ? AND end_time IS NULL",
                    (session_id,)
                ).fetchone()
            elif pid and user:
                row = conn.execute(
                    "SELECT id, start_time FROM sessions WHERE pid = ? AND user = ? AND end_time IS NULL",
                    (pid, user)
                ).fetchone()
            else:
                return

            if row:
                start = datetime.fromisoformat(row['start_time'])
                duration = int((datetime.now() - start).total_seconds())
                conn.execute("""
                    UPDATE sessions
                    SET end_time = ?, duration = ?, end_reason = ?
                    WHERE id = ?
                """, (end_time, duration, reason, row['id']))

    def update_daily_summary(self, user: str, gaming_seconds: int = 0,
                             total_seconds: int = 0, warnings: int = 0,
                             enforcements: int = 0):
        """Update or create daily summary for user."""
        today = date.today().isoformat()

        with get_connection(self.db_path) as conn:
            conn.execute("""
                INSERT INTO daily_summary (date, user, gaming_time, total_time, warnings_sent, enforcements)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(date, user) DO UPDATE SET
                    gaming_time = gaming_time + excluded.gaming_time,
                    total_time = total_time + excluded.total_time,
                    warnings_sent = warnings_sent + excluded.warnings_sent,
                    enforcements = enforcements + excluded.enforcements
            """, (today, user, gaming_seconds, total_seconds, warnings, enforcements))

    def update_hourly_activity(self, user: str, gaming_seconds: int = 0,
                               total_seconds: int = 0):
        """Update or create hourly activity for user."""
        today = date.today().isoformat()
        hour = datetime.now().hour

        with get_connection(self.db_path) as conn:
            conn.execute("""
                INSERT INTO hourly_activity (date, hour, user, gaming_seconds, total_seconds)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(date, hour, user) DO UPDATE SET
                    gaming_seconds = gaming_seconds + excluded.gaming_seconds,
                    total_seconds = total_seconds + excluded.total_seconds
            """, (today, hour, user, gaming_seconds, total_seconds))

    def get_hourly_activity(self, user: str, days: int = 7) -> list[dict]:
        """Get hourly activity for user over the last N days."""
        cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
        with get_connection(self.db_path) as conn:
            rows = conn.execute("""
                SELECT date, hour, gaming_seconds, total_seconds
                FROM hourly_activity
                WHERE user = ? AND date >= ?
                ORDER BY date, hour
            """, (user, cutoff)).fetchall()
            return [dict(row) for row in rows]

    def increment_session_count(self, user: str):
        """Increment session count for today."""
        today = date.today().isoformat()

        with get_connection(self.db_path) as conn:
            conn.execute("""
                INSERT INTO daily_summary (date, user, session_count)
                VALUES (?, ?, 1)
                ON CONFLICT(date, user) DO UPDATE SET
                    session_count = session_count + 1
            """, (today, user))

    def get_daily_summary(self, user: str, day: str = None) -> Optional[dict]:
        """Get daily summary for user."""
        if day is None:
            day = date.today().isoformat()

        with get_connection(self.db_path) as conn:
            row = conn.execute("""
                SELECT * FROM daily_summary WHERE user = ? AND date = ?
            """, (user, day)).fetchone()

            if row:
                return dict(row)
        return None

    def get_weekly_summary(self, user: str) -> list[dict]:
        """Get last 7 days of summaries."""
        with get_connection(self.db_path) as conn:
            rows = conn.execute("""
                SELECT * FROM daily_summary
                WHERE user = ?
                ORDER BY date DESC
                LIMIT 7
            """, (user,)).fetchall()
            return [dict(row) for row in rows]

    def get_history(self, user: str, days: int = 7) -> list[dict]:
        """Get daily summaries for the last N days."""
        with get_connection(self.db_path) as conn:
            rows = conn.execute("""
                SELECT * FROM daily_summary
                WHERE user = ?
                ORDER BY date DESC
                LIMIT ?
            """, (user, days)).fetchall()
            return [dict(row) for row in rows]

    def get_sessions_range(self, user: str, days: int = 1) -> list[dict]:
        """Get sessions from the last N days."""
        cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
        with get_connection(self.db_path) as conn:
            rows = conn.execute("""
                SELECT * FROM sessions
                WHERE user = ? AND date(start_time) >= ?
                ORDER BY start_time DESC
            """, (user, cutoff)).fetchall()
            return [dict(row) for row in rows]

    def get_top_apps(self, user: str, days: int = 7, limit: int = 5) -> list[dict]:
        """Get top apps by session count over last N days."""
        cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
        with get_connection(self.db_path) as conn:
            rows = conn.execute("""
                SELECT app,
                       COUNT(*) as session_count,
                       SUM(COALESCE(duration, 0)) as total_duration
                FROM sessions
                WHERE user = ? AND date(start_time) >= ?
                GROUP BY app
                ORDER BY session_count DESC
                LIMIT ?
            """, (user, cutoff, limit)).fetchall()
            return [dict(row) for row in rows]

    def get_recent_events(self, user: str, limit: int = 50) -> list[dict]:
        """Get recent events for user."""
        with get_connection(self.db_path) as conn:
            rows = conn.execute("""
                SELECT * FROM events
                WHERE user = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (user, limit)).fetchall()
            return [dict(row) for row in rows]

    def get_sessions_for_day(self, user: str, day: str = None) -> list[dict]:
        """Get all sessions for a specific day."""
        if day is None:
            day = date.today().isoformat()

        with get_connection(self.db_path) as conn:
            rows = conn.execute("""
                SELECT * FROM sessions
                WHERE user = ? AND date(start_time) = ?
                ORDER BY start_time
            """, (user, day)).fetchall()
            return [dict(row) for row in rows]

    def get_time_used_today(self, user: str) -> tuple[int, int]:
        """Get (total_time, gaming_time) used today in seconds."""
        summary = self.get_daily_summary(user)
        if summary:
            return summary['total_time'], summary['gaming_time']
        return 0, 0

    # --- Process Pattern Management ---

    def add_pattern(self, pattern: str, name: str, category: str,
                    cpu_threshold: float = 5.0, notes: str = None,
                    owner: str = None, monitor_state: str = 'active') -> int:
        """Add a new process pattern."""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            cursor = conn.execute("""
                INSERT INTO process_patterns
                    (pattern, name, category, monitor_state, owner,
                     cpu_threshold, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (pattern, name, category, monitor_state, owner,
                  cpu_threshold, notes, now, now))
            return cursor.lastrowid

    def get_patterns(self, category: str = None, enabled_only: bool = True,
                     include_all_states: bool = False, owner: str = None) -> list[dict]:
        """Get process patterns, optionally filtered.

        By default, only returns 'active' patterns. Set include_all_states=True
        to get patterns in any state.
        """
        with get_connection(self.db_path) as conn:
            conditions = []
            params = []

            if category:
                conditions.append("category = ?")
                params.append(category)

            if enabled_only:
                conditions.append("enabled = 1")

            if not include_all_states:
                conditions.append("monitor_state = 'active'")

            if owner:
                conditions.append("(owner = ? OR owner IS NULL)")
                params.append(owner)

            where = " AND ".join(conditions) if conditions else "1=1"
            # Order: user-specific patterns first, then global catchalls
            query = f"""SELECT * FROM process_patterns WHERE {where}
                        ORDER BY CASE WHEN owner IS NOT NULL THEN 0 ELSE 1 END,
                                 monitor_state, name"""

            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def get_all_patterns(self) -> list[dict]:
        """Get ALL patterns regardless of state (for CLI display)."""
        with get_connection(self.db_path) as conn:
            rows = conn.execute("""
                SELECT * FROM process_patterns
                ORDER BY monitor_state, owner, name
            """).fetchall()
            return [dict(row) for row in rows]

    def update_pattern(self, pattern_id: int, **kwargs):
        """Update a pattern by ID."""
        allowed = {'pattern', 'name', 'category', 'enabled', 'cpu_threshold', 'notes'}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return

        updates['updated_at'] = datetime.now().isoformat()
        set_clause = ', '.join(f"{k} = ?" for k in updates.keys())

        with get_connection(self.db_path) as conn:
            conn.execute(
                f"UPDATE process_patterns SET {set_clause} WHERE id = ?",
                (*updates.values(), pattern_id)
            )

    def delete_pattern(self, pattern_id: int):
        """Delete a pattern by ID."""
        with get_connection(self.db_path) as conn:
            conn.execute("DELETE FROM process_patterns WHERE id = ?", (pattern_id,))

    def seed_default_patterns(self):
        """Seed database with default patterns if empty."""
        existing = self.get_patterns(enabled_only=False, include_all_states=True)
        if existing:
            return  # Already has patterns

        defaults = [
            # Launchers (detected but not counted)
            ("^steam$", "Steam Launcher", "launcher", 0, "Steam client sitting idle"),
            ("minecraft-launcher", "Minecraft Launcher", "launcher", 0, "MC launcher before game starts"),

            # Gaming (counted against quota)
            (r"java.*minecraft", "Minecraft", "gaming", 5.0, "Java Minecraft game"),
            (r"gamescope", "Steam Game", "gaming", 5.0, "Steam Deck / gamescope wrapper"),
            (r"\.exe$", "Proton Game", "gaming", 10.0, "Windows games via Proton"),
            (r"retroarch", "RetroArch", "gaming", 5.0, "Emulator frontend"),
        ]

        for pattern, name, category, cpu_thresh, notes in defaults:
            self.add_pattern(pattern, name, category, cpu_thresh, notes)

    # --- Discovery & Statistics ---

    def get_discovery_config(self) -> dict:
        """Get discovery configuration as a dict."""
        with get_connection(self.db_path) as conn:
            rows = conn.execute("SELECT key, value FROM discovery_config").fetchall()
            config = {row['key']: row['value'] for row in rows}
            # Convert to appropriate types
            return {
                'enabled': config.get('enabled', '1') == '1',
                'cpu_threshold': float(config.get('cpu_threshold', '25')),
                'sample_window_seconds': int(config.get('sample_window_seconds', '30')),
                'min_samples': int(config.get('min_samples', '3')),
            }

    def set_discovery_config(self, key: str, value: str):
        """Update a discovery config value."""
        with get_connection(self.db_path) as conn:
            conn.execute("""
                UPDATE discovery_config SET value = ? WHERE key = ?
            """, (str(value), key))

    # --- Daemon Configuration ---

    def get_daemon_config(self) -> dict:
        """Get daemon configuration as a dict."""
        with get_connection(self.db_path) as conn:
            rows = conn.execute("SELECT key, value FROM daemon_config").fetchall()
            config = {row['key']: row['value'] for row in rows}
            return {
                'mode': config.get('mode', 'normal'),
                'strict_grace_seconds': int(config.get('strict_grace_seconds', '30')),
            }

    def set_daemon_config(self, key: str, value: str):
        """Update a daemon config value."""
        with get_connection(self.db_path) as conn:
            conn.execute("""
                INSERT INTO daemon_config (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = ?
            """, (key, str(value), str(value)))

    def get_daemon_mode(self) -> str:
        """Get current daemon mode (normal, passthrough, strict)."""
        return self.get_daemon_config()['mode']

    def set_daemon_mode(self, mode: str):
        """Set daemon mode. Valid values: normal, passthrough, strict."""
        if mode not in ('normal', 'passthrough', 'strict'):
            raise ValueError(f"Invalid mode: {mode}. Must be normal, passthrough, or strict.")
        self.set_daemon_config('mode', mode)

    def discover_pattern(self, pattern: str, name: str, owner: str,
                         cmdline: str = None, cpu_threshold: float = 5.0,
                         category: str = None, state: str = 'discovered') -> int:
        """Create a new discovered pattern."""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            cursor = conn.execute("""
                INSERT INTO process_patterns
                    (pattern, name, category, monitor_state, owner, enabled,
                     cpu_threshold, discovered_cmdline, created_at, updated_at, last_seen)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            """, (pattern, name, category, state, owner, cpu_threshold, cmdline, now, now, now))
            return cursor.lastrowid

    def get_pattern_by_name_and_owner(self, name: str, owner: str) -> Optional[dict]:
        """Find a pattern by name and owner (for discovery dedup)."""
        with get_connection(self.db_path) as conn:
            row = conn.execute("""
                SELECT * FROM process_patterns
                WHERE name = ? AND (owner = ? OR owner IS NULL)
            """, (name, owner)).fetchone()
            return dict(row) if row else None

    def get_patterns_by_state(self, state: str, owner: str = None) -> list[dict]:
        """Get patterns filtered by monitor_state."""
        with get_connection(self.db_path) as conn:
            if owner:
                rows = conn.execute("""
                    SELECT * FROM process_patterns
                    WHERE monitor_state = ? AND (owner = ? OR owner IS NULL)
                    ORDER BY total_runtime_seconds DESC
                """, (state, owner)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM process_patterns
                    WHERE monitor_state = ?
                    ORDER BY total_runtime_seconds DESC
                """, (state,)).fetchall()
            return [dict(row) for row in rows]

    def set_pattern_state(self, pattern_id: int, state: str,
                          category: str = None, name: str = None):
        """Change a pattern's monitor state (promote, ignore, disallow)."""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            updates = ["monitor_state = ?", "updated_at = ?"]
            params = [state, now]

            if category:
                updates.append("category = ?")
                params.append(category)
            if name:
                updates.append("name = ?")
                params.append(name)

            params.append(pattern_id)
            conn.execute(
                f"UPDATE process_patterns SET {', '.join(updates)} WHERE id = ?",
                params
            )

    def record_pid_seen(self, pattern_id: int, pid: int) -> bool:
        """Record that we've seen a PID for this pattern. Returns True if new."""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            try:
                conn.execute("""
                    INSERT INTO seen_pids (pattern_id, pid, first_seen)
                    VALUES (?, ?, ?)
                """, (pattern_id, pid, now))
                # New PID - increment counter
                conn.execute("""
                    UPDATE process_patterns
                    SET unique_pid_count = unique_pid_count + 1, last_seen = ?
                    WHERE id = ?
                """, (now, pattern_id))
                return True
            except sqlite3.IntegrityError:
                # Already seen this PID
                conn.execute("""
                    UPDATE process_patterns SET last_seen = ? WHERE id = ?
                """, (now, pattern_id))
                return False

    def add_runtime(self, pattern_id: int, seconds: int):
        """Add runtime seconds to a pattern's total."""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            conn.execute("""
                UPDATE process_patterns
                SET total_runtime_seconds = total_runtime_seconds + ?,
                    last_seen = ?, updated_at = ?
                WHERE id = ?
            """, (seconds, now, now, pattern_id))

    def cleanup_seen_pids(self, days: int = 7):
        """Remove old PID records (PIDs get recycled)."""
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with get_connection(self.db_path) as conn:
            conn.execute("DELETE FROM seen_pids WHERE first_seen < ?", (cutoff,))

    # --- User Limits Management ---

    def get_user_limits(self, user: str) -> Optional[dict]:
        """Get limits for a user."""
        with get_connection(self.db_path) as conn:
            row = conn.execute("""
                SELECT * FROM user_limits WHERE user = ?
            """, (user,)).fetchone()
            return dict(row) if row else None

    def set_user_limits(self, user: str, **kwargs) -> int:
        """Set or update user limits.

        Accepts modern columns directly (enabled, daily_total, schedule, daily_limits)
        and legacy convenience kwargs that get converted:
          - gaming_limit -> daily_limits (same value for all 7 days)
          - weekday_start/end + weekend_start/end -> schedule string
        """
        now = datetime.now().isoformat()
        existing = self.get_user_limits(user)

        # Convert legacy kwargs to modern columns
        if 'gaming_limit' in kwargs:
            gl = kwargs.pop('gaming_limit')
            if 'daily_limits' not in kwargs:
                kwargs['daily_limits'] = format_daily_limits([gl] * 7)

        time_range_keys = {'weekday_start', 'weekday_end', 'weekend_start', 'weekend_end'}
        time_ranges = {k: kwargs.pop(k) for k in time_range_keys if k in kwargs}
        if time_ranges and 'schedule' not in kwargs:
            # Fill in defaults for any missing range values
            if existing:
                # For existing users, only override the ranges that were passed
                wd_s = time_ranges.get('weekday_start', '16:00')
                wd_e = time_ranges.get('weekday_end', '21:00')
                we_s = time_ranges.get('weekend_start', '09:00')
                we_e = time_ranges.get('weekend_end', '22:00')
            else:
                wd_s = time_ranges.get('weekday_start', '16:00')
                wd_e = time_ranges.get('weekday_end', '21:00')
                we_s = time_ranges.get('weekend_start', '09:00')
                we_e = time_ranges.get('weekend_end', '22:00')
            kwargs['schedule'] = schedule_from_ranges(wd_s, wd_e, we_s, we_e)

        allowed = {'enabled', 'daily_total', 'schedule', 'daily_limits'}
        updates = {k: v for k, v in kwargs.items() if k in allowed}

        with get_connection(self.db_path) as conn:
            if existing:
                updates['updated_at'] = now
                set_clause = ', '.join(f"{k} = ?" for k in updates.keys())
                conn.execute(
                    f"UPDATE user_limits SET {set_clause} WHERE user = ?",
                    (*updates.values(), user)
                )
                return existing['id']
            else:
                # New user: ensure schedule and daily_limits have values
                updates.setdefault('schedule', schedule_from_ranges(
                    '16:00', '21:00', '09:00', '22:00'))
                updates.setdefault('daily_limits', DEFAULT_DAILY_LIMITS)
                updates.setdefault('enabled', 1)
                updates.setdefault('daily_total', 180)
                updates['user'] = user
                updates['created_at'] = now
                updates['updated_at'] = now
                columns = ', '.join(updates.keys())
                placeholders = ', '.join('?' * len(updates))
                conn.execute(
                    f"INSERT INTO user_limits ({columns}) VALUES ({placeholders})",
                    tuple(updates.values())
                )
                return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_schedule(self, user: str) -> str:
        """Get 168-char schedule string for user."""
        limits = self.get_user_limits(user)
        if not limits:
            return DEFAULT_SCHEDULE
        return limits.get('schedule') or DEFAULT_SCHEDULE

    def set_schedule(self, user: str, schedule: str):
        """Write a 168-char schedule string."""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE user_limits SET schedule = ?, updated_at = ? WHERE user = ?",
                (schedule, now, user)
            )

    def get_daily_limits(self, user: str) -> list[int]:
        """Get per-day gaming limits (7 ints, Mon-Sun, in minutes)."""
        limits = self.get_user_limits(user)
        if not limits:
            return [120] * 7
        dl = limits.get('daily_limits')
        if dl:
            return parse_daily_limits(dl)
        return [120] * 7

    def set_daily_limits(self, user: str, daily_limits: list[int]):
        """Write per-day gaming limits (7 ints, Mon-Sun, in minutes)."""
        now = datetime.now().isoformat()
        dl_str = format_daily_limits(daily_limits)
        with get_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE user_limits SET daily_limits = ?, updated_at = ? WHERE user = ?",
                (dl_str, now, user)
            )

    def get_all_monitored_users(self) -> list[str]:
        """Get list of all monitored users."""
        with get_connection(self.db_path) as conn:
            rows = conn.execute("""
                SELECT user FROM user_limits WHERE enabled = 1
            """).fetchall()
            return [row['user'] for row in rows]

    # --- Maintenance & Retention ---

    def cleanup_old_data(self, events_days: int = 30, sessions_days: int = 90,
                         keep_summaries: bool = True) -> dict:
        """Delete old data beyond retention period.

        Args:
            events_days: Delete events older than this many days
            sessions_days: Delete sessions older than this many days
            keep_summaries: If True, never delete daily summaries

        Returns:
            Dict with counts of deleted rows
        """
        from datetime import timedelta

        events_cutoff = (datetime.now() - timedelta(days=events_days)).isoformat()
        sessions_cutoff = (datetime.now() - timedelta(days=sessions_days)).isoformat()

        deleted = {}

        with get_connection(self.db_path) as conn:
            # Delete old events
            cursor = conn.execute("""
                DELETE FROM events WHERE timestamp < ?
            """, (events_cutoff,))
            deleted['events'] = cursor.rowcount

            # Delete old sessions
            cursor = conn.execute("""
                DELETE FROM sessions WHERE start_time < ?
            """, (sessions_cutoff,))
            deleted['sessions'] = cursor.rowcount

            # Optionally delete old summaries (usually want to keep these)
            if not keep_summaries:
                summaries_cutoff = (datetime.now() - timedelta(days=365)).isoformat()
                cursor = conn.execute("""
                    DELETE FROM daily_summary WHERE date < ?
                """, (summaries_cutoff,))
                deleted['summaries'] = cursor.rowcount

        return deleted

    def vacuum(self):
        """Compact the database file after deletions."""
        # VACUUM can't run inside a transaction
        conn = sqlite3.connect(self.db_path)
        conn.execute("VACUUM")
        conn.close()

    def get_db_stats(self) -> dict:
        """Get database statistics for monitoring."""
        import os

        stats = {
            'file_size_mb': os.path.getsize(self.db_path) / (1024 * 1024)
        }

        with get_connection(self.db_path) as conn:
            stats['events_count'] = conn.execute(
                "SELECT COUNT(*) FROM events"
            ).fetchone()[0]

            stats['sessions_count'] = conn.execute(
                "SELECT COUNT(*) FROM sessions"
            ).fetchone()[0]

            stats['summaries_count'] = conn.execute(
                "SELECT COUNT(*) FROM daily_summary"
            ).fetchone()[0]

            stats['patterns_count'] = conn.execute(
                "SELECT COUNT(*) FROM process_patterns"
            ).fetchone()[0]

            # Oldest event
            oldest = conn.execute(
                "SELECT MIN(timestamp) FROM events"
            ).fetchone()[0]
            stats['oldest_event'] = oldest

        return stats

    def maintenance(self, events_days: int = 30, sessions_days: int = 90,
                    message_log_days: int = 7) -> dict:
        """Run full maintenance cycle: cleanup + vacuum.

        Call this periodically (e.g., daily via cron or on daemon startup).
        """
        result = {
            'before': self.get_db_stats(),
            'deleted': self.cleanup_old_data(events_days, sessions_days),
        }

        # Also clean up message log
        result['deleted']['message_log'] = self.cleanup_message_log(message_log_days)

        self.vacuum()

        result['after'] = self.get_db_stats()
        return result

    # --- Pattern Notes ---

    def set_pattern_notes(self, pattern_id: int, notes: str):
        """Set notes on a pattern."""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            conn.execute("""
                UPDATE process_patterns
                SET notes = ?, updated_at = ?
                WHERE id = ?
            """, (notes, now, pattern_id))

    def get_pattern_by_id(self, pattern_id: int) -> Optional[dict]:
        """Get a pattern by ID."""
        with get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM process_patterns WHERE id = ?", (pattern_id,)
            ).fetchone()
            return dict(row) if row else None

    # --- Message Templates ---

    def get_templates(self, intention: str, enabled_only: bool = True) -> list[dict]:
        """Get all templates for an intention."""
        with get_connection(self.db_path) as conn:
            if enabled_only:
                rows = conn.execute("""
                    SELECT * FROM message_templates
                    WHERE intention = ? AND enabled = 1
                    ORDER BY variant
                """, (intention,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM message_templates
                    WHERE intention = ?
                    ORDER BY variant
                """, (intention,)).fetchall()
            return [dict(row) for row in rows]

    def get_template(self, intention: str, variant: int = 0) -> Optional[dict]:
        """Get a specific template by intention and variant."""
        with get_connection(self.db_path) as conn:
            row = conn.execute("""
                SELECT * FROM message_templates
                WHERE intention = ? AND variant = ? AND enabled = 1
            """, (intention, variant)).fetchone()
            return dict(row) if row else None

    def get_random_template(self, intention: str) -> Optional[dict]:
        """Get a random enabled template for an intention."""
        with get_connection(self.db_path) as conn:
            row = conn.execute("""
                SELECT * FROM message_templates
                WHERE intention = ? AND enabled = 1
                ORDER BY RANDOM()
                LIMIT 1
            """, (intention,)).fetchone()
            return dict(row) if row else None

    def get_all_templates(self) -> list[dict]:
        """Get all templates for listing."""
        with get_connection(self.db_path) as conn:
            rows = conn.execute("""
                SELECT * FROM message_templates
                ORDER BY intention, variant
            """).fetchall()
            return [dict(row) for row in rows]

    def add_template(self, intention: str, title: str, body: str,
                     variant: int = None, icon: str = "dialog-information",
                     urgency: str = "normal") -> int:
        """Add a new message template."""
        now = datetime.now().isoformat()

        with get_connection(self.db_path) as conn:
            # Auto-assign variant if not specified
            if variant is None:
                result = conn.execute("""
                    SELECT COALESCE(MAX(variant), -1) + 1 FROM message_templates
                    WHERE intention = ?
                """, (intention,)).fetchone()
                variant = result[0]

            cursor = conn.execute("""
                INSERT INTO message_templates
                    (intention, variant, title, body, icon, urgency, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (intention, variant, title, body, icon, urgency, now))
            return cursor.lastrowid

    def update_template(self, template_id: int, **kwargs):
        """Update a template."""
        allowed = {'title', 'body', 'icon', 'urgency', 'enabled'}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return

        set_clause = ', '.join(f"{k} = ?" for k in updates.keys())
        with get_connection(self.db_path) as conn:
            conn.execute(
                f"UPDATE message_templates SET {set_clause} WHERE id = ?",
                (*updates.values(), template_id)
            )

    def delete_template(self, template_id: int):
        """Delete a template."""
        with get_connection(self.db_path) as conn:
            conn.execute("DELETE FROM message_templates WHERE id = ?", (template_id,))

    # --- Message Log ---

    def log_message(self, user: str, intention: str, template_id: int,
                    rendered_title: str, rendered_body: str,
                    notification_id: int = 0, backend: str = None) -> int:
        """Log a sent message."""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            cursor = conn.execute("""
                INSERT INTO message_log
                    (timestamp, user, intention, template_id,
                     rendered_title, rendered_body, notification_id, backend)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (now, user, intention, template_id,
                  rendered_title, rendered_body, notification_id, backend))
            return cursor.lastrowid

    def get_recent_messages(self, user: str = None, limit: int = 50) -> list[dict]:
        """Get recent message log entries."""
        with get_connection(self.db_path) as conn:
            if user:
                rows = conn.execute("""
                    SELECT * FROM message_log
                    WHERE user = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, (user, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM message_log
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, (limit,)).fetchall()
            return [dict(row) for row in rows]

    def cleanup_message_log(self, days: int = 7) -> int:
        """Delete message_log entries older than N days."""
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with get_connection(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM message_log WHERE timestamp < ?", (cutoff,)
            )
            return cursor.rowcount

    # --- User State (for message router) ---

    def get_user_state(self, user: str) -> Optional[dict]:
        """Get current user state from daily_summary."""
        today = date.today().isoformat()
        with get_connection(self.db_path) as conn:
            row = conn.execute("""
                SELECT state, gaming_active, gaming_started_at, last_poll_at,
                       warned_30, warned_15, warned_5,
                       gaming_time, total_time
                FROM daily_summary
                WHERE user = ? AND date = ?
            """, (user, today)).fetchone()
            if row:
                return dict(row)
            return None

    def update_user_state(self, user: str, **kwargs):
        """Update user state in daily_summary (upsert)."""
        today = date.today().isoformat()
        allowed = {'state', 'gaming_active', 'gaming_started_at', 'last_poll_at',
                   'warned_30', 'warned_15', 'warned_5', 'gaming_time', 'total_time'}
        updates = {k: v for k, v in kwargs.items() if k in allowed}

        if not updates:
            return

        with get_connection(self.db_path) as conn:
            # Check if row exists
            exists = conn.execute("""
                SELECT 1 FROM daily_summary WHERE user = ? AND date = ?
            """, (user, today)).fetchone()

            if exists:
                set_clause = ', '.join(f"{k} = ?" for k in updates.keys())
                conn.execute(
                    f"UPDATE daily_summary SET {set_clause} WHERE user = ? AND date = ?",
                    (*updates.values(), user, today)
                )
            else:
                # Insert new row with defaults
                updates['date'] = today
                updates['user'] = user
                columns = ', '.join(updates.keys())
                placeholders = ', '.join('?' * len(updates))
                conn.execute(
                    f"INSERT INTO daily_summary ({columns}) VALUES ({placeholders})",
                    tuple(updates.values())
                )

    # --- Browser Patterns ---

    def add_browser_pattern(self, domain: str, name: str, category: str,
                            browser: str, owner: str = None,
                            monitor_state: str = 'active') -> int:
        """Add a browser domain pattern."""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            cursor = conn.execute("""
                INSERT INTO process_patterns
                    (pattern, name, category, pattern_type, browser,
                     monitor_state, owner, cpu_threshold, created_at, updated_at)
                VALUES (?, ?, ?, 'browser_domain', ?, ?, ?, 0, ?, ?)
            """, (domain, name, category, browser, monitor_state, owner, now, now))
            return cursor.lastrowid

    def get_browser_patterns(self, owner: str = None,
                             include_all_states: bool = False) -> list[dict]:
        """Get browser domain patterns."""
        with get_connection(self.db_path) as conn:
            conditions = ["pattern_type = 'browser_domain'"]
            params = []

            if not include_all_states:
                conditions.append("monitor_state = 'active'")

            if owner:
                conditions.append("(owner = ? OR owner IS NULL)")
                params.append(owner)

            where = " AND ".join(conditions)
            rows = conn.execute(
                f"SELECT * FROM process_patterns WHERE {where} ORDER BY name",
                params
            ).fetchall()
            return [dict(row) for row in rows]

    def get_pattern_by_domain_and_owner(self, domain: str, owner: str) -> Optional[dict]:
        """Find a browser pattern by domain and owner."""
        with get_connection(self.db_path) as conn:
            row = conn.execute("""
                SELECT * FROM process_patterns
                WHERE pattern = ? AND pattern_type = 'browser_domain'
                  AND (owner = ? OR owner IS NULL)
            """, (domain, owner)).fetchone()
            return dict(row) if row else None

    def discover_browser_domain(self, domain: str, browser: str, owner: str) -> int:
        """Create a discovered browser domain pattern."""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            cursor = conn.execute("""
                INSERT INTO process_patterns
                    (pattern, name, category, pattern_type, browser,
                     monitor_state, owner, enabled, cpu_threshold,
                     created_at, updated_at, last_seen)
                VALUES (?, ?, NULL, 'browser_domain', ?, 'discovered', ?, 1, 0, ?, ?, ?)
            """, (domain, domain, browser, owner, now, now, now))
            return cursor.lastrowid

        if not updates:
            return

        with get_connection(self.db_path) as conn:
            # Check if row exists
            exists = conn.execute("""
                SELECT 1 FROM daily_summary WHERE user = ? AND date = ?
            """, (user, today)).fetchone()

            if exists:
                set_clause = ', '.join(f"{k} = ?" for k in updates.keys())
                conn.execute(
                    f"UPDATE daily_summary SET {set_clause} WHERE user = ? AND date = ?",
                    (*updates.values(), user, today)
                )
            else:
                # Insert new row with defaults
                updates['date'] = today
                updates['user'] = user
                columns = ', '.join(updates.keys())
                placeholders = ', '.join('?' * len(updates))
                conn.execute(
                    f"INSERT INTO daily_summary ({columns}) VALUES ({placeholders})",
                    tuple(updates.values())
                )
