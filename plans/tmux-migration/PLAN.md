# Migrate PTY backend from pexpect+pyte to tmux

This is a living document. It is the single source of truth for this work. Any session or agent must be able to read ONLY this file and continue the work without prior context.

## Purpose

Replace the in-process pexpect+pyte terminal-emulator path with tmux as the agent process owner and renderer. After this work:

- Agent sessions survive llm-cc crashes / restarts — `tmux ls` finds them, llm-cc reattaches.
- Terminal fidelity (cursor, alt-screen, mouse, bracketed paste, every key, every escape) is correct because tmux is doing the rendering, not us.
- Remote / external attach works for free (`tmux attach -t llmcc-<task_id>` from any terminal, including over SSH).
- The hand-rolled key map and pyte→Rich color converter (~400 LOC of fragile code) goes away.

How to see it working: spawn a Claude Code agent in a worktree, kill llm-cc with SIGKILL, restart, observe the agent still running and its scrollback intact. Press a key combination Textual didn't previously forward (Shift+Tab, F-key, paste a multi-line block) and observe correct behavior.

In parallel, restructure the UI from "fullscreen kanban + modal agent panel" to "tabbed views with kanban overview" so multi-agent monitoring stops requiring modal switches.

## Goals

1. tmux backend replaces pexpect+pyte for spawning, sending input, capturing output, sizing, and lifecycle.
2. AgentBackend protocol surface stays stable — `pipeline.py`, `health.py`, `storage.py`, `git.py`, `models.py`, `app.py` need zero or minimal changes.
3. Agent sessions persist across llm-cc restarts; reattach on startup.
4. UI restructures to tabbed layout with kanban as overview tab; eliminate modal AgentPanel.
5. ApiBackend (Anthropic/OpenAI SDK path) keeps working unchanged.

## Non-goals

- Replacing Textual with a web UI (separate decision; deferred).
- Mouse forwarding into our integrated terminal viewer (works in tmux-attach; integrated viewer mouse is out of scope here).
- Cross-platform support — macOS only (matches current scope).
- Codex Backend changes beyond verifying it still works under tmux.
- Legacy UI escape hatch — no `LLM_CC_LEGACY_UI` flag.
- pexpect fallback — tmux is a hard requirement; startup fails fast if missing.

## Progress

- [x] M0 — Foundation: dependency check, branch creation, CI updates
- [x] M1 — TmuxBackend: parity rewrite of agents.py PtyBackend → TmuxBackend
- [x] M2 — OutputBuffer rewrite: capture-pane based, Rich.from_ansi rendering
- [x] M3 — Session persistence: reattach on startup, atexit policy decision
- [x] M4 — UI restructure: tabbed layout, eliminate modal AgentPanel
- [x] M5 — Polish: diff syntax, status dots on tabs, quit confirmation
  - Diff: `Syntax(lexer="diff")` rendering in `DiffView`
  - Tab labels show `●` (running) / `◐` (waiting) / `⚠` (error)
  - Quit (`q`) prompts confirmation when active agent tabs are open;
    message reflects `--clean-exit` mode (kill vs leave running)
  - pexpect/pyte already removed in M0/M2; no dead imports remain
  - Deferred: pulse animation, persistent last-active tab

## Current State

Pre-implementation. This plan is the artifact.

- Files changed so far: `src/llm_cc/permissions.py:22` (worktree default `acceptEdits` → `bypassPermissions`, separate change, already on main)
- What's working: existing pexpect+pyte stack
- What's not working yet: nothing in this work; greenfield
- Blocked on: decision on session persistence policy (see Decision Log)

## Surprises & Discoveries

(Fill in during implementation.)

## Decision Log

