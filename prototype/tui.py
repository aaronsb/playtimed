#!/usr/bin/env python3
"""
playtimed TUI - Screen time management interface

Run with: sudo python tui.py     # Real data from playtimed
          python tui.py --mock   # Mock data for development
"""

import os
import sqlite3
import subprocess
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.screen import Screen, ModalScreen
from textual.widgets import (
    Header, Footer, Static, Label, Button, DataTable,
    TabbedContent, TabPane, Input, Select, ProgressBar,
    Rule, Checkbox, ListItem, ListView, OptionList, ContentSwitcher
)
from textual.widgets.option_list import Option
from textual.message import Message
from textual.reactive import reactive
from textual import events, on


# =============================================================================
# Mock Data Layer - Replace with real playtimed interface later
# =============================================================================

class AppState(Enum):
    DISCOVERED = "discovered"
    TRACKED = "tracked"
    IGNORED = "ignored"
    BLOCKED = "blocked"

class Category(Enum):
    GAMING = "gaming"
    EDUCATIONAL = "educational"
    SOCIAL = "social"
    PRODUCTIVITY = "productivity"
    ENTERTAINMENT = "entertainment"
    UNCATEGORIZED = "-"


@dataclass
class AppPattern:
    id: int
    name: str
    owner: str  # "*" for global, username for user-specific
    state: AppState
    category: Category
    runtime_seconds: int
    last_seen: datetime
    pattern_type: str = "process"  # process, browser:chrome, etc.

    @property
    def runtime_display(self) -> str:
        hours, remainder = divmod(self.runtime_seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h{minutes}m"
        return f"{minutes}m"


@dataclass
class User:
    username: str
    enabled: bool = True
    gaming_limit: int = 120  # minutes
    daily_total: int = 180   # minutes
    weekday_start: str = "16:00"
    weekday_end: str = "21:00"
    weekend_start: str = "09:00"
    weekend_end: str = "22:00"

    # Current session (mock)
    gaming_today: int = 0  # minutes used
    total_today: int = 0   # minutes used


@dataclass
class MockData:
    """Container for all mock data - edit this to experiment"""
    users: list[User] = field(default_factory=list)
    apps: list[AppPattern] = field(default_factory=list)

    @classmethod
    def create_sample(cls) -> "MockData":
        """Generate sample data mimicking real playtimed"""
        now = datetime.now()

        users = [
            User("anders", gaming_limit=240, daily_total=300,
                 gaming_today=45, total_today=67),
            User("aaron", gaming_limit=120, daily_total=480,
                 gaming_today=12, total_today=340),
        ]

        apps = [
            # Global patterns (active)
            AppPattern(1, "Steam Launcher", "*", AppState.TRACKED, Category.GAMING,
                      47*3600, now - timedelta(hours=1), "process"),
            AppPattern(2, "Minecraft Launcher", "*", AppState.TRACKED, Category.GAMING,
                      0, now - timedelta(days=2), "process"),
            AppPattern(3, "Minecraft", "*", AppState.TRACKED, Category.GAMING,
                      8*3600+5*60, now - timedelta(minutes=30), "process"),
            AppPattern(4, "RetroArch", "*", AppState.TRACKED, Category.GAMING,
                      0, now - timedelta(days=5), "process"),

            # Anders' discovered apps
            AppPattern(10, "OxygenNotIncluded", "anders", AppState.DISCOVERED, Category.UNCATEGORIZED,
                      17*60, now - timedelta(hours=2), "process"),
            AppPattern(11, "hollow_knight.e", "anders", AppState.DISCOVERED, Category.UNCATEGORIZED,
                      16*60, now - timedelta(hours=3), "process"),
            AppPattern(12, "tf_linux64", "anders", AppState.DISCOVERED, Category.UNCATEGORIZED,
                      64*60, now - timedelta(days=1), "process"),
            AppPattern(13, "discord.com", "anders", AppState.DISCOVERED, Category.UNCATEGORIZED,
                      2*3600+10*60, now - timedelta(minutes=15), "browser:chrome"),
            AppPattern(14, "music.youtube.com", "anders", AppState.DISCOVERED, Category.UNCATEGORIZED,
                      45*60, now - timedelta(hours=1), "browser:chrome"),

            # Aaron's discovered apps
            AppPattern(20, "chrome", "aaron", AppState.DISCOVERED, Category.UNCATEGORIZED,
                      87*3600, now - timedelta(minutes=5), "process"),
            AppPattern(21, "claude", "aaron", AppState.IGNORED, Category.PRODUCTIVITY,
                      32*3600, now - timedelta(minutes=2), "process"),
            AppPattern(22, "github.com", "aaron", AppState.DISCOVERED, Category.UNCATEGORIZED,
                      58*60, now - timedelta(hours=6), "browser:chrome"),
            AppPattern(23, "google.com", "aaron", AppState.DISCOVERED, Category.UNCATEGORIZED,
                      10*3600+39*60, now - timedelta(hours=2), "browser:chrome"),
            AppPattern(24, "YouTube", "aaron", AppState.TRACKED, Category.ENTERTAINMENT,
                      28*3600+40*60, now - timedelta(minutes=10), "browser:chrome"),

            # Some ignored/blocked examples
            AppPattern(30, "pytest", "aaron", AppState.IGNORED, Category.PRODUCTIVITY,
                      0, now - timedelta(days=2), "process"),
            AppPattern(31, "cheat-engine", "anders", AppState.BLOCKED, Category.UNCATEGORIZED,
                      5*60, now - timedelta(days=7), "process"),
        ]

        return cls(users=users, apps=apps)


# =============================================================================
# Real Backend - Connects to playtimed SQLite database
# =============================================================================

class PlaytimedBackend:
    """Real backend that queries playtimed's SQLite database"""

    DEFAULT_DB = "/var/lib/playtimed/playtimed.db"

    def __init__(self, db_path: str = None):
        self.db_path = db_path or self.DEFAULT_DB
        self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    @classmethod
    def is_available(cls, db_path: str = None) -> bool:
        """Check if we can access the playtimed database"""
        path = db_path or cls.DEFAULT_DB
        return os.path.exists(path) and os.access(path, os.R_OK)

    def get_users(self) -> list[User]:
        """Fetch all monitored users with their limits and today's usage"""
        cursor = self.conn.execute("""
            SELECT user, enabled, gaming_limit, daily_total,
                   weekday_start, weekday_end, weekend_start, weekend_end
            FROM user_limits
        """)

        users = []
        today = datetime.now().strftime("%Y-%m-%d")

        for row in cursor:
            # Get today's usage from daily_summary (times are in seconds)
            usage = self.conn.execute("""
                SELECT gaming_time, total_time
                FROM daily_summary
                WHERE user = ? AND date = ?
            """, (row['user'], today)).fetchone()

            # Convert seconds to minutes
            gaming_today = (usage['gaming_time'] // 60) if usage else 0
            total_today = (usage['total_time'] // 60) if usage else 0

            users.append(User(
                username=row['user'],
                enabled=bool(row['enabled']),
                gaming_limit=row['gaming_limit'] or 120,
                daily_total=row['daily_total'] or 180,
                weekday_start=row['weekday_start'] or "16:00",
                weekday_end=row['weekday_end'] or "21:00",
                weekend_start=row['weekend_start'] or "09:00",
                weekend_end=row['weekend_end'] or "22:00",
                gaming_today=gaming_today,
                total_today=total_today,
            ))

        return users

    def get_apps(self, user: str = None) -> list[AppPattern]:
        """Fetch all patterns, optionally filtered by user"""
        query = """
            SELECT id, name, owner, monitor_state, category,
                   total_runtime_seconds, last_seen, pattern_type
            FROM process_patterns
            WHERE enabled = 1
        """
        params = []

        if user:
            query += " AND (owner = ? OR owner IS NULL OR owner = '*')"
            params.append(user)

        query += " ORDER BY last_seen DESC"

        cursor = self.conn.execute(query, params)

        apps = []
        for row in cursor:
            # Map monitor_state to AppState
            state_map = {
                'active': AppState.TRACKED,
                'discovered': AppState.DISCOVERED,
                'ignored': AppState.IGNORED,
                'disallowed': AppState.BLOCKED,
            }
            state = state_map.get(row['monitor_state'], AppState.DISCOVERED)

            # Map category
            cat_map = {
                'gaming': Category.GAMING,
                'educational': Category.EDUCATIONAL,
                'productive': Category.PRODUCTIVITY,
                'launcher': Category.ENTERTAINMENT,  # launchers shown differently
            }
            category = cat_map.get(row['category'], Category.UNCATEGORIZED)

            # Parse last_seen
            last_seen = datetime.now()
            if row['last_seen']:
                try:
                    last_seen = datetime.fromisoformat(row['last_seen'])
                except (ValueError, TypeError):
                    pass

            apps.append(AppPattern(
                id=row['id'],
                name=row['name'],
                owner=row['owner'] or '*',
                state=state,
                category=category,
                runtime_seconds=row['total_runtime_seconds'] or 0,
                last_seen=last_seen,
                pattern_type=row['pattern_type'] or 'process',
            ))

        return apps

    def promote_pattern(self, pattern_id: int, category: str) -> bool:
        """Promote a discovered pattern to active monitoring"""
        try:
            result = subprocess.run(
                ['playtimed', 'discover', 'promote', str(pattern_id), category],
                capture_output=True, text=True
            )
            return result.returncode == 0
        except Exception:
            return False

    def ignore_pattern(self, pattern_id: int) -> bool:
        """Mark a pattern as ignored"""
        try:
            result = subprocess.run(
                ['playtimed', 'discover', 'ignore', str(pattern_id)],
                capture_output=True, text=True
            )
            return result.returncode == 0
        except Exception:
            return False

    def block_pattern(self, pattern_id: int) -> bool:
        """Mark a pattern as blocked/disallowed"""
        try:
            result = subprocess.run(
                ['playtimed', 'discover', 'disallow', str(pattern_id)],
                capture_output=True, text=True
            )
            return result.returncode == 0
        except Exception:
            return False

    def delete_pattern(self, pattern_id: int) -> bool:
        """Delete a pattern entirely"""
        try:
            result = subprocess.run(
                ['playtimed', 'patterns', 'delete', str(pattern_id)],
                capture_output=True, text=True
            )
            return result.returncode == 0
        except Exception:
            return False

    def set_user_limits(self, username: str, gaming_limit: int = None,
                        daily_total: int = None) -> bool:
        """Update user limits"""
        cmd = ['playtimed', 'user', 'add', username]
        if gaming_limit is not None:
            cmd.extend(['--gaming-limit', str(gaming_limit)])
        if daily_total is not None:
            cmd.extend(['--daily-total', str(daily_total)])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            return result.returncode == 0
        except Exception:
            return False

    def get_daemon_mode(self) -> str:
        """Get current daemon mode"""
        try:
            result = subprocess.run(
                ['playtimed', 'mode'],
                capture_output=True, text=True
            )
            for line in result.stdout.split('\n'):
                if line.strip().startswith('●'):
                    return line.split()[1]
            return 'unknown'
        except Exception:
            return 'unknown'

    def refresh(self):
        """Refresh data (close connection to force re-read)"""
        self.close()


# =============================================================================
# Data Provider - Switches between mock and real backend
# =============================================================================

class DataProvider:
    """Unified data provider that uses real or mock backend"""

    def __init__(self, use_mock: bool = False):
        self.use_mock = use_mock
        self._backend = None
        self._mock_data = None

    @property
    def backend(self) -> PlaytimedBackend | None:
        if self.use_mock:
            return None
        if self._backend is None:
            self._backend = PlaytimedBackend()
        return self._backend

    @property
    def mock_data(self) -> MockData:
        if self._mock_data is None:
            self._mock_data = MockData.create_sample()
        return self._mock_data

    @property
    def is_live(self) -> bool:
        return not self.use_mock and self.backend is not None

    @property
    def users(self) -> list[User]:
        if self.use_mock:
            return self.mock_data.users
        return self.backend.get_users()

    @property
    def apps(self) -> list[AppPattern]:
        if self.use_mock:
            return self.mock_data.apps
        return self.backend.get_apps()

    def get_user_apps(self, username: str) -> list[AppPattern]:
        """Get apps for a specific user (their own + global tracked)"""
        if self.use_mock:
            return [a for a in self.mock_data.apps
                    if a.owner == username or
                       (a.owner == '*' and a.state == AppState.TRACKED)]
        return self.backend.get_apps(username)

    def promote(self, pattern_id: int, category: str) -> bool:
        if self.use_mock:
            return True  # Pretend success
        return self.backend.promote_pattern(pattern_id, category)

    def ignore(self, pattern_id: int) -> bool:
        if self.use_mock:
            return True
        return self.backend.ignore_pattern(pattern_id)

    def block(self, pattern_id: int) -> bool:
        if self.use_mock:
            return True
        return self.backend.block_pattern(pattern_id)

    def delete(self, pattern_id: int) -> bool:
        if self.use_mock:
            return True
        return self.backend.delete_pattern(pattern_id)

    def set_limits(self, username: str, gaming: int = None, total: int = None) -> bool:
        if self.use_mock:
            return True
        return self.backend.set_user_limits(username, gaming, total)

    def refresh(self):
        if self.backend:
            self.backend.refresh()


# Determine if we should use mock data
def should_use_mock() -> bool:
    import sys
    if '--mock' in sys.argv:
        return True
    if not PlaytimedBackend.is_available():
        return True
    return False

# Global data provider
DATA = DataProvider(use_mock=should_use_mock())


# =============================================================================
# Custom Messages
# =============================================================================

class UserSelected(Message):
    """Fired when a user card is clicked"""
    def __init__(self, user: User) -> None:
        self.user = user
        super().__init__()


class AppSelected(Message):
    """Fired when an app row is clicked"""
    def __init__(self, app: AppPattern) -> None:
        self.app_pattern = app
        super().__init__()


class AppAction(Message):
    """Fired for app context actions"""
    def __init__(self, app: AppPattern, action: str) -> None:
        self.app_pattern = app
        self.action = action
        super().__init__()


# =============================================================================
# Widgets
# =============================================================================

class UserCard(Static):
    """Display a user's status as a card - clickable"""

    can_focus = True

    def __init__(self, user: User, **kwargs):
        super().__init__(**kwargs)
        self.user = user

    def compose(self) -> ComposeResult:
        u = self.user
        gaming_pct = min(100, int(u.gaming_today / u.gaming_limit * 100)) if u.gaming_limit else 0
        total_pct = min(100, int(u.total_today / u.daily_total * 100)) if u.daily_total else 0

        def bar(pct: int, width: int = 10) -> str:
            filled = int(pct / 100 * width)
            return "▓" * filled + "░" * (width - filled)

        status = "enabled" if u.enabled else "disabled"

        yield Static(f"[bold]{u.username}[/bold] [{status}]", classes="user-name")
        yield Static(f"Gaming:  {u.gaming_today}m / {u.gaming_limit}m  {bar(gaming_pct)} {gaming_pct}%")
        yield Static(f"Total:   {u.total_today}m / {u.daily_total}m  {bar(total_pct)} {total_pct}%")
        yield Static(f"Hours:   Weekday {u.weekday_start}-{u.weekday_end}  Weekend {u.weekend_start}-{u.weekend_end}", classes="dim")

    def on_click(self, event: events.Click) -> None:
        self.post_message(UserSelected(self.user))

    def on_enter(self, event: events.Enter) -> None:
        self.add_class("hover")

    def on_leave(self, event: events.Leave) -> None:
        self.remove_class("hover")

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            self.post_message(UserSelected(self.user))


class AppRow(Static):
    """A single app in the list - clickable/selectable"""

    can_focus = True
    selected = reactive(False)

    def __init__(self, app_pattern: AppPattern, **kwargs):
        super().__init__(**kwargs)
        self.app_pattern = app_pattern

    def compose(self) -> ComposeResult:
        a = self.app_pattern
        state_icons = {
            AppState.DISCOVERED: "○",
            AppState.TRACKED: "●",
            AppState.IGNORED: "◌",
            AppState.BLOCKED: "⊘",
        }
        type_short = "🌐" if "browser" in a.pattern_type else "⚙"

        yield Static(
            f"{state_icons[a.state]} {type_short} {a.name:<25} {a.runtime_display:>8}  [{a.category.value}]",
            classes=f"app-{a.state.value}"
        )

    def on_click(self, event: events.Click) -> None:
        if self.parent:
            for sibling in self.parent.query("AppRow"):
                sibling.selected = False
        self.selected = True
        self.post_message(AppSelected(self.app_pattern))

    def watch_selected(self, selected: bool) -> None:
        self.set_class(selected, "selected")

    def on_enter(self, event: events.Enter) -> None:
        self.add_class("hover")

    def on_leave(self, event: events.Leave) -> None:
        self.remove_class("hover")

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            self.post_message(AppSelected(self.app_pattern))
        elif event.key == "t":
            self.post_message(AppAction(self.app_pattern, "track"))
        elif event.key == "i":
            self.post_message(AppAction(self.app_pattern, "ignore"))
        elif event.key == "b":
            self.post_message(AppAction(self.app_pattern, "block"))
        elif event.key == "d":
            self.post_message(AppAction(self.app_pattern, "delete"))


# =============================================================================
# Content Panes (used inside tabs)
# =============================================================================

class UsersPane(Container):
    """Users list pane"""

    selected_user: User | None = None

    def compose(self) -> ComposeResult:
        yield Static("[bold]Watched Users[/bold]", classes="section-title")
        yield Static("[dim]Click a user or use ↑↓ to navigate, Enter to view apps[/dim]\n")
        yield ScrollableContainer(
            *[UserCard(user, classes="user-card") for user in DATA.users],
            id="users-list"
        )
        yield Horizontal(
            Button("[w] Watch New", id="btn-watch", variant="primary"),
            Button("[l] Limits", id="btn-limits"),
            Button("[r] Refresh", id="btn-refresh"),
            classes="button-bar"
        )

    def on_user_selected(self, event: UserSelected) -> None:
        self.selected_user = event.user
        for card in self.query("UserCard"):
            card.set_class(card.user == event.user, "selected")
        # Navigate to user's apps
        self.app.show_user_apps(event.user)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-watch":
            self.app.notify("Would open 'watch new user' dialog")
        elif event.button.id == "btn-limits":
            if self.selected_user:
                self.app.push_screen(LimitsModal(self.selected_user))
            elif DATA.users:
                self.app.push_screen(LimitsModal(DATA.users[0]))
        elif event.button.id == "btn-refresh":
            self.app.notify("Would refresh user data")


class AppsPane(Container):
    """Apps list for a specific user"""

    selected_app: AppPattern | None = None

    def __init__(self, user: User, **kwargs):
        super().__init__(**kwargs)
        self.user = user

    def compose(self) -> ComposeResult:
        user_apps = DATA.get_user_apps(self.user.username)

        yield Static(f"[bold]Apps for {self.user.username}[/bold]  [dim]Click to select[/dim]", classes="section-title")

        with TabbedContent(id="apps-tabs"):
            with TabPane("All", id="apps-tab-all"):
                yield ScrollableContainer(
                    *[AppRow(a, classes="app-row") for a in user_apps],
                )
            with TabPane(f"Discovered ({len([a for a in user_apps if a.state == AppState.DISCOVERED])})", id="apps-tab-discovered"):
                discovered = [a for a in user_apps if a.state == AppState.DISCOVERED]
                yield ScrollableContainer(
                    *[AppRow(a, classes="app-row") for a in discovered],
                )
            with TabPane(f"Tracked ({len([a for a in user_apps if a.state == AppState.TRACKED])})", id="apps-tab-tracked"):
                tracked = [a for a in user_apps if a.state == AppState.TRACKED]
                yield ScrollableContainer(
                    *[AppRow(a, classes="app-row") for a in tracked],
                )
            with TabPane("Ignored", id="apps-tab-ignored"):
                ignored = [a for a in user_apps if a.state == AppState.IGNORED]
                yield ScrollableContainer(
                    *[AppRow(a, classes="app-row") for a in ignored],
                )
            with TabPane("Blocked", id="apps-tab-blocked"):
                blocked = [a for a in user_apps if a.state == AppState.BLOCKED]
                yield ScrollableContainer(
                    *[AppRow(a, classes="app-row") for a in blocked],
                )

        yield Horizontal(
            Button("← Back", id="btn-back"),
            Button("Track", id="btn-track", variant="success"),
            Button("Ignore", id="btn-ignore"),
            Button("Block", id="btn-block", variant="error"),
            classes="button-bar-inline"
        )

    def on_app_selected(self, event: AppSelected) -> None:
        self.selected_app = event.app_pattern
        self.app.notify(f"Selected: {event.app_pattern.name} ({event.app_pattern.state.value})")

    def on_app_action(self, event: AppAction) -> None:
        self.app.notify(f"Action '{event.action}' on {event.app_pattern.name}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-back":
            self.app.show_users()
        elif event.button.id == "btn-track":
            if self.selected_app:
                if DATA.promote(self.selected_app.id, "gaming"):
                    self.app.notify(f"Tracked {self.selected_app.name} as gaming")
                else:
                    self.app.notify(f"Failed to track {self.selected_app.name}", severity="error")
            else:
                self.app.notify("No app selected")
        elif event.button.id == "btn-ignore":
            if self.selected_app:
                if DATA.ignore(self.selected_app.id):
                    self.app.notify(f"Ignored {self.selected_app.name}")
                else:
                    self.app.notify(f"Failed to ignore {self.selected_app.name}", severity="error")
            else:
                self.app.notify("No app selected")
        elif event.button.id == "btn-block":
            if self.selected_app:
                if DATA.block(self.selected_app.id):
                    self.app.notify(f"Blocked {self.selected_app.name}")
                else:
                    self.app.notify(f"Failed to block {self.selected_app.name}", severity="error")
            else:
                self.app.notify("No app selected")


class StatusPane(Container):
    """Real-time status pane"""

    def compose(self) -> ComposeResult:
        yield Static("[bold]Current Status[/bold]\n", classes="section-title")
        yield ScrollableContainer(
            Static("This pane would show real-time activity:\n"),
            Static("• Currently running tracked apps"),
            Static("• Active sessions per user"),
            Static("• Time remaining warnings"),
            Static("• Recent notifications sent"),
            Static("\n[dim]Would auto-refresh every few seconds[/dim]"),
            id="status-content"
        )
        yield Horizontal(
            Button("[r] Refresh", id="btn-status-refresh", variant="primary"),
            Button("[p] Pause All", id="btn-pause"),
            Button("[m] Mode", id="btn-mode"),
            classes="button-bar"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-status-refresh":
            self.app.notify("Would refresh status")
        elif event.button.id == "btn-pause":
            self.app.notify("Would pause all tracking")
        elif event.button.id == "btn-mode":
            self.app.notify("Would open mode selector")


class ReportPane(Container):
    """Historical reports pane"""

    def compose(self) -> ComposeResult:
        yield Static("[bold]Usage Reports[/bold]\n", classes="section-title")
        yield ScrollableContainer(
            Static("This pane would show historical data:\n"),
            Static("• Daily/weekly/monthly summaries"),
            Static("• Time per app breakdown"),
            Static("• Limit compliance history"),
            Static("• Trends over time"),
            Static("\n"),
            Horizontal(
                Static("User: "),
                Select([(u.username, u.username) for u in DATA.users], id="report-user"),
                Static("  Days: "),
                Select([("7 days", 7), ("14 days", 14), ("30 days", 30), ("90 days", 90)], id="report-days"),
                classes="form-row-inline"
            ),
            id="report-content"
        )
        yield Horizontal(
            Button("[g] Generate", id="btn-generate", variant="primary"),
            Button("[e] Export", id="btn-export"),
            Button("[c] Clear", id="btn-clear"),
            classes="button-bar"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-generate":
            self.app.notify("Would generate report")
        elif event.button.id == "btn-export":
            self.app.notify("Would export report")
        elif event.button.id == "btn-clear":
            self.app.notify("Would clear report")


class SettingsPane(Container):
    """Settings pane"""

    def compose(self) -> ComposeResult:
        yield Static("[bold]Settings[/bold]\n", classes="section-title")
        yield Static("Daemon configuration:\n")
        yield Horizontal(
            Static("Mode: ", classes="label-inline"),
            Select([("Normal", "normal"), ("Passthrough", "passthrough"), ("Strict", "strict")], id="mode-select"),
            classes="form-row-inline"
        )
        yield Static("\n")
        yield Static("Discovery settings:\n")
        yield Horizontal(
            Static("CPU threshold: ", classes="label-inline"),
            Input("30", id="cpu-threshold", classes="small-input"),
            Static("%"),
            classes="form-row-inline"
        )
        yield Horizontal(
            Static("Enabled: ", classes="label-inline"),
            Checkbox("", id="discovery-enabled", value=True),
            classes="form-row-inline"
        )
        yield Horizontal(
            Button("[s] Save", id="btn-save-settings", variant="primary"),
            Button("[x] Reset", id="btn-reset-settings"),
            Button("[d] Defaults", id="btn-defaults"),
            classes="button-bar"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save-settings":
            self.app.notify("Would save settings")
        elif event.button.id == "btn-reset-settings":
            self.app.notify("Would reset form values")
        elif event.button.id == "btn-defaults":
            self.app.notify("Would restore to defaults")


# =============================================================================
# Modal Screens
# =============================================================================

class AppsScreen(Screen):
    """Full screen for viewing/managing apps for a user"""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("t", "track", "Track"),
        Binding("i", "ignore", "Ignore"),
        Binding("b", "block", "Block"),
        Binding("d", "delete", "Delete"),
    ]

    selected_app: AppPattern | None = None
    _restore_tab: str | None = None

    def __init__(self, user: User, **kwargs):
        super().__init__(**kwargs)
        self.user = user

    def on_mount(self) -> None:
        """Restore active tab if set"""
        if self._restore_tab:
            try:
                tabs = self.query_one("#apps-tabs", TabbedContent)
                tabs.active = self._restore_tab
            except Exception:
                pass

    def compose(self) -> ComposeResult:
        yield Header()

        user_apps = DATA.get_user_apps(self.user.username)

        yield Static(f"[bold]Apps for {self.user.username}[/bold]  [dim]Click to select, t/i/b for actions[/dim]", classes="screen-title")

        with TabbedContent(id="apps-tabs"):
            with TabPane("All", id="apps-tab-all"):
                yield ScrollableContainer(
                    *[AppRow(a, classes="app-row") for a in user_apps],
                )
            with TabPane(f"Discovered ({len([a for a in user_apps if a.state == AppState.DISCOVERED])})", id="apps-tab-discovered"):
                discovered = [a for a in user_apps if a.state == AppState.DISCOVERED]
                yield ScrollableContainer(
                    *[AppRow(a, classes="app-row") for a in discovered],
                )
            with TabPane(f"Tracked ({len([a for a in user_apps if a.state == AppState.TRACKED])})", id="apps-tab-tracked"):
                tracked = [a for a in user_apps if a.state == AppState.TRACKED]
                yield ScrollableContainer(
                    *[AppRow(a, classes="app-row") for a in tracked],
                )
            with TabPane("Ignored", id="apps-tab-ignored"):
                ignored = [a for a in user_apps if a.state == AppState.IGNORED]
                yield ScrollableContainer(
                    *[AppRow(a, classes="app-row") for a in ignored],
                )
            with TabPane("Blocked", id="apps-tab-blocked"):
                blocked = [a for a in user_apps if a.state == AppState.BLOCKED]
                yield ScrollableContainer(
                    *[AppRow(a, classes="app-row") for a in blocked],
                )

        yield Horizontal(
            Button("← Back", id="btn-back"),
            Button("Track", id="btn-track", variant="success"),
            Button("Ignore", id="btn-ignore"),
            Button("Block", id="btn-block", variant="error"),
            Button("Delete", id="btn-delete", variant="warning"),
            classes="button-bar"
        )
        yield Footer()

    def on_app_selected(self, event: AppSelected) -> None:
        self.selected_app = event.app_pattern
        self.notify(f"Selected: {event.app_pattern.name} ({event.app_pattern.state.value})")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-back":
            self.action_back()
        elif event.button.id == "btn-track":
            self.action_track()
        elif event.button.id == "btn-ignore":
            self.action_ignore()
        elif event.button.id == "btn-block":
            self.action_block()
        elif event.button.id == "btn-delete":
            self.action_delete()

    def action_back(self) -> None:
        self.app.pop_screen()

    def _refresh_screen(self) -> None:
        """Refresh by replacing this screen, preserving active tab"""
        DATA.refresh()
        # Remember which tab was active
        try:
            tabs = self.query_one("#apps-tabs", TabbedContent)
            active_tab = tabs.active
        except Exception:
            active_tab = None

        self.app.pop_screen()
        new_screen = AppsScreen(self.user)
        new_screen._restore_tab = active_tab  # Pass to new screen
        self.app.push_screen(new_screen)

    def action_track(self) -> None:
        if self.selected_app:
            if DATA.promote(self.selected_app.id, "gaming"):
                self.notify(f"Tracked {self.selected_app.name} as gaming")
                self._refresh_screen()
            else:
                self.notify(f"Failed to track {self.selected_app.name}", severity="error")
        else:
            self.notify("No app selected - click an app first")

    def action_ignore(self) -> None:
        if self.selected_app:
            if DATA.ignore(self.selected_app.id):
                self.notify(f"Ignored {self.selected_app.name}")
                self._refresh_screen()
            else:
                self.notify(f"Failed to ignore {self.selected_app.name}", severity="error")
        else:
            self.notify("No app selected")

    def action_block(self) -> None:
        if self.selected_app:
            if DATA.block(self.selected_app.id):
                self.notify(f"Blocked {self.selected_app.name}")
                self._refresh_screen()
            else:
                self.notify(f"Failed to block {self.selected_app.name}", severity="error")
        else:
            self.notify("No app selected")

    def action_delete(self) -> None:
        if self.selected_app:
            if DATA.delete(self.selected_app.id):
                self.notify(f"Deleted {self.selected_app.name}")
                self._refresh_screen()
            else:
                self.notify(f"Failed to delete {self.selected_app.name}", severity="error")
        else:
            self.notify("No app selected")


class LimitsModal(ModalScreen):
    """Modal dialog for editing user limits"""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, user: User, **kwargs):
        super().__init__(**kwargs)
        self.user = user

    def compose(self) -> ComposeResult:
        yield Container(
            Static(f"[bold]Edit Limits: {self.user.username}[/bold]\n"),
            Horizontal(
                Static("Gaming limit (min): ", classes="label"),
                Input(str(self.user.gaming_limit), id="gaming-limit"),
                classes="form-row"
            ),
            Horizontal(
                Static("Daily total (min):  ", classes="label"),
                Input(str(self.user.daily_total), id="daily-total"),
                classes="form-row"
            ),
            Static("\n[bold]Allowed Hours[/bold]\n"),
            Horizontal(
                Static("Weekday: ", classes="label"),
                Input(self.user.weekday_start, id="weekday-start", classes="time-input"),
                Static(" to "),
                Input(self.user.weekday_end, id="weekday-end", classes="time-input"),
                classes="form-row"
            ),
            Horizontal(
                Static("Weekend: ", classes="label"),
                Input(self.user.weekend_start, id="weekend-start", classes="time-input"),
                Static(" to "),
                Input(self.user.weekend_end, id="weekend-end", classes="time-input"),
                classes="form-row"
            ),
            Static("\n"),
            Horizontal(
                Button("Save", variant="primary", id="save"),
                Button("Cancel", id="cancel"),
                classes="button-row"
            ),
            id="limits-modal"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            try:
                gaming = int(self.query_one("#gaming-limit", Input).value)
                total = int(self.query_one("#daily-total", Input).value)
                if DATA.set_limits(self.user.username, gaming=gaming, total=total):
                    self.app.notify(f"Updated limits for {self.user.username}: gaming={gaming}m, total={total}m")
                else:
                    self.app.notify(f"Failed to save limits", severity="error")
            except ValueError:
                self.app.notify("Invalid number entered", severity="error")
                return
            self.dismiss(True)
        elif event.button.id == "cancel":
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


# =============================================================================
# Main App
# =============================================================================

class PlaytimedTUI(App):
    """Main TUI application with tabbed interface"""

    CSS = """
    Screen {
        background: $surface;
    }

    /* Main tabs at top */
    #main-tabs {
        height: 100%;
    }

    #main-tabs > TabPane {
        padding: 1 2;
    }

    /* Content switcher for users/apps views */
    #users-content {
        height: 100%;
    }

    .section-title {
        text-style: bold;
        color: $primary;
        padding-bottom: 1;
    }

    .user-card {
        border: solid $primary;
        padding: 1;
        margin: 1 0;
        background: $panel;
    }

    .user-card.hover {
        border: solid $secondary;
        background: $surface-lighten-1;
    }

    .user-card.selected {
        border: double $warning;
        background: $surface-lighten-2;
    }

    .user-card:focus {
        border: solid $warning;
    }

    .user-name {
        text-style: bold;
    }

    .dim {
        color: $text-muted;
    }

    /* App rows */
    .app-row {
        padding: 0 1;
        height: 2;
    }

    .app-row.hover {
        background: $surface-lighten-1;
    }

    .app-row.selected {
        background: $primary-darken-2;
        text-style: bold;
    }

    .app-row:focus {
        background: $primary-darken-1;
    }

    .app-discovered {
        color: $warning;
    }

    .app-tracked {
        color: $success;
    }

    .app-ignored {
        color: $text-muted;
    }

    .app-blocked {
        color: $error;
    }

    /* Nested tabs for apps */
    #apps-tabs {
        height: 1fr;
    }

    #apps-tabs > TabPane {
        padding: 1;
    }

    /* Screen title */
    .screen-title {
        padding: 1 2;
        background: $panel;
    }

    /* Button bars */
    .button-bar {
        dock: bottom;
        height: auto;
        padding: 1;
        background: $panel;
    }

    .button-bar Button {
        margin-right: 1;
    }

    .button-bar-inline {
        height: auto;
        padding: 1 0;
    }

    .button-bar-inline Button {
        margin-right: 1;
    }

    /* Forms */
    .form-row {
        height: 3;
        margin: 1 0;
    }

    .form-row-inline {
        height: auto;
        margin: 0 0 1 0;
    }

    .label {
        width: 20;
        padding-top: 1;
    }

    .label-inline {
        width: auto;
        padding-right: 1;
    }

    .time-input {
        width: 8;
    }

    .small-input {
        width: 6;
    }

    .button-row {
        margin-top: 1;
    }

    .button-row Button {
        margin-right: 2;
    }

    /* Modal styling */
    LimitsModal {
        align: center middle;
    }

    #limits-modal {
        width: 60;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: thick $primary;
    }

    /* Select widgets */
    Select {
        width: auto;
        min-width: 12;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("1", "tab_users", "Users"),
        Binding("2", "tab_status", "Status"),
        Binding("3", "tab_report", "Report"),
        Binding("4", "tab_settings", "Settings"),
        Binding("w", "watch_user", "Watch", show=False),
        Binding("l", "edit_limits", "Limits"),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("p", "pause_all", "Pause", show=False),
        Binding("m", "toggle_mode", "Mode", show=False),
        Binding("g", "generate_report", "Generate", show=False),
        Binding("e", "export_report", "Export", show=False),
        Binding("c", "clear_report", "Clear", show=False),
        Binding("s", "save_settings", "Save", show=False),
        Binding("x", "reset_settings", "Reset", show=False),
        Binding("d", "restore_defaults", "Defaults", show=False),
        Binding("?", "help", "Help"),
    ]

    TITLE = "playtimed"

    current_user: User | None = None

    def on_mount(self) -> None:
        """Set subtitle based on data source"""
        if DATA.is_live:
            self.sub_title = "LIVE - Screen time management"
        else:
            self.sub_title = "MOCK DATA - Development mode"

    def compose(self) -> ComposeResult:
        yield Header()

        with TabbedContent(id="main-tabs"):
            with TabPane("Users", id="tab-users"):
                yield UsersPane(id="users-pane")
            with TabPane("Status", id="tab-status"):
                yield StatusPane()
            with TabPane("Report", id="tab-report"):
                yield ReportPane()
            with TabPane("Settings", id="tab-settings"):
                yield SettingsPane()

        yield Footer()

    def show_user_apps(self, user: User) -> None:
        """Switch to apps view for a user (push a screen)"""
        self.current_user = user
        self.push_screen(AppsScreen(user))

    def action_tab_users(self) -> None:
        self.query_one("#main-tabs", TabbedContent).active = "tab-users"

    def action_tab_status(self) -> None:
        self.query_one("#main-tabs", TabbedContent).active = "tab-status"

    def action_tab_report(self) -> None:
        self.query_one("#main-tabs", TabbedContent).active = "tab-report"

    def action_tab_settings(self) -> None:
        self.query_one("#main-tabs", TabbedContent).active = "tab-settings"

    def action_watch_user(self) -> None:
        self.notify("Would open 'watch new user' dialog")

    def action_edit_limits(self) -> None:
        if self.current_user:
            self.push_screen(LimitsModal(self.current_user))
        elif DATA.users:
            self.push_screen(LimitsModal(DATA.users[0]))
        else:
            self.notify("No user selected")

    def action_refresh(self) -> None:
        DATA.refresh()
        self.notify("Data refreshed" if DATA.is_live else "Mock data (no refresh needed)")

    def action_pause_all(self) -> None:
        self.notify("Would pause all tracking")

    def action_toggle_mode(self) -> None:
        self.notify("Would open mode selector")

    def action_generate_report(self) -> None:
        self.notify("Would generate report")

    def action_export_report(self) -> None:
        self.notify("Would export report")

    def action_clear_report(self) -> None:
        self.notify("Would clear report")

    def action_save_settings(self) -> None:
        self.notify("Would save settings")

    def action_reset_settings(self) -> None:
        self.notify("Would reset form values")

    def action_restore_defaults(self) -> None:
        self.notify("Would restore to defaults")

    def action_help(self) -> None:
        self.notify(
            "Tab/Click: Navigate tabs | 1-4: Jump to tab\n"
            "↑↓/Click: Select items | Enter: Activate\n"
            "t/i/b/d: Track/Ignore/Block/Delete app | l: Edit limits",
            title="Help",
            timeout=6
        )


# =============================================================================
# Entry Point
# =============================================================================

if __name__ == "__main__":
    app = PlaytimedTUI()
    app.run()
