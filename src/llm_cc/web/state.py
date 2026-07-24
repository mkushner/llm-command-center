"""Board-state snapshot for the web UI.

Mirrors BoardScreen._poll_agent_status: resolves each task's status kind
(error / ready / waiting / running / stale), health, context %, tokens, and a
plain-text preview of the agent's screen for grid cells. Read-only — mutations
go through the pipeline endpoints.
"""

from __future__ import annotations

from typing import Any

from ..models import TaskStatus

ACTIVE_STAGES = {TaskStatus.PLANNING, TaskStatus.EXECUTE, TaskStatus.REVIEW}
_PREVIEW_LINES = 40


def _model_label(ac: Any) -> str:
    """Model name with effort appended, e.g. 'claude-opus-4-8 / xhigh'."""
    if ac.model and getattr(ac, "effort", None):
        return f"{ac.model} / {ac.effort}"
    return ac.model or ""


async def build_state(app_state: Any) -> dict[str, Any]:
    storage = app_state.storage
    registry = app_state.registry
    config = app_state.config

    store = storage.load_tasks()
    tasks: list[dict[str, Any]] = []
    agg = {"running": 0, "waiting": 0, "ready": 0, "error": 0, "exec_active": 0}

    for task in store.tasks:
        dto = await _task_dto(task, registry, config)
        tasks.append(dto)
        if dto["session_id"]:
            if task.status == TaskStatus.EXECUTE:
                agg["exec_active"] += 1
            kind = dto["status_kind"]
            if kind == "error":
                agg["error"] += 1
            elif kind == "ready":
                agg["ready"] += 1
            elif kind == "waiting":
                agg["waiting"] += 1
            elif kind == "running":
                agg["running"] += 1

    stages: list[dict[str, Any]] = []
    for status in config.active_stages():
        entry = {"key": status.value, "label": config.label_for(status), "agent": "", "model": ""}
        try:
            ac = config.agent_for_stage(status)
            entry["agent"] = ac.name
            entry["model"] = _model_label(ac)
        except Exception:
            pass
        stages.append(entry)

    return {
        "type": "state",
        "project": config.project.name or "",
        "base_branch": config.project.git.base_branch or "",
        "stages": stages,
        "tasks": tasks,
        "aggregate": agg,
    }


async def _task_dto(task: Any, registry: Any, config: Any) -> dict[str, Any]:
    sid = task.session_id
    agent = model = ""
    try:
        ac = config.agent_for_stage(task.status, task)
        agent = ac.name
        model = _model_label(ac)
    except Exception:
        pass

    dto: dict[str, Any] = {
        "id": task.id,
        "title": task.title,
        "description": task.description or "",
        "status": task.status.value,
        "session_id": sid,
        "agent": agent,
        "model": model,
        "branch": task.branch_name or task.checkout_branch or "",
        "status_kind": None,
        "health": None,
        "health_color": None,
        "context_remaining": None,
        "context_color": None,
        "tokens": None,
        "top_error": None,
        "stale": False,
        "attention": False,
        "preview": None,
    }

    backend = None
    if sid and agent:
        try:
            backend = registry.backend_for(agent)
        except Exception:
            backend = None

    if sid and backend is not None:
        try:
            if hasattr(backend, "health"):
                h = backend.health(sid)
                if h is not None:
                    dto["health"] = h.score
                    dto["health_color"] = h.color
                    dto["context_remaining"] = h.context_remaining
                    dto["context_color"] = h.context_color
                    tok = (
                        (h.input_tokens or 0)
                        + (h.cache_creation_tokens or 0)
                        + (h.cache_read_tokens or 0)
                    )
                    dto["tokens"] = tok or None
                    if h.errors:
                        worst = max(h.errors, key=lambda e: e.severity)
                        dto["top_error"] = worst.pattern_name
        except Exception:
            pass

        complete = waiting = False
        try:
            if hasattr(backend, "is_stage_complete"):
                complete = backend.is_stage_complete(sid)
            if hasattr(backend, "is_waiting_for_input"):
                waiting = backend.is_waiting_for_input(sid)
        except Exception:
            pass

        if dto["top_error"]:
            dto["status_kind"] = "error"
        elif complete:
            dto["status_kind"] = "ready"
        elif waiting:
            dto["status_kind"] = "waiting"
        else:
            dto["status_kind"] = "running"

        try:
            if hasattr(backend, "get_output"):
                out = await backend.get_output(sid)
                if out:
                    dto["preview"] = "\n".join(out.splitlines()[-_PREVIEW_LINES:])
        except Exception:
            pass
    elif not sid and task.status in ACTIVE_STAGES:
        dto["stale"] = True
        dto["status_kind"] = "stale"

    dto["attention"] = dto["status_kind"] in ("error", "waiting", "ready")
    return dto
