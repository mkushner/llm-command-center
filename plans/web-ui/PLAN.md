# llm-cc --web — Plan

## Purpose
Add a browser frontend (`llm-cc --web`) as an ALTERNATE frontend to the existing Textual TUI.
Same backend objects (`Storage` / `AgentRegistry` / `PipelineEngine` / `GitWorkspace`), no logic
rewrite. Goal: nicer, prettier, faster switching between parallel agent contexts, with
Claude-Code-fidelity live terminal I/O (see + type into each agent).

Key principle: **add, don't migrate.** TUI stays. Both frontends attach to the same tmux sessions
and same `.llm-cc/tasks.json` (fcntl-locked, multi-process safe). Instant reversibility.

## Settled architecture
- **Backend layer reused 1:1** (~3400 lines untouched): `pipeline.py`, `agents.py` (TmuxBackend),
  `git.py`, `storage.py`, `models.py`, `health.py`, `permissions.py`.
- **New: web server** (Starlette/FastAPI + uvicorn, added under `[project.optional-dependencies]`
  or core). Owns the same `pipeline`/`registry`/`storage` objects `app.py:60-73` builds.
- **Terminal I/O = xterm.js + PTY bridge to `tmux attach`.** Per-session WebSocket: spawn a PTY
  running `tmux attach -t <session>`, pipe PTY→WS (raw bytes) and WS→PTY (keystrokes). Byte-for-byte
  real terminal in a browser tab. Replaces the capture-pane polling + Rich pipeline entirely for the
  live view (that pipeline stays for board-card telemetry / waiting detection).
- **Entry point**: `__main__.py:57 main()` — add `--web` (and `--port`) branch before `app.run()`;
  reuse existing startup side-effects (tmux check, ensure_dirs, reattach, statusline hook).

## Backend API surface (derived from feature inventory — parity)
### REST (thin wrappers over existing methods)
- `GET /api/state` → board snapshot: visible stages (`active_stages`), stage labels
  (`label_for`), per-stage agent/model, tasks grouped by status, aggregate header counts.
- `GET /api/task/{id}` → full task + resolved agent + health/telemetry + status chip state.
- `POST /api/task` create, `PUT /api/task/{id}` edit, `DELETE /api/task/{id}` delete
  (title/description/verify/done/checkout_branch; multi-line title split handled client-side).
- `POST /api/task/{id}/advance | /revert | /restart | /stop` → pipeline.advance/revert/restart/stop.
- `GET /api/task/{id}/diff` → `git.diff_from_base` (render with diff highlighting client-side).
- `POST /api/task/{id}/input` → `backend.send_input` (text-input mode line send; PTY WS handles raw).
- (decision) `POST /api/task/{id}/pr` → wire `PRManager.create` OR drop. Currently DEAD code.
### WebSocket
- `/ws/board` — push board state every ~2s (mirror `_poll_agent_status`): health, ctx%, tokens,
  status chips, waiting/complete flags, aggregate header, tab status dots, notifications/attention.
- `/ws/pty/{session_id}` — bidirectional raw terminal bridge (tmux attach). Handles resize msgs.

## PARITY CHECKLIST (must not lose)
### Board / cards
- [ ] Columns from `active_stages()`: BACKLOG + configured pipeline stages + DONE, custom labels.
- [ ] Column header: label + count; active-stage meta line agent·model; empty-state (backlog/done).
- [ ] Card: title, status chip, description (truncated), brainstorm line (cycle X/Y · agent),
      telemetry (agent label, health score+color, ctx bar+%, token count) when live & not stale.
- [ ] Status chip priority: error ⚠ → needs-restart ● → ready ● → waiting ● → running ● → none.
- [ ] Stale = active stage but no session_id (dimmed + needs-restart chip).
### Navigation / actions (keyboard parity, web-idiomatic)
- [ ] Task CRUD: new (o), edit (e), delete (x, confirm).
- [ ] Pipeline: advance (m), revert (b), restart (r), stop (s, confirm), diff (d).
- [ ] Open agent (enter), overview (ctrl+o), next/prev tab (ctrl+←/→), close tab (ctrl+w).
- [ ] Help (?), quit (n/a for web — closing tab), command palette (new, web-idiomatic).
### Agent terminal view
- [ ] Live PTY: see execution + type (raw forward). Text-input line send. Waiting-for-input banner.
- [ ] Status bar: heartbeat glyph, ctx %, in/out/cache tokens. Titlebar: agent·model·branch·title.
- [ ] Scroll history + tail-follow/pause. Copy output / paste clipboard (browser Clipboard API).
- [ ] Interrupt (ctrl+c → \x03 to PTY).
### Poll loop behavior (→ /ws/board, ~2s)
- [ ] Health fetch; completion vs waiting detection; auto-advance on `stage.auto`; brainstorm
      auto-advance within stage; auto context-restart at threshold; tab sync; aggregate header.
- [ ] Attention signals (replace desktop bell): waiting-for-input, stage-complete, context-critical,
      error. Web: title badge / favicon / sound / toast.
### Startup / lifecycle
- [ ] tmux required; ensure_dirs/gitignore/recent; reattach_existing (toast N reattached);
      statusline hook; --tasks import; --clean-exit vs detach on shutdown.
### Config respected
- [ ] stages/labels, agents (name/model), git mode, `auto`, `context_restart_threshold`,
      `auto_open_agent_tabs`, agent resolution (task override → stage → default).
