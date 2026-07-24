# Quality Refactor — Plan

## Purpose
Behavior-preserving quality pass over the whole codebase, from an independent review.
Six workstreams: polling performance, duplication, backend protocol, silent failure,
dead code, types/lint. **No logic changes** — every user-visible behavior stays identical.

Baseline at start (verified): `137 passed`, `ruff check` = 34 E501, `mypy src/` = 71 errors.

## Measured problem (why item 1 leads)
On this machine: `tmux has-session` = 8.8 ms/call, `capture-pane` = 6.3 ms/call.
`is_alive()` is a **blocking** `subprocess.run` called from async code at 5 Hz per session
(`agents.py:_poll_output`), plus again in `health()`, plus board (0.5 Hz) and panels (1 Hz).

| | per session | 4 agents |
|---|---|---|
| event-loop stall from `is_alive` @5Hz | 44 ms/s | **176 ms/s** |
| total tmux work in `_poll_output` | 139 ms/s | 556 ms/s |

Verified by probe (`Text.from_ansi(ansi).plain` vs `capture-pane -p`): identical after
`display()` normalization → the separate plain capture is redundant and can be derived
in-process.

## Workstreams

### 1. Polling performance (`agents.py`, `ui/panels.py`)
- [x] Liveness from the async `_capture` instead of a blocking fork; TTL cache (1 s) for
      external `is_alive()` callers, refreshed by the poll loop.
- [x] Derive plain viewport from the ANSI capture (`Text.from_ansi(...).plain`) — drops one
      fork/tick and removes a latent skew between `display()` and `display_rich()`.
- [x] Capture 5000-line scrollback at 1 Hz instead of 5 Hz (it only changes when lines scroll off).
- [x] Cache parsed `Text` for viewport + history, keyed on the raw ANSI string.
- [x] Cache cleaned/lowered `display()` text; invalidate on write.
- NOT doing: adaptive poll rate when no viewer attached — would change `_stable_ticks`
  semantics (the `>= 3` gate is 0.6 s at 5 Hz) and therefore detection timing.

### 2. Duplication
- [x] `pipeline`: `_build_stage_prompt` dispatch dict (was 3× `match`), `_merge_stage_tools`
      (was 3×), `_start_stage_agent` (was 3× in `advance`), `restart`/`context_restart` unified.
- [x] `agents`: `_teardown(session_id, kill)` shared by `stop`/`detach`.
- [x] One `sanitize_session_name` (was 3 copies / 2 implementations across agents + panels).
- [x] `health.status_kind()` — one status ladder for board card, board poll, and web state.
- [x] `AgentHealth.context_tokens` — token sum was derived in 4 places.

### 3. `AgentBackend` protocol is real
- [x] Protocol declares the full surface the UI consumes; both backends implement all of it.
- [x] Delete 17 `hasattr(backend, ...)` probes and the `isinstance(backend, TmuxBackend)` checks.

### 4. Silent failure
- [x] `logging` to `.llm-cc/logs/llm-cc.log`, configured once at startup.
- [x] Replace consequential `except Exception: pass` with logged handlers (targeted, not all 74).

### 5. Dead code
- [x] `AgentConfig.resume_template`, `AgentConfig.display_label`, `PipelineStage.prompt_template`,
      `OutputBuffer.total_lines`, `OutputBuffer._cols`, `OutputBuffer.stats` 3-tuple
      (`_total_bytes`/`_last_output_time` were write-only; `HealthScorer` tracks the same thing).
- [x] `reattach_existing` `cleared` flag (`if cleared: pass`).
- [x] `_sessions: dict[str, str]` mapping keys to themselves → `set[str]` (both classes).

### 6. Types & lint
- [x] All 34 E501.
- [x] `BoardScreen.pipeline`/`registry` non-optional (only ever constructed with both in `app.py`)
      → removes 6 union-attr errors and the dead no-pipeline fallback branches.
