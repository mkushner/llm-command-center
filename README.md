# LLM Command Center

A terminal UI that orchestrates AI coding agents through configurable pipelines. Kanban board with vim keybindings, native PTY agent management, file-based context flow between stages.

No frameworks beyond Textual (TUI), pexpect (PTY), pyte (terminal emulation), Pydantic (models).

```
llm-cc /path/to/project
```

---

## How It Works

Tasks flow through a kanban board. Each stage spawns an agent (Claude, Codex, etc.) in a pseudo-terminal. All transitions are manual — you decide when to advance, revert, or restart.

Stages are **optional** — only stages with `[[pipeline]]` entries appear as columns. BACKLOG and DONE are always present.

```
Default (all stages):
Backlog ──m──> Planning ──m──> Execute ──m──> Review ──m──> Done

Minimal (skip planning — agent plans internally via CLAUDE.md):
Backlog ──m──> Execute ──m──> Review ──m──> Done

Execute only:
Backlog ──m──> Execute ──m──> Done
```

### Context Flow

Stages share context through files, not memory. Each task gets a docs directory at `.llm-cc/tasks/<id>/`:

```
.llm-cc/tasks/a1b2c3d4/
├── task.md              # title + description (created on first advance)
├── planning-output.md   # captured when leaving planning stage
├── diff.md              # git diff, written before review starts
├── review-output.md     # captured when leaving review stage
└── ...                  # agents can write additional files here
```

The agent receives a minimal prompt pointing to this directory. It reads what it needs. Planning agents write plans, execute agents read them, review agents get diffs — all via filesystem. Configure agent behavior through your project's `CLAUDE.md`, not through this tool.

**Key decision: file-based over in-memory.** Earlier versions captured PTY output into model fields (`review_feedback`) and injected it into prompts. This was fragile — large outputs got truncated, context was lost on restart, and the app was doing work agents should own. Now the app just writes files and points the agent at them. The agent decides what context it needs.

### Agent Input Detection

The board polls active agents every 2s and shows a yellow **WAITING FOR INPUT** indicator when the agent appears idle. Detection uses two signals:

1. **Screen stability** — the pyte terminal buffer hasn't changed for 3+ poll ticks (~0.3s)
2. **Pattern matching** — full screen text matches known input patterns

Both must be true simultaneously. Brief flickering (green→yellow→green) can occur when agents redraw the screen — this is expected and draws attention to prompts that need response.

```python
_INPUT_PATTERNS = (
    # Claude CLI permission prompts
    "enter to confirm", "y/n", "yes/no", "(y)es/(n)o",
    "allow", "deny", "press enter", "esc to cancel",
    "do you want to proceed", "tab to amend",
    "i trust this", "yes, i trust", "interrupt received",
    # Agent idle / finished
    "what would you like", "how can i help",
    # Stage completion markers
    "planning complete", "execute complete", "review complete",
)
```

**Important: searches full screen, not bottom N lines.** CLI tools use cursor positioning escape codes (ANSI CSI sequences) that can render prompts anywhere on the virtual terminal. Searching only the bottom rows misses prompts rendered higher up. The pyte `Screen.display` returns the full 40-row buffer — we search all of it.

Agents are prompted to say `PLANNING COMPLETE` / `EXECUTE COMPLETE` / `REVIEW COMPLETE` when done. These trigger the waiting indicator — you then decide whether to advance, interact, or restart. There is no separate "stage complete" state — all waiting is treated uniformly.

## Keybindings

| Key | Action |
|-----|--------|
| `h` `l` | Move between columns |
| `j` `k` | Move between tasks |
| `m` | Advance task to next stage |
| `b` | Move task back one stage |
| `r` | Restart current stage agent |
| `o` | Create new task |
| `e` | Edit task |
| `Enter` | Open agent panel (live output + input) |
| `s` | Stop agent |
| `d` | Show git diff |
| `x` | Delete task |
| `q` | Quit |

### Agent Panel

`Enter` opens a fullscreen modal showing the agent's live PTY output with ANSI colors preserved. The panel uses pyte's `HistoryScreen` for scrollback (5000 lines) and renders styled Rich `Text` objects. The PTY is dynamically resized to match the panel dimensions.

Keys are forwarded directly to the agent:

- All printable characters, Enter, Tab, arrows → sent to PTY
- `Esc` → close panel
- `Ctrl+C` → send interrupt to agent
- `Ctrl+T` → toggle text input mode (for longer prompts)
- `Shift+PageUp/PageDown` → scroll output history
- `Shift+Home/End` → jump to top / resume auto-scroll
- Mouse wheel → scroll (auto-scroll resumes when you reach the bottom)

