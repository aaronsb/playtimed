# ADR-002: Modular Worker Architecture

Status: Proposed
Date: 2026-02-07
Deciders: @aaron, @claude

## Context

`main.py` is a 2000-line monolith that handles five distinct responsibilities: process scanning, enforcement, CLI commands, configuration, and daemon orchestration. As we add detection sources (Chrome, Firefox, Proton, native processes) and communication backends (KDE, GNOME, Clippy.js), the monolith becomes harder to extend without touching unrelated code.

The browser worker pattern (`BrowserWorker` ABC in `browser/base.py`) already demonstrates the right approach — Chrome and Firefox implement a shared interface, the kernel iterates workers without knowing their internals. But this pattern hasn't been applied to:

- Process detection (inlined in `_scan_all_processes()`)
- Proton game discovery (inlined in scan loop)
- CLI commands (10 `cmd_*` functions in `main.py`)
- Configuration loading (scattered across modules)

**Trigger:** Adding Firefox domain tracking revealed that the modular browser worker pattern is the right architecture for all detection sources, not just browsers.

## Decision

Decompose `main.py` into focused modules organized by responsibility, with clear interfaces between layers.

### Architecture

```
Detection Workers ──┐
  workers/           │
    chrome.py        │
    firefox.py       ├──▶  Kernel (daemon core)  ──▶  Communication Workers
    proton.py        │       kernel/                    comms/
    process.py       │         enforcement.py             kde.py (freedesktop)
                     │         sessions.py                gnome.py
                     ┘         discovery.py               clippy.py
                                                          log.py
                     ┌──▶  Reporting Workers
                     │       cli/
                     │         status.py
                     │         report.py
                     │         sessions.py
                     │         patterns.py
                     │         discover.py
                     │
                     └──▶  Configuration
                             config/
                               loader.py
                               schema.py
```

### Layer Responsibilities

#### Detection Workers (`workers/`)

Each worker answers: **"what is happening right now?"**

Common interface:
```python
class DetectionWorker(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def scan(self, uid: int) -> list[DetectedActivity]: ...

    @abstractmethod
    def is_available(self) -> bool:
        """Can this worker function on this system?"""
        ...
```

Returns `DetectedActivity` objects that the kernel consumes uniformly:
```python
@dataclass
class DetectedActivity:
    name: str           # e.g., "discord.com", "FalloutNV", "minecraft"
    source: str         # e.g., "chrome", "firefox", "proton", "process"
    category: str       # from pattern DB or None if undiscovered
    pid: Optional[int]  # for killable processes
    metadata: dict      # source-specific (domain, exe path, cpu%, etc.)
```

Workers:
- **`chrome.py`** — Existing `ChromeWorker`, adapted to `DetectionWorker` interface. Signature matching + History DB + session files.
- **`firefox.py`** — Existing `FirefoxWorker`, adapted. Signature matching + places.sqlite.
- **`proton.py`** — Extracts individual `.exe` names from Wine/Proton processes. Auto-discovers specific games from the catchall pattern.
- **`process.py`** — Native Linux process scanning via psutil. CPU threshold filtering, launcher detection.

#### Enforcement Kernel (`kernel/`)

Answers: **"what should we do about it?"**

- **`enforcement.py`** — Time accumulation, limit checks, kill decisions. Consumes `DetectedActivity` from workers, produces enforcement actions.
- **`sessions.py`** — Session state machine (start, end, duration tracking). Manages the lifecycle bug we fixed in v0.3.0.
- **`discovery.py`** — Unknown activity → discovered → active/ignored pipeline. Threshold logic, candidate tracking.
- **`daemon.py`** — The orchestration loop. Calls workers, feeds results to enforcement, dispatches to communication. This is what remains of `PlaytimeDaemon` after extraction.

#### Communication Workers (`comms/`)

Answers: **"how do we tell the user?"**

Already mostly modular via `notify.py`:
- **`kde.py`** — `FreedesktopBackend` (covers KDE and most Linux DEs)
- **`gnome.py`** — Could specialize for GNOME-specific features
- **`clippy.py`** — `ClippyBackend` stub, future Clippy.js/Qt widget
- **`log.py`** — `LogOnlyBackend` fallback
- **`router.py`** — Existing `MessageRouter` (template selection + rendering)

#### Reporting / CLI (`cli/`)

Answers: **"show me what happened"**

- **`status.py`** — `playtimed status`
- **`report.py`** — `playtimed report`, `playtimed history`
- **`sessions.py`** — `playtimed sessions`
- **`patterns.py`** — `playtimed patterns list/add/remove`
- **`discover.py`** — `playtimed discover list/promote/ignore`
- **`user.py`** — `playtimed user add/list`
- **`message.py`** — `playtimed message list/test/add`

Each CLI module registers its argparse subparser and handler. `main.py` becomes just the entry point that wires parsers together and starts the daemon.

#### Configuration (`config/`)

Answers: **"what are the rules?"**

- **`loader.py`** — YAML config loading with defaults, validation at startup
- **`schema.py`** — Config schema definition, fail-fast on missing required values

### Migration Path

This is not a rewrite. It's extracting existing code into modules:

1. **Phase 1: CLI extraction** — Move `cmd_*` functions to `cli/` package. Lowest risk, highest line-count reduction. `main.py` drops ~800 lines.
2. **Phase 2: Detection workers** — Extract process scanning and Proton discovery into `workers/`. Adapt to `DetectionWorker` interface alongside existing browser workers.
3. **Phase 3: Kernel extraction** — Pull enforcement, sessions, discovery out of `PlaytimeDaemon` into `kernel/`. Daemon becomes the orchestrator.
4. **Phase 4: Config consolidation** — Gather scattered config loading into `config/`.

Each phase is independently deployable. No big-bang rewrite.

## Consequences

### Positive

- Adding a new detection source (e.g., Wayland compositor, game launcher API) means implementing one interface
- Adding a new notification backend (Clippy.js widget, web push) means implementing one interface
- CLI commands can be tested independently of the daemon
- `main.py` drops from 2000 lines to ~200 (entry point + daemon loop)
- Each module has a clear, testable responsibility

### Negative

- More files to navigate (mitigated by clear naming)
- Import graph becomes more complex
- Risk of over-engineering if we create abstractions we never use a second time

### Neutral

- Database layer (`db.py`) stays as-is — it's already a clean data access layer
- Existing tests continue to work through each phase
- No new dependencies required

## Alternatives Considered

### 1. Keep the Monolith

Just keep adding to `main.py`.

**Rejected because:** Already at 2000 lines, each new feature requires understanding the whole file. Firefox tracking was the trigger — we kept tripping over the monolith while adding it.

### 2. Plugin Architecture

Dynamic plugin loading with entry points.

**Rejected because:** Over-engineering for a system with 4-5 known worker types. Direct imports are simpler and more debuggable. Can revisit if external contributors want to add detection sources.

### 3. Microservices / Separate Processes

Each worker as its own process, communicating via IPC.

**Rejected because:** This is a single-machine parental control daemon, not a distributed system. In-process calls are simpler, faster, and easier to debug.
