"""Tests for M3: session persistence + reattach on startup."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from llm_cc import agents as agents_mod
from llm_cc.agents import (
    AgentRegistry,
    TmuxBackend,
    is_clean_exit_mode,
    set_clean_exit_mode,
)
from llm_cc.models import AgentConfig, Task, TaskStatus
from llm_cc.storage import Storage


@pytest.fixture
def patched_tmux():
    """Patch tmux invocations and provide a controllable session-list."""
    live_sessions: set[str] = set()

    async def fake_async(*args, **kwargs):
        if not args:
            return 0, b"", b""
        sub = args[0]
        if sub == "new-session":
            # Last arg is the command; session name follows -s
            try:
                idx = args.index("-s")
                live_sessions.add(args[idx + 1])
            except ValueError:
                pass
            return 0, b"", b""
        if sub == "kill-session":
            try:
                idx = args.index("-t")
                live_sessions.discard(args[idx + 1])
            except ValueError:
                pass
            return 0, b"", b""
        if sub == "list-sessions":
            out = "\n".join(sorted(live_sessions)).encode()
            return 0, out, b""
        return 0, b"", b""

    def fake_sync(*args, **kwargs):
        if not args:
            return 0, b""
        sub = args[0]
        if sub == "has-session":
            try:
                idx = args.index("-t")
                return (0 if args[idx + 1] in live_sessions else 1, b"")
            except ValueError:
                return 1, b""
        if sub == "kill-session":
            try:
                idx = args.index("-t")
                live_sessions.discard(args[idx + 1])
            except ValueError:
                pass
            return 0, b""
        return 0, b""

    with patch("llm_cc.agents._tmux", side_effect=fake_async), \
         patch("llm_cc.agents._tmux_sync", side_effect=fake_sync):
        yield live_sessions


@pytest.fixture(autouse=True)
def reset_clean_exit():
    """Reset module flag between tests."""
    set_clean_exit_mode(False)
    yield
    set_clean_exit_mode(False)


# --- clean-exit mode flag ---


def test_clean_exit_default_false():
    assert is_clean_exit_mode() is False


def test_set_clean_exit_mode():
    set_clean_exit_mode(True)
    assert is_clean_exit_mode() is True
    set_clean_exit_mode(False)
    assert is_clean_exit_mode() is False


async def test_start_skips_pm_register_by_default(tmp_path, patched_tmux):
    """Without --clean-exit, sessions are not registered for atexit kill."""
    backend = TmuxBackend(process_manager=agents_mod._ProcessManager())
    task = Task(title="t")
    config = AgentConfig(name="claude", command="echo", args_template="{prompt}")

    sid = await backend.start(config, task, "hi", tmp_path, stage="execute")
    assert sid not in backend._pm._sessions  # not registered
    await backend.stop(sid)


async def test_start_registers_pm_when_clean_exit(tmp_path, patched_tmux):
    """With --clean-exit, sessions are registered for atexit kill."""
    set_clean_exit_mode(True)
    backend = TmuxBackend(process_manager=agents_mod._ProcessManager())
    task = Task(title="t")
    config = AgentConfig(name="claude", command="echo", args_template="{prompt}")

    sid = await backend.start(config, task, "hi", tmp_path, stage="execute")
    assert sid in backend._pm._sessions
    await backend.stop(sid)


# --- reattach ---


async def test_reattach_live_session(tmp_path, patched_tmux):
    """reattach() registers a tmux session that's already alive."""
    backend = TmuxBackend()
    task = Task(title="resume me", status=TaskStatus.EXECUTE)
    sid = f"llmcc_{task.id}_execute"
    patched_tmux.add(sid)  # pretend tmux already has it

    ok = await backend.reattach(sid, task, tmp_path, stage="execute")
    assert ok is True
    assert sid in backend._sessions
    assert backend.is_alive(sid)
    assert sid in backend._buffers
    assert sid in backend._poll_tasks
    # Status file path is computed from cwd + task.id
    assert backend._status_files[sid] == tmp_path / ".llm-cc" / "status" / f"{task.id}.json"

    await backend.detach(sid)


async def test_reattach_dead_session(tmp_path, patched_tmux):
    """reattach() returns False when tmux session is gone."""
    backend = TmuxBackend()
    task = Task(title="ghost")
    sid = f"llmcc_{task.id}_execute"
    # Don't add to live_sessions — has-session will return non-zero

    ok = await backend.reattach(sid, task, tmp_path)
    assert ok is False
    assert sid not in backend._sessions