- **Decided:** Use tmux as backend. Hard dependency, no pexpect fallback. Rationale: clean cut; one path to maintain.
- **Decided:** Session persistence — Option B. tmux sessions outlive llm-cc exit; reattach on startup. Add `--clean-exit` flag for opt-out.
- **Decided:** Session naming `llmcc-<task_id>` (no stage suffix). Stage transitions reuse the same session via `send-keys`; brainstorm sub-agents share one session.
- **Decided:** macOS-only. No Linux/CI matrix expansion.
- **Decided:** No legacy UI escape hatch. M4 replaces modal AgentPanel with tabbed layout in one cut.
- **Decided:** Tabbed UI with kanban as Overview tab + aggregate status header. (Rationale in "UI restructure" section.)
- **Decided:** Keep `OutputBuffer` class name and external API; rewrite internals only. Minimizes blast radius across panels.py / health.py / pipeline.py.
- **Decided:** Keep raw escape-byte wire format for `send_raw` (e.g. `\x1b[A`); TmuxBackend translates to `tmux send-keys`. Panels.py key map stays unchanged.

## Context

### Stack
- Python 3.12+, Textual ≥1.0, pexpect 4.9, pyte 0.8, Pydantic, Rich (via Textual)
- macOS only (POSIX PTY, fcntl locking)
- ~3000 LOC total

### Critical files (full paths)
- `/Users/maximkushner/Documents/GitHub/mkp/llm-command-center/src/llm_cc/agents.py` — backends, OutputBuffer, AgentRegistry (816 LOC; the blast radius)
- `/Users/maximkushner/Documents/GitHub/mkp/llm-command-center/src/llm_cc/health.py` — error/context/health detection (plain-string consumer, no PTY coupling)
- `/Users/maximkushner/Documents/GitHub/mkp/llm-command-center/src/llm_cc/pipeline.py` — stage orchestration; talks to backends via protocol
- `/Users/maximkushner/Documents/GitHub/mkp/llm-command-center/src/llm_cc/ui/panels.py` — AgentPanel, key forwarding, output rendering
- `/Users/maximkushner/Documents/GitHub/mkp/llm-command-center/src/llm_cc/ui/board.py` — kanban; two `isinstance(backend, PtyBackend)` leaks at lines 269, 320
- `/Users/maximkushner/Documents/GitHub/mkp/llm-command-center/src/llm_cc/models.py` — Pydantic models; `Task.session_id` is opaque
- `/Users/maximkushner/Documents/GitHub/mkp/llm-command-center/src/llm_cc/app.py` — entry point, screen wiring, statusline hook
- `/Users/maximkushner/Documents/GitHub/mkp/llm-command-center/tests/test_statusline.py` — patches `pexpect.spawn`; needs rewrite
- `/Users/maximkushner/Documents/GitHub/mkp/llm-command-center/tests/test_waiting_detection.py` — feeds escape-laden strings into OutputBuffer; thin refactor

### Key non-obvious couplings (from audit)
1. **Log path contract.** `pipeline.py:481` reads `.llm-cc/logs/<session_id>.log` for stage-transition compression. Today written by `OutputBuffer._log_file` on every `append`. Replacement: `tmux pipe-pane -o 'cat >> .llm-cc/logs/<id>.log'` at session start. Strip ANSI before piping (or via filter) — Haiku gets cleaner input.
2. **DSR cursor responder** at `agents.py:569-572` answers `\x1b[6n` queries; tmux owns this under tmux. **Test codex specifically** — codex reportedly stalls without DSR reply.
3. **`Task.session_id` format change.** Today `pty_<task>_<stage>`. Under tmux it's the tmux session name (`llmcc-<task_id>` or `llmcc-<task>-<stage>`). Document; otherwise opaque to models.
4. **`isinstance(backend, PtyBackend)`** at `ui/board.py:269, 320`. Easy miss. Either rename class and update both, or expose `supports_input_detection()` on protocol.
5. **`_sessions` direct attribute access** at `agents.py:799`. Keep the attribute or expose a method.
6. **Statusline hook** is process-env dependent: `LLM_CC_TASK_ID` must reach the spawned process. Pass via `tmux new-session -e LLM_CC_TASK_ID=...`.
7. **`_interrupted` set + Ctrl+C semantics** (`agents.py:267, 439-444, 483-485`). `tmux send-keys C-c` is async; keep the `_interrupted` flag pattern — set before/after `send-keys`.
8. **Color fidelity.** Rich's `Text.from_ansi` doesn't behave identically to pyte for 256-color and multi-param SGR sequences. Test color rendering early in M2.

## Plan of Work

### M0 — Foundation (~½ day)

