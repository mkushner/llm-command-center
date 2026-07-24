"""Starlette web server for `llm-cc --web`.

Reuses the exact backend objects the TUI builds (Storage / AgentRegistry /
PipelineEngine / GitWorkspace). REST endpoints are thin wrappers over pipeline
methods, serialized with a single lock (the TUI uses an exclusive worker group
for the same reason). Two WebSockets: /ws/board pushes board state; /ws/pty/*
is the interactive terminal bridge.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ..agents import AgentRegistry, is_clean_exit_mode
from ..git import GitWorkspace
from ..models import Task
from ..pipeline import PipelineEngine
from ..statusline import setup_statusline
from ..storage import Storage
from .state import build_state

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response

# tmux session names are sanitized to this charset (agents._sanitize_session_name).
_VALID_SESSION = re.compile(r"[A-Za-z0-9_-]{1,64}")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _origin_ok(headers: Mapping[str, str]) -> bool:
    """CSWSH / CSRF guard for browser requests.

    Allow when the request is same-origin (Origin netloc == Host — works on any
    bind address, including LAN access from a phone) or the Origin is loopback
    (covers the Vite dev proxy). Reject browser requests from any other site.
    An absent Origin means a non-browser client, which is not the CSWSH threat.
    """
    origin = headers.get("origin")
    if not origin:
        return True
    parsed = urlparse(origin)
    host = headers.get("host", "")
    if host and parsed.netloc == host:
        return True
    return parsed.hostname in _LOOPBACK_HOSTS


def _find_dist() -> Path | None:
    """Locate the built frontend: env override, else the packaged static/ dir."""
    env = os.environ.get("LLM_CC_WEB_DIST")
    if env and (Path(env) / "index.html").exists():
        return Path(env)
    static = Path(__file__).resolve().parent / "static"
    if (static / "index.html").exists():
        return static
    return None


async def _session_alive(session_id: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "has-session",
        "-t",
        session_id,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0


def create_app(project_path: Path) -> Starlette:
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import HTMLResponse, JSONResponse
    from starlette.routing import Mount, Route, WebSocketRoute
    from starlette.staticfiles import StaticFiles
    from starlette.websockets import WebSocket

    from .pty_bridge import attach

    storage = Storage(project_path)
    config = storage.load_config()
    registry = AgentRegistry(config.agents, sessions_dir=project_path / ".llm-cc" / "sessions")
    git = GitWorkspace(project_path, config.project.git)
    pipeline = PipelineEngine(config, registry, git, storage)
    setup_statusline(project_path)  # so agents write ctx%/token telemetry the UI reads

    def _fresh(task_id: str) -> Task | None:
        return storage.load_tasks().get(task_id)

    # --- REST: state ---

    async def state_endpoint(request: Request) -> Response:
        return JSONResponse(await build_state(request.app.state))

    # --- REST: task CRUD ---

    async def create_task(request: Request) -> Response:
        body = await request.json()
        title = (body.get("title") or "").strip()
        if not title:
            return JSONResponse({"error": "title is required"}, status_code=400)
        task = Task(
            title=title,
            description=(body.get("description") or None),
            verify=(body.get("verify") or None),
            done=(body.get("done") or None),
            checkout_branch=(body.get("checkout_branch") or None),
        )
        storage.save_task(task)
        return JSONResponse({"ok": True, "id": task.id})

    async def edit_task(request: Request) -> Response:
        task = _fresh(request.path_params["task_id"])
        if not task:
            return JSONResponse({"error": "not found"}, status_code=404)
        body = await request.json()
        for field in ("title", "description", "verify", "done", "checkout_branch"):
            if field not in body:
                continue
            val = body[field]
            if isinstance(val, str) and field != "title":
                val = val.strip() or None
            setattr(task, field, val)
        task.touch()
        storage.save_task(task)
        return JSONResponse({"ok": True})

    async def delete_task(request: Request) -> Response:
        task = _fresh(request.path_params["task_id"])
        if task and task.session_id:
            with contextlib.suppress(Exception):
                await registry.stop_session(task.session_id)
        storage.delete_task(request.path_params["task_id"])
        return JSONResponse({"ok": True})

    # --- REST: pipeline ops (serialized) ---

    async def _stop_task(task: Task) -> None:
        if not task.session_id:
            return
        await registry.stop_session(task.session_id)
        task.session_id = None
        task.touch()
        storage.save_task(task)

    pipeline_ops: dict[str, Callable[[Task], Awaitable[Any]]] = {
        "advance": pipeline.advance,
        "revert": pipeline.revert,
        "restart": pipeline.restart,
        "stop": _stop_task,
    }

    def _pipeline_route(op: str) -> Callable[[Request], Awaitable[Response]]:
        """Build a handler that runs one pipeline op under the shared lock.

        The lock serializes mutations the same way the TUI's exclusive worker
        group does — two concurrent advances would race on tasks.json.
        """
        async def handler(request: Request) -> Response:
            task = _fresh(request.path_params["task_id"])
            if not task:
                return JSONResponse({"error": "not found"}, status_code=404)
            async with request.app.state.pipe_lock:
                try:
                    await pipeline_ops[op](task)
                except Exception as e:
                    return JSONResponse({"error": str(e)}, status_code=500)
            return JSONResponse({"ok": True})

        return handler

    async def diff(request: Request) -> Response:
        task = _fresh(request.path_params["task_id"])
        if not task:
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            text = await git.diff_from_base(task)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"diff": text})

    async def send_reply(request: Request) -> Response:
        task = _fresh(request.path_params["task_id"])
        if not task or not task.session_id:
            return JSONResponse({"error": "no active session"}, status_code=404)
        body = await request.json()
        text = body.get("text") or ""
        try:
            agent_cfg = config.agent_for_stage(task.status, task)
            backend = registry.backend_for(agent_cfg.name)
            await backend.send_input(task.session_id, text)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"ok": True})

    # --- WebSocket: board state ---

    async def board_ws(ws: WebSocket) -> None:
        if not _origin_ok(ws.headers):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            while True:
                await ws.send_json(await build_state(ws.app.state))
                await asyncio.sleep(1.5)
        except Exception:
            pass

    # --- WebSocket: interactive terminal ---

    async def pty_ws(ws: WebSocket) -> None:
        session_id = ws.path_params["session_id"]
        # Reject cross-origin (CSWSH) and malformed/flag-smuggling session ids.
        if not _origin_ok(ws.headers) or not _VALID_SESSION.fullmatch(session_id):
            await ws.close(code=1008)
            return
        await ws.accept()
        if not await _session_alive(session_id):
            await ws.close(code=1011)
            return
        try:
            cols = int(ws.query_params.get("cols", "120"))
            rows = int(ws.query_params.get("rows", "32"))
        except ValueError:
            cols, rows = 120, 32
        await attach(ws, session_id, cols, rows)

    async def no_dist_page(request: Request) -> Response:
        return HTMLResponse(
            "<pre style='font:14px monospace;color:#d7dde6;background:#0a0d12;"
            "padding:40px'>llm-cc web backend is running.\n\n"
            "Frontend not built yet. Build it with:\n\n"
            "  cd web && npm install && npm run build\n\n"
            "then reload. (API is live at /api/state)</pre>"
        )

    routes = [
        Route("/api/state", state_endpoint),
        Route("/api/task", create_task, methods=["POST"]),
        Route("/api/task/{task_id}", edit_task, methods=["PUT"]),
        Route("/api/task/{task_id}", delete_task, methods=["DELETE"]),
        *[
            Route(f"/api/task/{{task_id}}/{op}", _pipeline_route(op), methods=["POST"])
            for op in pipeline_ops
        ],
        Route("/api/task/{task_id}/diff", diff),
        Route("/api/task/{task_id}/input", send_reply, methods=["POST"]),
        WebSocketRoute("/ws/board", board_ws),
        WebSocketRoute("/ws/pty/{session_id}", pty_ws),
    ]

    dist = _find_dist()
    if dist is not None:
        routes.append(Mount("/", app=StaticFiles(directory=str(dist), html=True)))
    else:
        routes.append(Route("/", no_dist_page))

    async def csrf_guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Block cross-origin browser writes (form-POST CSRF); reads stay open.
        write = request.method in ("POST", "PUT", "DELETE", "PATCH")
        if write and not _origin_ok(request.headers):
            return JSONResponse({"error": "forbidden origin"}, status_code=403)
        return await call_next(request)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        with contextlib.suppress(Exception):
            await registry.reattach_existing(project_path)
        yield
        if registry.session_store:
            registry.session_store.flush_all()
        if is_clean_exit_mode():
            await registry.cleanup_all()
        else:
            await registry.detach_all()

    app = Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=csrf_guard)],
    )
    app.state.project_path = project_path
    app.state.storage = storage
    app.state.config = config
    app.state.registry = registry
    app.state.git = git
    app.state.pipeline = pipeline
    app.state.pipe_lock = asyncio.Lock()
    return app


def run_web(
    project_path: Path,
    host: str = "127.0.0.1",
    port: int = 7420,
    open_browser: bool = True,
) -> None:
    try:
        import uvicorn
    except ImportError:
        raise SystemExit("llm-cc --web needs the web extra. Install with:  uv sync --extra web")
    app = create_app(project_path)
    dist = _find_dist()
    where = "serving built UI" if dist else "API only — frontend not built (open the URL to build)"
    url_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{url_host}:{port}"
    print(f"llm-cc web → {url}  ({where})")
    if open_browser and dist is not None:
        import threading
        import webbrowser

        # Open the default browser shortly after uvicorn is up.
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