---

## Configuration

### Zero-config

Works out of the box with no config file. Default pipeline uses `claude` for all stages.

### Project config (`.llm-cc/config.toml`)

```toml
[project]
name = "my-project"

# Git isolation mode: "none" (default), "worktree", or "branch"
[git]
mode = "none"
base_branch = "main"
branch_prefix = "task/"   # prefix for branch names; "" for flat naming

# Define agents
[agents.claude_opus]
command = "claude"
model = "claude-opus-4-6"                # shown in column header + auto-injected as --model flag
mode = "pty"

[agents.claude_sonnet]
command = "claude"
model = "claude-sonnet-4-6"
mode = "pty"

# Pipeline: which agent runs at each stage
[[pipeline]]
stage = "planning"
agent = "claude_opus"

[[pipeline]]
stage = "execute"
agent = "claude_sonnet"

[[pipeline]]
stage = "review"
agent = "claude_opus"

# Custom column labels (optional)
[project.stage_labels]
planning = "Design"
execute = "Build"
review = "QA"
```

### Plan file location

Configure where the planning agent writes its plan:

```toml
[project]
plan_dir = "plans/{branch}"   # template: {id}, {slug}, {branch}, {title}
plan_file = "PLAN.md"         # constant filename within plan_dir
review_file = "PLAN.md"       # optional: where review agent writes summary (in plan_dir)
```

Defaults: `plan_dir = ".llm-cc/tasks/{id}"`, `plan_file = "plan.md"`, `review_file = None`. The `{branch}` variable works with any git mode — `worktree` and `branch` modes create branches; `none` mode detects the current branch name without creating one (so you can manually check out a branch and `{branch}` resolves).

All stages reference the same plan path — planning writes it, execute reads it, review checks against it. Path traversal is blocked (resolved path must stay inside project directory).

When `review_file` is set, the review prompt includes a hint: `Write your review to: {plan_dir}/{review_file}`. This is a suggestion — the agent decides whether to follow it. Useful for keeping review output alongside the plan (e.g., both in `plans/{branch}/`).

If the PLANNING stage is skipped (no `[[pipeline]]` entry), git workspace setup happens at EXECUTE instead. The execute agent creates the plan itself (e.g., via CLAUDE.md rules) and the review agent still gets the plan path reference.

### Agent resolution

When a task enters a stage, the agent is resolved in order:

1. **Task override** — `task.agent_override` (per-task)
2. **Pipeline config** — `[[pipeline]]` stage mapping
3. **Global default** — `default_agent` from `~/.config/llm-cc/config.toml`

### Git modes

| Mode | Behavior | Concurrent Execute |
|------|----------|-------------------|
| `none` | Agent runs in project directory as-is | One task at a time |
| `branch` | Creates a branch, no directory isolation | One task at a time |
| `worktree` | Each task gets its own git worktree under `.llm-cc/worktrees/` | Unlimited |

`none` is the default. With worktree/branch modes, the pipeline creates git branches named `{branch_prefix}<id>-<slug>` and manages workspace setup (file copying, init scripts). The `branch_prefix` defaults to `task/` but is configurable (set to `""` for flat branch names).

### Built-in agents

Two agents are registered by default (override in config):

- **claude** — `claude {prompt}`
- **codex** — `codex "{prompt}"`

An "agent" is a named config, not a product. The same CLI with different models = different agent configs:

```toml
[agents.planner]
command = "claude"
model = "claude-opus-4-6"

[agents.coder]
command = "claude"
model = "claude-sonnet-4-6"
```

When `model` is set, it's auto-injected as `--model {model}` into the command unless `{model}` already appears in `args_template`. The model name also appears in the board column header alongside the agent name.

### Agent modes