1. Branch `tmux-migration` created. Plan at `plans/tmux-migration/PLAN.md`.
2. Add `shutil.which("tmux")` check to `app.py` startup. If missing, print `brew install tmux` hint and exit non-zero.
3. Update `CLAUDE.md` Architecture section: mention tmux; remove pexpect/pyte references.
4. No backend switch env var — tmux is the only backend.

Note: pexpect/pyte stay in `pyproject.toml` until end of M2 (when both `agents.py` and `OutputBuffer` are rewritten); ripping them during M0 would break the build.

### M1 — TmuxBackend parity (~3-4 days)

Goal: a working `TmuxBackend` that satisfies AgentBackend protocol and can pass existing tests for non-rendering surface.

1. **New file** `src/llm_cc/tmux_backend.py` (or rename `PtyBackend` in place; prefer side-by-side until parity).
2. Create `tmux_cmd(*args, **kwargs)` helper using `asyncio.create_subprocess_exec` (no shell). Capture stdout/stderr, return tuple. Used by every backend method.
3. `TmuxBackend.start(...)`:
   - Build `full_cmd` same as today.
   - Compute session name: `llmcc-<task_id>-<stage>` (sanitize for tmux: alnum + `_-`).
   - `tmux new-session -d -s <session> -x <cols> -y <rows> -e LLM_CC_TASK_ID=<tid> -e ... <full_cmd>`
   - On success: register session in `_sessions`, start pipe-pane logger.
4. `TmuxBackend._start_pipe_pane(session, log_path)`:
   - `tmux pipe-pane -t <session> -o 'cat >> <log_path>'`
   - Consider piping through `sed` to strip ANSI for compression friendliness; punt to M2 if simpler.
5. `TmuxBackend.send_input(session, text)`: `tmux send-keys -t <session> -- <text> Enter`. Use `--` to handle leading dashes safely.
6. `TmuxBackend.send_raw(session, raw)`: translation table:
   - `\x03` → `send-keys C-c`
   - `\r` → `send-keys Enter`
   - `\t` → `send-keys Tab`
   - `\x7f` → `send-keys BSpace`
   - `\x1b[A/B/C/D` → `Up/Down/Right/Left`
   - `\x1b[Z` → `BTab` (Shift+Tab — new capability)
   - Printable text → `send-keys -l <text>` (literal mode preserves spaces)
   - For complex sequences not in the table, fall back to `send-keys -H` with hex bytes.
7. `TmuxBackend.is_alive(session)`: `tmux has-session -t <session>`; return code 0 = alive.
8. `TmuxBackend.resize_session(session, cols, rows)`: `tmux resize-window -t <session> -x <cols> -y <rows>`.
9. `TmuxBackend.stop(session)`: `tmux kill-session -t <session>`. Idempotent.
10. `TmuxBackend.resume(session, prompt)`: same as today — call `send_input(session, prompt)`. Tmux session keeps running, just feeds new input.
11. Keep status-file machinery (`_read_status_file`, `status_data`, `health`) **untouched** — orthogonal to PTY.
12. `_ProcessManager` → simplify to a session-name set; `atexit` policy from D1.

Validation for M1:
- `uv run pytest tests/test_brainstorm_pipeline.py tests/test_brainstorm_models.py` — pass
- Manual: `LLM_CC_BACKEND=tmux uv run llm-cc <project>`, create task, advance to PLANNING, observe agent spawn, check `tmux ls`, send keys, observe response.
- Manual: kill llm-cc with SIGKILL, restart, run `tmux ls` — session still there.

### M2 — OutputBuffer rewrite + Rich rendering (~2 days)

Goal: replace pyte-based rendering with capture-pane + Rich.from_ansi while keeping public API.

1. New `OutputBuffer` internals:
   - Drop `pyte.HistoryScreen`, `pyte.Stream`, `_screen`, `_stream`.
   - Add `_session_name: str | None`, `_cached_viewport_text: str = ""`, `_cached_viewport_ansi: str = ""`, `_cached_history_ansi: str = ""`.
   - `append(data)` — keep for ApiBackend path; for tmux path, called periodically by backend with `capture-pane -p` plain text snapshot (replaces stream).
   - `display()` — return cached plain text.
   - `display_rich()` — `rich.text.Text.from_ansi(self._cached_viewport_ansi)`.
   - `history_rich()` — same pattern from `_cached_history_ansi`.
   - `mark_idle()` / `stable_ticks` / `appears_waiting` / `appears_stage_complete` — unchanged.
   - `total_lines`, `stats` — unchanged.
