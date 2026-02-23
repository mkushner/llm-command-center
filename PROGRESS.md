# Build Progress

## Rules
1. **Update this file** after every step — mark items done, log observations
2. **Verify each phase** before moving to next
3. **Track issues** that need fixing or revisiting

---

## Phase 1: Foundation — COMPLETE

- [x] `pyproject.toml` — uv project config, deps, entry point
- [x] `models.py` — all Pydantic models
- [x] `storage.py` — JSON store (fcntl locking), TOML config, defaults
- [x] `utils.py` — async_run helper, ProcessManager
- [x] `__main__.py` — CLI entry point

## Phase 2: TUI Board — COMPLETE

- [x] `app.py` — Textual App with pipeline stack
- [x] `ui/board.py` — BoardScreen, KanbanColumn, TaskCard, vim keybindings
- [x] `ui/dashboard.py` — DashboardScreen
- [x] `ui/panels.py` — TaskInputDialog, ConfirmDialog
- [x] `ui/styles.tcss` — Dark theme CSS

## Phase 3: Git Integration — COMPLETE

- [x] `git.py` — GitWorkspace (worktree/branch/none modes), PRManager

## Phase 4: Agent System — COMPLETE

- [x] `agents.py` — AgentBackend protocol, PtyBackend, ApiBackend, AgentRegistry, OutputBuffer
- [x] AgentPanel modal + DiffView modal in panels.py

## Phase 5: Pipeline Engine — COMPLETE

- [x] `pipeline.py` — PipelineEngine, stage transitions, agent handoffs

## Phase 6: API Backend + PR — COMPLETE

- [x] Anthropic/OpenAI SDK integration (lazy imports)
- [x] PR creation via PRManager
- [ ] Tool use for review agents — DEFERRED
- [ ] PRDialog widget — DEFERRED

---

## Phase 7: Pipeline Simplification — COMPLETE

- [x] Removed Check, Hook, ReviewVerdict, CheckResult models
- [x] Removed checks runner from pipeline
- [x] Default GitMode changed to NONE
- [x] Removed auto-accept trust/bypass prompts
- [x] Removed `--dangerously-skip-permissions`

## Phase 8: Manual Pipeline with Planning — COMPLETE

- [x] Restored PLANNING stage (5 columns: Backlog, Planning, Execute, Review, Done)
- [x] All transitions manual: `m` advance, `b` back, `r` restart
- [x] Completion markers in prompts (PLANNING/EXECUTE/REVIEW COMPLETE)
- [x] Execute slot validation (one task at a time for GitMode.NONE)
- [x] File-based context flow (.llm-cc/tasks/<id>/ with task.md, diff.md, *-output.md)
- [x] Removed in-memory context passing (review_feedback field → file-based)

## Phase 9: Configurable Plan Paths & Branch Naming — COMPLETE

- [x] `plan_dir` template in ProjectConfig (variables: {id}, {slug}, {branch}, {title})
- [x] `plan_file` constant in ProjectConfig (default: "plan.md")
- [x] `branch_prefix` in GitConfig (default: "task/", configurable per-project)
- [x] `_resolve_plan_path()` in pipeline.py with path traversal protection
- [x] Per-stage prompt builders (_build_planning_prompt, _build_execute_prompt, _build_review_prompt)
- [x] plan_path resolved after git.setup() in PLANNING stage (timing fix)
- [x] Playbook project config: plans/{branch}/PLAN.md, branch mode, empty prefix

## Phase 10: Optional Stages & Dynamic Board — COMPLETE

- [x] Optional pipeline stages: only `[[pipeline]]` entries appear as board columns
- [x] `_next_status()` / `_prev_status()` skip unconfigured stages
- [x] `MergedConfig.active_stages()` returns visible stage list
- [x] Dynamic board columns: `BoardScreen` renders only configured stages
- [x] Column index tracking: `KanbanColumn.col_idx` for click events
- [x] `_follow_task()` safety guard for tasks in invisible stages
- [x] Git none-mode branch detection: detects current branch for `{branch}` template without creating branches
- [x] `git.setup()` called in EXECUTE when PLANNING stage is skipped
- [x] `_resolve_plan_path()` validates `{branch}` before `format()` (clearer errors)
- [x] Playbook config: planning stage removed (agent plans via CLAUDE.md)

## Phase 11: Agent Display, Model Config & Review Output — COMPLETE