- **PTY** (default) — spawns CLI in a pseudo-terminal via pexpect. Interactive — supports live output viewing, key forwarding, input detection.
- **API** — calls Anthropic/OpenAI SDK directly. Non-interactive, runs as background asyncio task. Optional deps: `pip install llm-command-center[anthropic]` or `[all]`.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    TUI Layer (Textual)               │
│  ┌──────────┐ ┌──────────┐ ┌───────────────────┐   │
│  │Dashboard │ │  Board   │ │  Agent Output     │   │
│  │ Screen   │ │  Screen  │ │  Panel            │   │
│  └──────────┘ └──────────┘ └───────────────────┘   │
├─────────────────────────────────────────────────────┤
│              Orchestration Layer                      │
│  ┌──────────┐ ┌──────────┐ ┌───────────────────┐   │
│  │ Pipeline │ │  Agent   │ │  Git Workspace    │   │
│  │  Engine  │ │ Registry │ │  Manager          │   │
│  └──────────┘ └──────────┘ └───────────────────┘   │
├─────────────────────────────────────────────────────┤
│                Backend Layer (Async)                  │
│  ┌──────────┐ ┌──────────┐ ┌───────────────────┐   │
│  │PTY Agent │ │API Agent │ │  JSON Storage     │   │
│  │ Backend  │ │ Backend  │ │  + Config         │   │
│  └──────────┘ └──────────┘ └───────────────────┘   │
└─────────────────────────────────────────────────────┘
```

### File structure

```
src/llm_cc/
├── models.py       # Pydantic models: Task, Config, Pipeline, Agent
├── storage.py      # JSON store (fcntl locking) + TOML config
├── pipeline.py     # Stage transitions, agent lifecycle, file-based context
├── agents.py       # PTY/API backends, output buffer, input detection
├── git.py          # Worktree/branch ops, diff, PR creation
├── app.py          # Textual App, session cleanup
├── utils.py        # async_run, process management
└── ui/
    ├── board.py    # Kanban board screen
    ├── dashboard.py # Project selector
    ├── panels.py   # Agent panel, diff view, dialogs
    └── styles.tcss # Dark theme CSS
```

### Data flow

```
Storage (JSON + TOML)
    ↕
PipelineEngine
    ├── reads/writes task docs (.llm-cc/tasks/<id>/)
    ├── manages agent lifecycle via AgentRegistry
    └── git ops via GitWorkspace
    ↕
BoardScreen (Textual)
    ├── renders columns for configured stages from TaskStore
    ├── polls agent status every 2s (waiting detection)
    └── dispatches user actions (m/b/r/s/x) to PipelineEngine
```

### Layer separation: orchestration vs agent

This tool is the **orchestration layer**. It starts/stops agents and manages task flow. It does NOT control what agents do internally.

```
┌──────────────────────────────────────────────┐
│  LLM Command Center (orchestration layer)    │
│                                              │
│  Pipeline:                                   │
│    1. Start agent for stage                  │
│    2. Point agent at task docs               │
│    3. Detect when agent is waiting           │
│    4. User decides: advance / revert / input │
│       ┌────────────────────────┐             │
│       │ Claude (agent layer)   │             │
│       │ - reads CLAUDE.md      │             │
│       │ - reads .agent/        │             │
│       │ - has its own skills   │             │
│       │ - decides what to do   │             │
│       └────────────────────────┘             │
└──────────────────────────────────────────────┘
```

Agent behavior is configured via project files the agent natively reads (`CLAUDE.md`, `.agent/knowledge/`, hooks, skills). Not via prompt injection from our tool. Our prompts are minimal:

```
PLANNING: Fix login bug

Task description: .llm-cc/tasks/a1b2c3d4/task.md
Write your plan to: plans/a1b2-fix-login-bug/PLAN.md