### Flagged
- PR creation (`git.py:319 PRManager.create`) is UNWIRED today — decide: wire in web or leave out.
- macOS clipboard → browser Clipboard API. Desktop bell → web attention (badge/sound).

## Decisions (RESOLVED with user)
1. **Frontend stack: React + Vite.** Built `dist/` served static from Starlette. (confirmed)
2. **Layout: Sidebar + tabbed main (variant A).** Persistent left sidebar = live sessions,
   sorted attention-first. Right = tab strip (one tab = Board/kanban, others = agent terminals). (confirmed)
3. **Multi-agent view: BOTH focus + grid, grid is the HOME/landing view.** Land on a grid of all
   live agent terminals (see everyone at once). **Click a grid cell OR a sidebar row → drill into
   FOCUS** (full terminal for that agent, where you type replies). Back to grid via ⌘G / Esc.
   Attention ring (waiting/error/ready) on grid cell + sidebar row + tab + browser-title badge + sound. (confirmed)
4. Scope: walking skeleton first (sidebar + grid of live terminals + drill-to-focus + board tab),
   then fill parity checklist.
5. **PR button: DROP for v1** (dead code today; revisit later). (default taken)

Prototype (design reference): scratchpad/web-ui-prototype.html
  → https://claude.ai/code/artifact/5d5032e6-7650-474e-a1ef-5db331018593

## Backend contract (IMPLEMENTED — src/llm_cc/web/)
- `server.py create_app(project_path)` builds Storage/registry/git/pipeline on app.state; lifespan
  reattaches sessions on startup, detach_all/cleanup_all on shutdown. `run_web(path,host,port)` (uvicorn).
- REST: GET /api/state; POST /api/task; PUT|DELETE /api/task/{id};
  POST /api/task/{id}/{advance|revert|restart|stop} (serialized via app.state.pipe_lock);
  GET /api/task/{id}/diff.
- TaskDTO: {id,title,description,status,session_id,agent,model,branch,status_kind
  ('error'|'ready'|'waiting'|'running'|'stale'|null), health, health_color, context_remaining,
  context_color, tokens, top_error, stale, attention, preview}.
- WS /ws/board → pushes state JSON (~1.5s). WS /ws/pty/{session_id}?cols=&rows= → BINARY frames out
  (PTY output), TEXT JSON in ({"t":"i","d":keys} / {"t":"r","c":cols,"r":rows}). tmux attach bridge.
- `--web [--host --port]` wired in __main__.py. Deps: `web` extra (starlette, uvicorn[standard]).
- VERIFIED: PTY bridge output+input+session-survives-detach on real tmux; build_state builds clean.

## Security (done — from automated review)
- CSWSH: /ws/board + /ws/pty validate Origin before accept (same-origin OR loopback), close 1008 otherwise.
  CSRF guard middleware rejects cross-origin POST/PUT/DELETE. `_origin_ok()` in server.py — 8 cases verified.
- Argument injection: session_id validated `[A-Za-z0-9_-]{1,64}` in pty_ws before _session_alive/attach.
- Deliberately NOT adding a per-run token (Origin check is the canonical CSWSH defense for a loopback
  single-user tool; token would be over-engineering). Revisit if binding to non-loopback becomes common.

## Deferred (parity phase, don't forget)
- Statusline hook install (app.py:_setup_statusline) NOT run in web yet → ctx%/tokens absent until TUI
  installed it. Extract to shared helper + call from web.
- Board WS is read-only; port auto-advance (stage.auto), brainstorm auto-advance, auto context-restart,
  notifications/attention events, aggregate exec-slot. Currently only state push.
- Diff endpoint returns raw text; frontend renders. PR dropped (v1).

## Progress
- [x] Read full backend (agents/panels/app/models/main/styles).
- [x] Feature inventory (parity checklist above).
- [x] Design research → concept → prototype artifact.
- [x] Resolved decisions (layout A / grid-home+drill / React+Vite).
- [x] Backend walking skeleton (server + pty bridge + --web) — VERIFIED.
- [x] Frontend (Vite React, xterm, sidebar+grid+focus+board+dialog+diff) — built.
- [x] End-to-end verified in browser (Playwright): grid home, drill→focus, live xterm,
      typing (direct + reply row), attention ring, board kanban, telemetry, new-task dialog.
- [x] Reply row restored + POST /api/task/{id}/input; tmux status line hidden on attach;
      sidebar/statusbar telemetry (model + ctx% + tokens); responsive pass (tabs toggle pinned,
      focus actions wrap, header/sidebar/statusbar graceful, sidebar ellipsis fix).
- [ ] Parity phase remaining (deferred list above): statusline hook install in web, board-WS
      auto-advance/brainstorm/context-restart, attention sound/title-badge, keyboard shortcuts
      (m/b/r/s/d), tests for web module.

## How to run
- Real: `uv run llm-cc <project> --web` → http://127.0.0.1:7420 (needs `uv sync --extra web` +
  `web/dist` built via `cd web && npm run build`). Dev: `cd web && npm run dev` (proxies to :7420).

## Validation
- Web + TUI run against same project simultaneously without corrupting tasks.json.
- Every parity-checklist item demonstrable in browser.
- `uv run ruff check`, `uv run mypy src/`, `uv run pytest` green.