2. TmuxBackend periodic capture (every 200ms while AgentPanel mounted):
   - Plain: `tmux capture-pane -t <s> -p` → feed `OutputBuffer.append`/refresh cache.
   - ANSI viewport: `tmux capture-pane -t <s> -e -p` → cache.
   - ANSI history: `tmux capture-pane -t <s> -e -p -S -<scrollback> -E -1` → cache.
3. **Critical:** verify viewport/history split — capture without `-S` returns visible viewport only. Test with output that exceeds the screen.
4. Test color fidelity: spawn an agent that emits 256-color and truecolor text; compare rendering before/after migration.
5. Remove `_pyte_color_to_rich` (`agents.py:116-139`), `_row_to_rich` (`agents.py:141-185`), pyte imports.

Validation for M2:
- `uv run pytest tests/test_waiting_detection.py` — pass after refactor (input format change to plain text).
- Manual: open AgentPanel, observe colors match what tmux-attach shows.
- Manual: scroll up in panel, see history; resize terminal, see correct reflow.

### M3 — Session persistence + reattach (~1 day, depends on D1)

If D1 = Option B:

1. On `app.py` startup, after loading tasks: scan `tmux ls -F '#{session_name}'` for `llmcc-*` patterns.
2. For each match, look up the corresponding Task by parsed session id; if Task exists and `status` ≠ DONE, reattach (set `task.session_id`, register in `AgentRegistry._sessions`).
3. Orphan tmux sessions (no matching task) → log a warning, optionally offer to kill.
4. Remove or guard `atexit.register(_process_manager.cleanup_all)` (`agents.py:742`) so exit doesn't kill sessions. Add `--clean-exit` flag.
5. Add `app.py` command `llm-cc kill-all-sessions` for manual cleanup.

Validation for M3:
- Start agent in EXECUTE, kill llm-cc with SIGKILL, restart, observe board shows agent as alive, AgentPanel reattaches and shows scrollback.

### M4 — UI restructure: tabs over modal (~3-4 days)

**Design rationale.** Current modal `AgentPanel` (`ui/panels.py:146`) costs peripheral awareness — when watching one agent you can't see board state. Tabs solve this without sacrificing the kanban overview.

**Layout:**