- [x] `model` field on AgentConfig: shown in column headers, auto-injected as `--model` flag
- [x] `display_label` property on AgentConfig: `"{name} {model}"` for UI
- [x] Column headers: stage name → agent name → model name → task count
- [x] DSR response in PTY poll loop: responds to `\x1b[6n` cursor position queries (Codex compatibility)
- [x] Execute slot validation: both `GitMode.NONE` and `GitMode.BRANCH` restrict to one task in Execute
- [x] `review_file` on ProjectConfig: optional path where review agent writes summary (resolved in `plan_dir`)
- [x] Review prompt includes `Write your review to: {path}` when `review_file` is set
- [x] Waiting detection reverted to simple implementation (no hysteresis — brief flickering is acceptable)
- [x] Playbook config: `model = "opus-4.6"` for claude, `model = "gpt-5"` for codex, `review_file = "PLAN.md"`

---

## Reliability Fixes — ALL COMPLETE

- [x] Storage: atomic writes (tempfile + os.replace), fcntl locking, no TOCTOU
- [x] Sessions: orphan prevention (stop old before start), atexit cleanup
- [x] Workers: task_id not Task object, re-read from storage before mutating
- [x] Buffer: cleanup on stop, log file opened once per session
- [x] Keys: event isolation in agent panel input mode
- [x] PTY: pyte terminal emulator (not regex ANSI stripping)
- [x] Input detection: full screen search (not bottom N lines), stability + pattern matching
- [x] Worker groups: pipeline ops share group, diff is independent
- [x] Base branch: auto-detect (main → master → develop)
- [x] Stale sessions: cleared on startup

---

## Phase 12: Agent Panel UX — COMPLETE

- [x] Fullscreen agent panel (100% width/height CSS)
- [x] ANSI color preservation: `display_rich()` walks pyte per-character style attributes → Rich `Text`
- [x] Scrollback history: `pyte.HistoryScreen` (5000 lines), `history_rich()` for styled history
- [x] Smart auto-scroll: position-based detection — only scrolls to bottom if user was already there
- [x] Scroll keybinds: Shift+PageUp/Down, Shift+Home/End, mouse wheel
- [x] Dynamic PTY sizing: pyte buffer + pexpect dimensions match real terminal, resize on panel open/terminal resize
- [x] Backend API: `get_output_rich()`, `get_history_rich()`, `resize_session()` on PtyBackend

## Phase 13: Brainstorm Mode — COMPLETE

- [x] PipelineStage: `agents` list, `max_loops`, `summarizer`, `is_brainstorm` property, `agent_at()` method, `model_validator` requiring `agent` or `agents`
- [x] Task: `sub_agent_idx`, `loop_count`, `brainstorm_summarizing` fields
- [x] `agent_for_stage()`: resolves sub-agent via `stage.agent_at(task.sub_agent_idx)`
- [x] Pipeline brainstorm logic: `advance_sub_agent()`, `_spawn_brainstorm_agent()`, `_save_brainstorm_output()`, `_build_brainstorm_prompt()`
- [x] Brainstorm prompt: includes role, cycle info, participants, previous output references, FINAL CYCLE marker
- [x] Summarizer: `_spawn_brainstorm_summarizer()`, `_build_summary_prompt()` — writes to `brainstorm/<task-id>/summary.md`
- [x] Board auto-advance: `_poll_agent_status()` detects dead/idle brainstorm sub-agents, `_do_brainstorm_advance()` worker
- [x] Revert resets brainstorm counters (`sub_agent_idx`, `loop_count`, `brainstorm_summarizing`)
- [x] PTY env fix: strip `CLAUDECODE` from spawn environment (allows nested Claude sessions)
- [x] Idle detection for auto-advance: checks `is_waiting_for_input()` in addition to `is_alive()`
- [x] Clean output capture: agents write output files directly, PTY capture as fallback only
- [x] Tests: `test_brainstorm_models.py` (7 tests), `test_brainstorm_pipeline.py` (8 tests), `test_brainstorm_board.py` (1 test)

---

## Remaining

- [ ] Search overlay (fuzzy task search)
- [ ] Hot-reload config (file watcher)
- [ ] Tool use for API review agents
- [ ] PRDialog widget
- [ ] Tests (test_models.py, test_pipeline.py, test_agents.py, test_git.py)
- [ ] Interactive TUI test (manual)
