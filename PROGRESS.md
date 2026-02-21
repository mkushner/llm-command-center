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

---

## Reliability Fixes — ALL COMPLETE

- [x] Storage: atomic writes (tempfile + os.replace), fcntl locking, no TOCTOU
- [x] Sessions: orphan prevention (stop old before start), atexit cleanup
- [x] Workers: task_id not Task object, re-read from storage before mutating
- [x] Buffer: cleanup on stop, log file opened once per session
- [x] Keys: event isolation in agent panel input mode
- [x] PTY: pyte terminal emulator (not regex ANSI stripping)
- [x] Input detection: full screen search (not bottom N lines)
- [x] Worker groups: pipeline ops share group, diff is independent
- [x] Base branch: auto-detect (main → master → develop)
- [x] Stale sessions: cleared on startup

---

## File Inventory

| File | Lines | Status |
|------|-------|--------|
| `pyproject.toml` | ~45 | Done |
| `src/llm_cc/__init__.py` | 3 | Done |
| `src/llm_cc/__main__.py` | 48 | Done |
| `src/llm_cc/models.py` | 177 | Simplified |
| `src/llm_cc/storage.py` | 219 | Hardened |
| `src/llm_cc/utils.py` | 72 | Done |
| `src/llm_cc/app.py` | 65 | Done |
| `src/llm_cc/ui/board.py` | 521 | Rewritten |
| `src/llm_cc/ui/panels.py` | 254 | Hardened |
| `src/llm_cc/ui/styles.tcss` | 202 | Done |
| `src/llm_cc/git.py` | 258 | Done |
| `src/llm_cc/agents.py` | 496 | Hardened |
| `src/llm_cc/pipeline.py` | 170 | Rewritten |
| **Total** | **~2,530** | |

---

## Remaining

- [ ] Search overlay (fuzzy task search)
- [ ] Hot-reload config (file watcher)
- [ ] Tool use for API review agents
- [ ] PRDialog widget
- [ ] Tests (test_models.py, test_pipeline.py, test_agents.py, test_git.py)
- [ ] Interactive TUI test (manual)
