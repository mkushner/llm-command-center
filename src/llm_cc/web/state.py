"""Board-state snapshot for the web UI.

Mirrors BoardScreen._poll_agent_status: resolves each task's status kind
(error / ready / waiting / running / stale), health, context %, tokens, and a
plain-text preview of the agent's screen for grid cells. Read-only — mutations
go through the pipeline endpoints.
"""

from __future__ import annotations

from typing import Any

from ..health import status_kind
from ..log import logger
from ..models import AgentConfig, TaskStatus

ACTIVE_STAGES = {TaskStatus.PLANNING, TaskStatus.EXECUTE, TaskStatus.REVIEW}
_PREVIEW_LINES = 40
_ATTENTION_KINDS = ("error", "waiting", "ready")


def _model_label(ac: AgentConfig) -> str:
    """Model name with effort appended, e.g. 'claude-opus-4-8 / xhigh'."""
    if ac.model and ac.effort:
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
        # BACKLOG/DONE have no executor. agent_for_stage() can't say "none" — it
        # falls back to the global default — so gate it here, as the TUI does.
        if status in ACTIVE_STAGES:
            try:
                ac = config.agent_for_stage(status)
                entry["agent"] = ac.name
                entry["model"] = _model_label(ac)
            except (KeyError, StopIteration) as e:
                logger.debug("no agent resolved for stage %s: %s", status.value, e)
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
    if task.status in ACTIVE_STAGES:
        try:
            ac = config.agent_for_stage(task.status, task)
            agent = ac.name
            model = _model_label(ac)
        except (KeyError, StopIteration) as e:
            logger.debug("no agent resolved for task %s: %s", task.id, e)

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
        except KeyError as e:
            logger.debug("no backend for agent %r on task %s: %s", agent, task.id, e)

    if sid and backend is not None:
        # One guard for the whole backend block: this feeds a 1.5s websocket
        # push, and a single unhealthy session must not take the board down.
        try:
            h = backend.health(sid)
            if h is not None:
                dto["health"] = h.score
                dto["health_color"] = h.color
                dto["context_remaining"] = h.context_remaining
                dto["context_color"] = h.context_color
                dto["tokens"] = h.context_tokens
                dto["top_error"] = h.top_error

            dto["status_kind"] = status_kind(
                has_session=True,
                in_active_stage=task.status in ACTIVE_STAGES,
                top_error=dto["top_error"],
                complete=backend.is_stage_complete(sid),
                waiting=backend.is_waiting_for_input(sid),
            )

            out = await backend.get_output(sid)
            if out:
                dto["preview"] = "\n".join(out.splitlines()[-_PREVIEW_LINES:])
        except Exception as e:
            logger.warning("could not read backend state for task %s: %s", task.id, e)
    elif not sid:
        dto["status_kind"] = status_kind(
            has_session=False,
            in_active_stage=task.status in ACTIVE_STAGES,
            top_error=None,
            complete=False,
            waiting=False,
        )
        dto["stale"] = dto["status_kind"] == "stale"

    dto["attention"] = dto["status_kind"] in _ATTENTION_KINDS
    return dto
