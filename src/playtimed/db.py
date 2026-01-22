"""
SQLite database for playtimed activity tracking.

Stores structured activity data for long-term metrics and analytics.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = "/var/lib/playtimed/playtimed.db"


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
            CREATE TABLE IF NOT EXISTS process_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT NOT NULL,
                name TEXT NOT NULL,
                category TEXT,  -- 'gaming', 'launcher', 'productive' (NULL for discovered)
                monitor_state TEXT NOT NULL DEFAULT 'active',
                owner TEXT,  -- user who owns this process (NULL = all users)
                enabled INTEGER NOT NULL DEFAULT 1,
                cpu_threshold REAL DEFAULT 5.0,  -- minimum CPU% to count as active

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
                gaming_limit INTEGER NOT NULL DEFAULT 120,  -- minutes
                weekday_start TEXT DEFAULT '16:00',
                weekday_end TEXT DEFAULT '21:00',
                weekend_start TEXT DEFAULT '09:00',
                weekend_end TEXT DEFAULT '22:00',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- Indexes for common queries
            CREATE INDEX IF NOT EXISTS idx_events_user_date
                ON events(user, timestamp);
            CREATE INDEX IF NOT EXISTS idx_daily_user_date
                ON daily_summary(user, date);
            CREATE INDEX IF NOT EXISTS idx_sessions_user_date
                ON sessions(user, start_time);
            CREATE INDEX IF NOT EXISTS idx_patterns_category
                ON process_patterns(category, enabled);
            CREATE INDEX IF NOT EXISTS idx_patterns_state
                ON process_patterns(monitor_state);
            CREATE INDEX IF NOT EXISTS idx_patterns_owner
                ON process_patterns(owner);

            -- Daemon configuration (mode, etc.)
            CREATE TABLE IF NOT EXISTS daemon_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                description TEXT
            );

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
            query = f"SELECT * FROM process_patterns WHERE {where} ORDER BY monitor_state, name"

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
                         cmdline: str = None, cpu_threshold: float = 5.0) -> int:
        """Create a new discovered pattern (state='discovered')."""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            cursor = conn.execute("""
                INSERT INTO process_patterns
                    (pattern, name, category, monitor_state, owner, enabled,
                     cpu_threshold, discovered_cmdline, created_at, updated_at, last_seen)
                VALUES (?, ?, NULL, 'discovered', ?, 1, ?, ?, ?, ?, ?)
            """, (pattern, name, owner, cpu_threshold, cmdline, now, now, now))
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

    def set_pattern_state(self, pattern_id: int, state: str, category: str = None):
        """Change a pattern's monitor state (promote, ignore, disallow)."""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            if category:
                conn.execute("""
                    UPDATE process_patterns
                    SET monitor_state = ?, category = ?, updated_at = ?
                    WHERE id = ?
                """, (state, category, now, pattern_id))
            else:
                conn.execute("""
                    UPDATE process_patterns
                    SET monitor_state = ?, updated_at = ?
                    WHERE id = ?
                """, (state, now, pattern_id))

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
        """Set or update user limits."""
        now = datetime.now().isoformat()
        existing = self.get_user_limits(user)

        allowed = {'enabled', 'daily_total', 'gaming_limit',
                   'weekday_start', 'weekday_end', 'weekend_start', 'weekend_end'}
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
                conn.execute("""
                    INSERT INTO user_limits
                        (user, enabled, daily_total, gaming_limit,
                         weekday_start, weekday_end, weekend_start, weekend_end,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    user,
                    updates.get('enabled', 1),
                    updates.get('daily_total', 180),
                    updates.get('gaming_limit', 120),
                    updates.get('weekday_start', '16:00'),
                    updates.get('weekday_end', '21:00'),
                    updates.get('weekend_start', '09:00'),
                    updates.get('weekend_end', '22:00'),
                    now, now
                ))
                return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

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

    def maintenance(self, events_days: int = 30, sessions_days: int = 90) -> dict:
        """Run full maintenance cycle: cleanup + vacuum.

        Call this periodically (e.g., daily via cron or on daemon startup).
        """
        result = {
            'before': self.get_db_stats(),
            'deleted': self.cleanup_old_data(events_days, sessions_days),
        }

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
