"""Live smoke test: spawn an agent, simulate llm-cc restart, verify reattach."""

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from llm_cc.agents import AgentRegistry, TmuxBackend, list_llmcc_sessions
from llm_cc.models import AgentConfig, Task, TaskStatus
from llm_cc.storage import Storage


async def main() -> int:
    if not shutil.which("tmux"):
        print("tmux not on PATH — skip", file=sys.stderr)
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        storage = Storage(cwd)
        storage.ensure_dirs()

        # === Phase 1: simulated "first run" — start a session ===
        backend = TmuxBackend()
        task = Task(title="reattach-smoke", status=TaskStatus.EXECUTE)
        config = AgentConfig(name="bash", command="bash", args_template="--norc -i")
        sid = await backend.start(config, task, "", cwd, stage="execute")
        task.session_id = sid
        storage.save_task(task)
        print(f"started: {sid}")

        # Send something so the buffer has known content
        await backend.send_input(sid, "echo phase1-marker")
        await asyncio.sleep(0.5)

        # Detach (do NOT kill) — simulates llm-cc shutting down without --clean-exit
        await backend.detach(sid)
        assert sid not in backend._sessions, "detach should clear local state"
        rc = subprocess.run(
            ["tmux", "has-session", "-t", sid], capture_output=True
        ).returncode
        assert rc == 0, "tmux session must survive detach"
        print("detach: OK (tmux session still alive)")

        # === Phase 2: simulated "second run" — fresh registry, reattach ===
        registry = AgentRegistry(
            agents={"bash": config},
            sessions_dir=cwd / ".llm-cc" / "sessions",
        )

        live = await list_llmcc_sessions()
        assert sid in live, f"list_llmcc_sessions should report {sid}, got {live}"
        print(f"list_llmcc_sessions: {live}")

        reattached, orphans = await registry.reattach_existing(cwd)
        assert reattached == 1, f"expected 1 reattach, got {reattached}"
        assert orphans == [], f"unexpected orphans: {orphans}"
        assert sid in registry._pty._sessions
        assert registry._pty.is_alive(sid)
        print(f"reattach_existing: reattached={reattached}, orphans={orphans}")

        # Send another command via the new backend handle
        await registry._pty.send_input(sid, "echo phase2-marker")
        await asyncio.sleep(0.5)

        output = await registry._pty.get_output(sid)
        assert "phase2-marker" in output, f"phase2-marker missing in:\n{output[-400:]}"
        # phase1-marker may have scrolled off the visible viewport — that's fine.
        print("send_input after reattach: OK")

        # === Cleanup ===
        await registry.cleanup_all()
        rc2 = subprocess.run(
            ["tmux", "has-session", "-t", sid], capture_output=True
        ).returncode
        assert rc2 != 0, "tmux session should be gone after cleanup_all"
        print("cleanup_all: OK")

    print("ALL REATTACH SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