- [x] Annotate `web/server.py` handlers (33 errors), `pipeline.py` untyped defs, generics on
      `Screen`/`App`, `_poll_timer`, `push_screen` callbacks, `__main__._import_tasks`.
- [x] Hoist stdlib imports out of function bodies (`storage.py` `time`/`shutil`, `utils.py`
      `shlex`, `git.py` `subprocess`, `panels.py` `rich.syntax`). Genuinely-lazy imports
      (`anthropic`, `openai`, `uvicorn`, `web.server`) stay lazy — they are optional extras.

## Research result: `_INPUT_PATTERNS` false positives — CLAIM WITHDRAWN
Tested all 12 patterns against 4 real captured agent logs (657-line planning session + 3 short):
**zero occurrences of any input pattern, anywhere, in any log.** `allow`, `deny`, `y/n`,
`press enter` etc. all count 0. The claim was wrong. Reasons:
- Claude's permission prompts render in the alt-screen and are transient — they don't persist
  in the pane transcript the way I assumed.
- The `_stable_ticks >= 3` gate requires a still screen, and a still screen usually *is* an
  idle agent, so the heuristic's failure mode is benign.
`_INPUT_PATTERNS` is left untouched.

## Real bug found instead — FIXED (sentinel + whole-line match)
`_COMPLETE_PATTERNS` genuinely false-positived. The prompts we build embed the literal marker
("When finished, say: EXECUTE COMPLETE" — `pipeline._build_execute_prompt`), that text is echoed
into the pane, and `appears_stage_complete` substring-matched the *instruction* rather than the
agent's declaration. Confirmed in `llmcc_c6c21bf9_planning.log`: 5 occurrences, none of which
were the agent declaring completion. With `stage.auto = true` this auto-advanced a stage before
any work happened.

**Why a pure sentinel is impossible:** to instruct a literal marker you must write it, and the
prompt is rendered into the agent's own transcript — so the marker is always on screen from the
moment the stage starts. The working form is a distinctive sentinel **plus a whole-line match**:
our instruction always carries text on the same line, an agent's declaration stands alone.

- `agents.STAGE_COMPLETE_MARKER = "<<<LLM-CC-DONE>>>"`, matched only when it is the entire line
  (after stripping CLI gutter decoration — bullets, quote bars).
- `pipeline._DONE_INSTRUCTION` is built from that constant so prompt and detector can't drift,
  and is kept short so a narrow pane can't wrap the marker onto a line of its own.
- Legacy "<stage> COMPLETE" phrasing still matches under the same whole-line rule, so in-flight
  sessions and paraphrasing agents aren't stranded.
- Result cached alongside the other derived views (the poll loops ask several times/second).

Six tests added in `test_waiting_detection.py`, including the real log line verbatim. Verified
non-vacuous: under the previous substring logic the real-log echo returns True (the bug) and the
new marker returns False; both are correct now.

## Validation — all verified after the change

| Check | Before | After |
|---|---|---|
| `uv run pytest` | 137 passed | **143 passed** (6 new regression tests) |
| `uv run ruff check src/ tests/` | 34 errors | **All checks passed** |
| `uv run mypy src/` | 71 errors | **Success, no issues** |
| tmux forks/sec per polled session | 20.0 | **5.5** |
| blocking `has-session` on the event loop | 5/s/session | **0** |

`ruff format --check` still reports 20 files — but it reported 18 at baseline
(verified by stashing), i.e. this repo has never been ruff-formatted. Running it
would reformat files this change never touched, so it was left alone. Worth doing
as its own commit.

Live tmux smoke test (real detached session, since the unit tests fake tmux) confirmed:
derived plain text is byte-identical to `capture-pane -p`, ANSI styling survives,
liveness is served from the poll sample, waiting-detection still fires, scrollback
caching returns a stable object, session death is still detected, and the
`append()` path (ApiBackend/tests) is unchanged.