async def test_reattach_idempotent(tmp_path, patched_tmux):
    """Calling reattach twice on the same id is a no-op."""
    backend = TmuxBackend()
    task = Task(title="dup", status=TaskStatus.PLANNING)
    sid = f"llmcc_{task.id}_planning"
    patched_tmux.add(sid)

    assert await backend.reattach(sid, task, tmp_path) is True
    poll1 = backend._poll_tasks[sid]
    assert await backend.reattach(sid, task, tmp_path) is True
    # Same poll task — not replaced
    assert backend._poll_tasks[sid] is poll1

    await backend.detach(sid)


# --- detach ---


async def test_detach_does_not_kill_tmux(tmp_path, patched_tmux):
    """detach() leaves the tmux session running."""
    backend = TmuxBackend()
    task = Task(title="x")
    sid = f"llmcc_{task.id}_execute"
    patched_tmux.add(sid)

    await backend.reattach(sid, task, tmp_path)
    assert sid in backend._sessions

    await backend.detach(sid)
    assert sid not in backend._sessions
    assert sid in patched_tmux  # tmux session still alive


async def test_detach_preserves_status_file(tmp_path, patched_tmux):
    """detach() does NOT remove the status file (the agent process still owns it)."""
    backend = TmuxBackend()
    task = Task(title="x")
    sid = f"llmcc_{task.id}_execute"
    patched_tmux.add(sid)
    await backend.reattach(sid, task, tmp_path)

    status_file = backend._status_files[sid]
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text('{"alive": true}')

    await backend.detach(sid)
    assert status_file.exists()


async def test_stop_still_removes_status_file(tmp_path, patched_tmux):
    """Regression: stop() (--clean-exit path) keeps the original behavior."""
    backend = TmuxBackend()
    task = Task(title="x")
    config = AgentConfig(name="claude", command="echo", args_template="{prompt}")
    sid = await backend.start(config, task, "hi", tmp_path, stage="execute")

    status_file = backend._status_files[sid]
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text('{"alive": true}')
    assert status_file.exists()

    await backend.stop(sid)
    assert not status_file.exists()


# --- AgentRegistry.reattach_existing ---


async def test_reattach_existing_matches_tasks(tmp_path, patched_tmux):
    """Live tmux sessions with matching tasks are reattached; orphans listed."""
    storage = Storage(tmp_path)
    storage.ensure_dirs()

    task = Task(title="resume", status=TaskStatus.EXECUTE)
    sid = f"llmcc_{task.id}_execute"
    task.session_id = sid
    storage.save_task(task)

    patched_tmux.add(sid)
    patched_tmux.add("llmcc_orphan_session")

    registry = AgentRegistry(
        agents={"claude": AgentConfig(name="claude", command="echo", args_template="{prompt}")},
    )
    reattached, orphans = await registry.reattach_existing(tmp_path)

    assert reattached == 1
    assert orphans == ["llmcc_orphan_session"]
    assert sid in registry._pty._sessions

    await registry._pty.detach(sid)


async def test_reattach_existing_clears_dead_session_id(tmp_path, patched_tmux):
    """Tasks pointing at dead tmux sessions get session_id cleared."""
    storage = Storage(tmp_path)
    storage.ensure_dirs()

    task = Task(title="ghost", status=TaskStatus.EXECUTE)
    task.session_id = f"llmcc_{task.id}_execute"  # not in live_sessions
    storage.save_task(task)

    registry = AgentRegistry(
        agents={"claude": AgentConfig(name="claude", command="echo", args_template="{prompt}")},
    )
    reattached, orphans = await registry.reattach_existing(tmp_path)
    assert reattached == 0

    # Task was rewritten with session_id = None
    refreshed = storage.load_tasks().get(task.id)
    assert refreshed is not None
    assert refreshed.session_id is None


# --- detach_all ---


async def test_detach_all_keeps_tmux_alive(tmp_path, patched_tmux):
    """AgentRegistry.detach_all leaves tmux sessions alive."""
    registry = AgentRegistry(
        agents={"claude": AgentConfig(name="claude", command="echo", args_template="{prompt}")},
    )
    task = Task(title="x")
    config = registry.config_for("claude")
    sid = await registry._pty.start(config, task, "hi", tmp_path, stage="execute")
    assert sid in patched_tmux

    await registry.detach_all()
    assert sid in patched_tmux  # still there
    assert sid not in registry._pty._sessions


async def test_cleanup_all_kills_tmux(tmp_path, patched_tmux):
    """AgentRegistry.cleanup_all kills tmux sessions (--clean-exit path)."""
    registry = AgentRegistry(
        agents={"claude": AgentConfig(name="claude", command="echo", args_template="{prompt}")},
    )
    task = Task(title="x")
    config = registry.config_for("claude")
    sid = await registry._pty.start(config, task, "hi", tmp_path, stage="execute")
    assert sid in patched_tmux

    await registry.cleanup_all()
    assert sid not in patched_tmux  # killed
