# playtimed TUI

Screen time management interface. Connects to the real playtimed backend when run with sudo, or uses mock data for development.

## Run

```bash
# Live mode - connects to real playtimed database (requires sudo)
sudo python tui.py

# Development mode - mock data, no backend required
python tui.py --mock
```

The header shows "LIVE" or "MOCK DATA" to indicate which mode is active.

## Mouse Support

| Action | Effect |
|--------|--------|
| **Click user card** | Select and enter user's apps view |
| **Hover user card** | Border changes color |
| **Click app row** | Select app (highlights) |
| **Hover app row** | Background highlights |
| **Click buttons** | Trigger actions |

## Keyboard Navigation

| Key | Action |
|-----|--------|
| `1` | Users tab |
| `2` | Status tab |
| `3` | Report tab |
| `4` | Settings tab |
| `Tab` | Next focusable element |
| `Enter` | Activate selected item |
| `Escape` | Back / Cancel |
| `q` | Quit |
| `?` | Help |

### Per-Tab Shortcuts

**Users Tab**
| Key | Action |
|-----|--------|
| `w` | Watch new user |
| `l` | Edit limits |
| `r` | Refresh |

**Status Tab**
| Key | Action |
|-----|--------|
| `r` | Refresh |
| `p` | Pause all |
| `m` | Mode selector |

**Report Tab**
| Key | Action |
|-----|--------|
| `g` | Generate report |
| `e` | Export report |
| `c` | Clear report |

**Settings Tab**
| Key | Action |
|-----|--------|
| `s` | Save settings |
| `x` | Reset form |
| `d` | Restore defaults |

### In Apps Screen

| Key | Action |
|-----|--------|
| `t` | Track selected app |
| `i` | Ignore selected app |
| `b` | Block selected app |
| `d` | Delete selected app |
| `/` | Filter (stub) |
| `Tab` | Switch tabs (All/Discovered/Tracked/Ignored/Blocked) |

## Visual Feedback

- **Hover**: Border/background changes on mouse hover
- **Selected**: Bold text, highlighted background
- **Focus**: Different background when keyboard-focused
- **State colors**:
  - Discovered: Yellow/warning
  - Tracked: Green/success
  - Ignored: Gray/muted
  - Blocked: Red/error

## Structure

```
tui.py
├── Mock Data Layer
│   ├── AppState, Category (enums)
│   ├── AppPattern, User (dataclasses)
│   └── MockData.create_sample()
│
├── Custom Messages
│   ├── UserSelected
│   ├── AppSelected
│   └── AppAction
│
├── Widgets
│   ├── UserCard - clickable user status card
│   ├── AppRow - selectable app with hover
│
├── Content Panes (tabs)
│   ├── UsersPane - user list with click-to-view
│   ├── StatusPane - real-time status (stub)
│   ├── ReportPane - historical reports (stub)
│   └── SettingsPane - daemon config (stub)
│
├── Screens
│   ├── AppsScreen - tabbed app list for a user
│   └── LimitsModal - edit user limits dialog
│
└── PlaytimedTUI - main app with TabbedContent + CSS
```

## Experiment Ideas

1. **Double-click** - Single click selects, double-click enters
2. **Right-click menu** - Context menu positioned at cursor
3. **Drag selection** - Select multiple apps
4. **Scroll wheel** - Navigate list
5. **Tooltips** - Hover for more info
6. **Search** - `/` to filter apps by name
7. **Bulk actions** - Select multiple, then action

## Backend Interface (future)

```python
class PlaytimedBackend(Protocol):
    def get_users(self) -> list[User]: ...
    def get_apps(self, user: str) -> list[AppPattern]: ...
    def set_limits(self, user: str, limits: dict) -> None: ...
    def track_app(self, app_id: int, category: str) -> None: ...
    def ignore_app(self, app_id: int) -> None: ...
    def block_app(self, app_id: int) -> None: ...
    def get_status(self, user: str) -> dict: ...
    def get_report(self, user: str, days: int) -> dict: ...
```

## Notes

- Actions show notifications (stubs)
- Edit `MockData.create_sample()` to test scenarios
- CSS is in `PlaytimedTUI.CSS` string
- Messages bubble up from widgets to screens