When finished, say: PLANNING COMPLETE
```

---

## Technical Decisions

### Why Python

The bottleneck is AI agent response time (seconds to minutes), not rendering speed. Python's advantages:

- **Textual** — best-in-class TUI framework (CSS theming, reactive state, component model)
- **pexpect** — mature PTY management with pattern matching and async support
- **pyte** — proper VT100 terminal emulator for decoding agent output
- **Pydantic** — type-safe models with validation and JSON serialization
- **asyncio** — all work is I/O-bound (waiting for AI, git, file I/O). GIL is irrelevant.
- **AI SDKs** — Anthropic and OpenAI SDKs are Python-first

Tradeoffs accepted: ~80ms startup (app launches once, stays open), ~40MB memory (irrelevant for dev tool), no single binary (use `uvx`).

### Why asyncio (not threads)

Every operation is I/O-bound:
- AI agents: seconds to minutes (PTY reads, API calls)
- Git ops: subprocess, <1s
- File I/O: <1ms

asyncio handles all concurrently on one thread. No GIL issues, no thread sync, no deadlocks. Textual's `@work` decorator integrates natively with asyncio.

### Why pyte (not regex ANSI stripping)

CLI agents output complex VT100 escape sequences — cursor positioning, screen clearing, color codes, alternate screen buffers. Regex stripping (`\x1b\[[0-9;]*m`) fails on:

- Cursor movement (`\x1b[H`, `\x1b[2J`)
- Scrolling regions
- Tab stops
- Multi-byte sequences

pyte is a full terminal emulator. Feed it raw PTY output, get a character grid back. `OutputBuffer` uses `pyte.HistoryScreen` (with 5000-line scrollback) for both display rendering and input pattern detection. The `display_rich()` method walks pyte's per-character style attributes (fg, bg, bold, italic, underscore) and reconstructs Rich `Text` objects with proper colors — including named colors, 256-color, and truecolor. The PTY and pyte buffer are dynamically sized to match the actual terminal dimensions.

### Why flat JSON (not SQLite)

- Human-readable — `cat .llm-cc/tasks.json` to debug
- No migration tooling needed
- Atomic writes via `tempfile` + `os.replace()`
- File locking via `fcntl.flock()` (POSIX) prevents corruption
- Task count is small (tens, not thousands) — no indexing needed

### Why no auto-advance

Earlier versions auto-advanced Execute→Review when the agent exited. Problems:

1. Agent exit doesn't mean "done" — it might crash, timeout, or need input
2. Review agent starting immediately prevents user from inspecting execute results
3. Removes user agency over the workflow

All transitions are now manual. The WAITING FOR INPUT indicator tells you when the agent is idle. You decide what happens next.

### Why no `--dangerously-skip-permissions`

Tried it — Claude CLI shows an additional "I trust this project" acceptance prompt that requires interactive response. Auto-accepting trust prompts was fragile (cursor positioning issues, race conditions). Removed entirely. Agents use Claude CLI's normal interactive permission model. Users grant permissions via the agent panel.

---

## Data Models

### Task lifecycle

```python
class TaskStatus(str, Enum):
    BACKLOG = "backlog"
    PLANNING = "planning"
    EXECUTE = "execute"
    REVIEW = "review"
    DONE = "done"

STAGE_ORDER = list(TaskStatus)  # defines valid transitions
```

### Task

```python
class Task(BaseModel):
    id: str                          # uuid4 hex[:8]
    title: str
    description: str | None
    status: TaskStatus               # current pipeline stage
    agent_override: str | None       # per-task agent (overrides pipeline config)
    session_id: str | None           # active PTY/API session
    worktree_path: str | None        # git worktree directory
    branch_name: str | None          # git branch
    pr_number: int | None
    pr_url: str | None
    docs_path: str | None            # .llm-cc/tasks/<id>/ — shared docs between stages
    created_at: datetime
    updated_at: datetime
```

### Agent config

```python
class AgentConfig(BaseModel):
    name: str                        # e.g. "claude", "codex", "claude_opus"
    command: str | None              # CLI binary (PTY mode)
    args_template: str               # how prompt is passed, e.g. "{prompt}"
    model: str | None                # model name — shown in UI, auto-injected as --model flag
    mode: AgentMode                  # "pty" or "api"
    api_provider: str | None         # "anthropic" or "openai" (API mode)
    api_model: str | None            # model ID (API mode)
    resume_template: str | None      # template for session resume
    co_author: str                   # git co-author line
    detect_command: str | None       # command to check availability
```

### Pipeline stage

```python
class PipelineStage(BaseModel):
    stage: TaskStatus                # which stage this config applies to
    agent: str                       # agent name from [agents.*]
    mode_override: AgentMode | None  # override agent's default mode
    prompt_template: str | None      # custom prompt (future)
    cli_flags: str                   # extra CLI flags
    auto: bool                       # auto-run without user trigger (future)
```

### Project config

```python
class ProjectConfig(BaseModel):
    name: str
    github_url: str | None
    git: GitConfig
    pipeline: list[PipelineStage]
    agents: dict[str, AgentConfig]
    stage_labels: dict[str, str]         # custom column labels
    plan_dir: str = ".llm-cc/tasks/{id}" # template: {id}, {slug}, {branch}, {title}
    plan_file: str = "plan.md"           # constant filename within plan_dir
    review_file: str | None = None       # optional: where review agent writes summary (in plan_dir)
```

### Git config

```python
class GitConfig(BaseModel):
    mode: GitMode = GitMode.NONE         # "none", "worktree", or "branch"
    base_branch: str = "main"            # auto-detected from repo
    branch_prefix: str = "task/"         # prefix for branch names
    copy_files: list[str] = []           # files to copy into worktrees
    init_script: str | None = None       # script to run in new worktrees
```

### Config hierarchy

```python
class MergedConfig(BaseModel):
    project: ProjectConfig           # from .llm-cc/config.toml
    global_cfg: GlobalConfig         # from ~/.config/llm-cc/config.toml
    agents: dict[str, AgentConfig]   # merged: defaults + project overrides
    pipeline: list[PipelineStage]    # project pipeline or defaults
```

Resolution: `project agents > default agents`, `project pipeline > default pipeline`.

---

## Agent Backend Protocol

```python
@runtime_checkable
class AgentBackend(Protocol):
    async def start(config, task, prompt, cwd, stage, cli_flags) -> str  # returns session_id
    async def resume(session_id, prompt) -> None
    async def stop(session_id) -> None
    async def send_input(session_id, text) -> None
    async def send_raw(session_id, data) -> None      # raw bytes for key forwarding
    async def get_output(session_id) -> str
    def is_alive(session_id) -> bool
```

### PTY Backend

Spawns CLI agents in native pseudo-terminals via pexpect:

1. **Command construction**: `{command} --model {model} {cli_flags} {args_template.format(prompt=quoted_prompt)}` — model flag auto-injected when `model` is set and `{model}` not in `args_template`
2. **Output polling**: background asyncio.Task reads PTY at 0.1s intervals, feeds data into pyte `OutputBuffer` (uses `HistoryScreen` with 5000-line scrollback and Rich text rendering)
3. **DSR response**: responds to cursor position queries (`\x1b[6n`) that some CLIs (e.g., Codex) use for terminal size detection
4. **Disk logging**: all PTY output written to `.llm-cc/logs/{session_id}.log`
5. **Session IDs**: `pty_{task_id}_{stage}` — e.g. `pty_a1b2c3d4_planning`
6. **Orphan prevention**: `start()` stops old session if same ID exists; `_ProcessManager` singleton registered with `atexit` kills all children on crash

### API Backend

Calls Anthropic/OpenAI SDKs directly:

1. SDKs imported lazily (only on first API call) — won't fail if not installed
2. Results persisted to disk same as PTY logs
3. Async task tracking — can check if API call is still running

---

## Storage

### Atomic writes

```
1. Acquire fcntl.LOCK_EX on tasks.lock
2. Read tasks.json
3. Modify in memory
4. Write to tempfile in same directory
5. os.replace(tempfile, tasks.json)  — atomic on POSIX
6. Release lock
```

No TOCTOU gap — single lock held across read-modify-write. JSON decode errors return empty store instead of crashing.

### Config loading

```
1. Read ~/.config/llm-cc/config.toml  → GlobalConfig
2. Read .llm-cc/config.toml          → ProjectConfig
3. Merge: project agents override defaults, project pipeline or default pipeline
4. Return MergedConfig (used everywhere at runtime)
```

TOML parsed via stdlib `tomllib` (Python 3.12+). No extra dependency.

---

## Pipeline Engine

### Stage transitions

```python
async def advance(task):
    # 1. Compute next stage (skipping unconfigured stages)
    # 2. Stop current agent (capture output to file if leaving active stage)
    # 3. Ensure task docs dir exists (.llm-cc/tasks/<id>/)
    # 4. Match on next stage:
    #    PLANNING: git setup + start agent
    #    EXECUTE:  git setup (if no PLANNING) + validate slot (NONE/BRANCH) + start agent
    #    REVIEW:   write diff.md + start agent
    #    DONE:     cleanup git, clear session
    # 5. Save task to storage

async def revert(task):
    # 1. Capture current stage output to file
    # 2. Stop current agent
    # 3. Move status back one step
    # 4. Save

async def restart(task):
    # 1. Stop current agent
    # 2. Re-generate context files (diff.md for review)
    # 3. Start new agent at same stage
    # 4. Save
```

### Prompt building

Per-stage prompt builders. Each is minimal — points agent at task docs and plan file:

- **Planning**: task description + plan path to write to
- **Execute**: task description + plan path to read
- **Review**: task description + plan path + diff.md

The agent discovers context by reading files. The planning agent writes a plan to the configured `plan_dir/plan_file`. The execute agent reads it. The review agent gets a pre-written `diff.md`. Users configure agent behavior via `CLAUDE.md` and project-level agent config — not through our prompt templates.

### Execute slot validation

When `git.mode` is `"none"` or `"branch"`, only one task can be in Execute at a time (agents share the working directory). With `"worktree"` mode, unlimited concurrent Execute tasks.

---

## Board Screen

### Columns

`KanbanColumn` widgets in a `Horizontal` container, one per configured stage (from `MergedConfig.active_stages()`). Only stages with `[[pipeline]]` entries are shown; BACKLOG and DONE always appear. Each column holds `TaskCard` widgets rendered from `TaskStore.by_status()`.

Column headers show: stage name, agent name, model name, and task count. Agent and model are resolved from the `[[pipeline]]` stage config and the corresponding `[agents.*]` entry.

### Task card indicators

| Indicator | Condition | Meaning |
|-----------|-----------|---------|
| `[bold red]STALE[/]` | Active stage + no session_id | Agent died, needs restart (`r`) |
| `[bold yellow]WAITING FOR INPUT[/]` | `OutputBuffer.appears_waiting` is True | Agent idle — permission prompt, completion, or needs input |
| `[agent_name]` | Has session_id, not waiting | Agent running normally |

### Async workers

All pipeline operations run in Textual `@work(exclusive=True, group="pipeline")` workers. This:
- Prevents concurrent pipeline operations (exclusive=True)
- Keeps the UI responsive (work runs in background)
- Groups related operations (advance/revert/restart/stop/delete share one group)

Diff viewing uses a separate `group="diff"` so it doesn't block pipeline ops.

### Stale task cleanup

On app startup, `_cleanup_stale_sessions()` scans all tasks in PLANNING/EXECUTE/REVIEW. If `session_id` is set but no backend recognizes it as alive, clears `session_id`. This handles tasks left running when the app crashed.

---

## Reliability

### Storage race conditions
Separate lock file (`tasks.lock`). Lock acquired before any read or write. Atomic write via temp file + `os.replace()`. `save_task()` holds single lock across read-modify-write.

### Session orphaning
`PtyBackend.start()` stops old session if same ID exists before spawning. `_ProcessManager` singleton registered with `atexit` kills all PTY children on crash.

### Stale task objects in async workers
All `@work` methods accept `task_id: str`, not `Task` object. Workers re-read from storage before mutating. Confirmation dialog closures capture `task_id` (stable), not task object (may be stale).

### Buffer memory
`PtyBackend.stop()` cleans up buffers (close log file handle, remove from dict). Log file opened once per session, not per-append. `HistoryScreen` caps scrollback at 5000 lines to bound memory usage.

### Key event isolation
AgentPanel `on_key` always calls `event.prevent_default()` + `event.stop()` when in input mode. Prevents board-level vim bindings from firing while typing in the agent panel.

### Process cleanup
`_ProcessManager` singleton tracks all spawned PTY children. Registered with `atexit` — kills children on crash. `PtyBackend` registers/unregisters on start/stop.

---

## Install

```bash
# From source
uv sync
uv run llm-cc /path/to/project

# With AI SDK support
uv sync --extra anthropic
uv sync --extra all
```

### Requirements

- Python 3.12+ (for `tomllib`, union types, match statements)
- macOS (uses POSIX PTY via `pexpect`, `fcntl` for file locking)
- `git` for worktree/branch/diff operations
- `gh` CLI for PR creation (optional)
- Agent CLIs: `claude`, `codex`, `aider`, etc.

### Dependencies

```
textual>=1.0       # TUI framework
pydantic>=2.0      # data models + validation
pexpect>=4.9       # PTY management
pyte>=0.8          # VT100 terminal emulator

# Optional
anthropic>=0.80    # API backend
openai>=1.60       # API backend
```

## Files generated

```
~/.config/llm-cc/
└── config.toml              # global: default_agent, recent_projects, theme

<project>/.llm-cc/
├── config.toml              # project: pipeline, agents, git config
├── tasks.json               # task data (JSON, file-locked)
├── tasks.lock               # fcntl lock file
├── tasks/                   # per-task docs (context between stages)
│   └── <task-id>/
│       ├── task.md
│       ├── plan.md           # (default location; configurable via plan_dir)
│       ├── planning-output.md
│       ├── diff.md
│       └── review-output.md
├── logs/                    # agent PTY output logs
│   └── pty_<id>_<stage>.log
└── worktrees/               # git worktrees (if git.mode = "worktree")
    └── <id>-<slug>/
```

Add `.llm-cc` to your global gitignore (check `git config --global core.excludesFile` for your file location):

```bash
echo ".llm-cc" >> "$(git config --global core.excludesFile)"
```

---

## Future

- [ ] Search overlay (fuzzy task search)
- [ ] Hot-reload config (file watcher)
- [ ] Tool use for API review agents
- [ ] PR creation dialog widget
- [ ] Tests (pytest)