```
┌─────────────────────────────────────────────────────────┐
│ [Overview] [● Task A] [◐ Task B] [✓ Task C]   3↑ 1⏸ 0⚠ │  ← tab bar + aggregate
├─────────────────────────────────────────────────────────┤
│                                                          │
│   <body: depends on active tab>                          │
│                                                          │
│   Overview tab → kanban                                  │
│   Task tab    → agent terminal (top 70%)                 │
│                  status sidebar (bottom 30%)             │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

- Tab bar status dots:
  - `●` (green) — agent active, healthy
  - `◐` (yellow) — waiting for input
  - `✓` (gray) — completed/idle
  - `⚠` (red) — error or critical context
- Aggregate counters in top-right: running / waiting / errors. Always visible.
- Number keys 1-9 jump to tab N. `o` jumps to Overview. `[`/`]` cycle tabs.
- Tabs auto-create when a task spawns an agent; auto-archive (still openable) when DONE.
- Long tab list: arrow-key scroll OR fuzzy switcher (`Cmd+P` style — `:`).

**Implementation steps:**

1. Replace `ModalScreen` AgentPanel with non-modal `Container` widget that lives inside a Textual `TabbedContent`/`TabPane` structure.
2. New `MainScreen` (Textual `Screen`): contains `TabbedContent` at root, `BoardScreen` content as the first tab.
3. Each task with active agent → dynamically add a `TabPane`. Tab label includes status dot.
4. Tab body: `Vertical` of `AgentTerminalView` (70% height) + `TaskStatusSidebar` (30% height) showing health, context %, recent errors, action buttons (advance, restart, stop, diff).
5. Keybinding refactor: replace existing 17-binding footer with a compact one + `?` for full reference.
6. Aggregate header widget: poll all backends every 2s, render counts.
7. DONE column: auto-collapse when >5 cards; click/hover to expand.
8. Quick task switcher: `:` opens fuzzy filter over all tasks (using Textual's input).

Validation for M4:
- Manual: have 4 active agents, switch between them via number keys without losing board state.
- Manual: trigger an error in agent 3; observe red dot on tab and aggregate counter.
- Manual: open `:`, type partial title, hit enter, jump to that task's tab.
- All existing keybindings still work for existing operations.

### M5 — Polish (~1-2 days)

1. Ctrl+C / quit confirmation when active agents exist.
2. Diff viewer: `Syntax(lexer="diff")` for color (carries over from quick-wins recommendation).
3. Status indicators: pulse animation for "waiting" tabs.
4. Persistent UI state: remember last-active tab across restarts.
5. Cleanup: remove `pexpect` import sites that are dead; bump pexpect to optional in pyproject.

## UI restructure decision (D2 — answer)

Considered five layouts (kanban+dock, tabs, hybrid, tiled grid, dashboard+drill).

**Chosen: hybrid tab bar with kanban as first tab + aggregate header.** Reasons:
- Solves the dominant UX failure (modal switch between board and agent) without sacrificing project-state visibility.
- Tabs are familiar (browser, IDE, tmux). Status dots in the tab bar give global awareness without leaving the current tab.
- Scales to many tasks (overflow + fuzzy switcher).
- Simpler than tiled grid; gives more room per agent than dock.
- Keeps kanban for "where is everything in the pipeline" question.

Rejected:
- Persistent dock (kanban above, agent below): kills agent-screen real estate; fights with Textual's Header/Footer; agents need full width for code editing UIs.
- Pure tabs without kanban: loses pipeline-state overview; project state becomes a scattered fact across tabs.
- Tiled grid: <6 agents practical, terminals unreadable when small.
- Dashboard + drill-down: an extra layer that adds clicks without solving peripheral awareness.

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Codex stalls without DSR reply | Test codex specifically on day 1 of M1. If broken, add a thin DSR shim (poll `tmux display -p '#{cursor_x}'` and inject reply via send-keys). |
| Color fidelity regressions vs pyte | M2 includes side-by-side color test against current build; document any deltas. |
| tmux not installed on user machine | Startup check, clear install hint, pexpect fallback retained for one release. |
| `tmux send-keys` paste perf for large prompts | If slow, use `set-buffer` + `paste-buffer` for >1KB. |
| Session name collisions across worktrees of same task | Sanitize + include task hash in session name. |
| Tests break in non-obvious ways | Run full test suite at end of each milestone; flag changes to test files in plan progress section. |
| UI redesign feels worse before it feels better | Keep old `AgentPanel` modal accessible behind a flag (`LLM_CC_LEGACY_UI=1`) for one release as escape hatch. |

## Validation

End-to-end acceptance after M3:

```bash
# Migration parity
uv run pytest                      # all green
uv run mypy src/                   # clean
uv run ruff check src/ tests/      # clean

# Manual smoke
uv run llm-cc /path/to/project     # board renders, all stages shown
# create a task, advance to PLANNING, observe agent spawn
tmux ls                            # llmcc-<id> session exists
# send a key not in old map (Shift+Tab)
# observe forwarded correctly
# kill -9 $(pgrep -f llm-cc)
uv run llm-cc /path/to/project     # session reattaches, scrollback intact
```

End-to-end acceptance after M4:

- Open 4 tasks in EXECUTE simultaneously.
- Number keys 1-4 switch between agent tabs without losing state.
- Aggregate header shows `4↑` (running).
- Trigger a Ctrl+C in agent 2; observe `◐` indicator on tab 2.
- Press `o` to return to overview, see kanban with same 4 tasks in EXECUTE column.

## Outcomes & Retrospective

(Fill at completion.)

## Open questions for user

(All decisions resolved. New questions surface here as implementation progresses.)
