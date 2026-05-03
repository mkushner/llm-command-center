"""Live tmux smoke test for TmuxBackend.

Spawns a real tmux session running `bash`, sends some input, captures output,
verifies the session shows up in `tmux ls`, then kills it.
"""

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from llm_cc.agents import TmuxBackend
from llm_cc.models import AgentConfig, Task


async def main() -> int:
    if not shutil.which("tmux"):
        print("tmux not on PATH — skip", file=sys.stderr)
        return 0

    backend = TmuxBackend()
    task = Task(title="smoke")
    config = AgentConfig(name="smoke", command="bash", args_template="--norc -i")

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        sid = await backend.start(config, task, "", cwd, stage="execute")
        print(f"started session: {sid}")

        # Verify tmux sees it
        rc = subprocess.run(
            ["tmux", "has-session", "-t", sid], capture_output=True
        ).returncode
        assert rc == 0, "tmux has-session should report alive"
        print("has-session: OK")

        # Wait for bash prompt to appear
        await asyncio.sleep(0.5)

        # Send a command via send_input
        await backend.send_input(sid, "echo hello-from-smoke")
        await asyncio.sleep(0.5)

        # Read output from the buffer
        output = await backend.get_output(sid)
        print("output snippet:", output[-200:] if output else "(empty)")
        assert "hello-from-smoke" in output, f"expected hello-from-smoke in output, got: {output!r}"
        print("send_input: OK")

        # Send Ctrl-C via send_raw, then a printable char
        await backend.send_raw(sid, "\x03")
        assert sid in backend._interrupted, "Ctrl-C should mark interrupted"
        await backend.send_raw(sid, "x")
        assert sid not in backend._interrupted, "printable should clear interrupt"
        print("send_raw: OK")

        # Resize
        backend.resize_session(sid, 80, 24)
        await asyncio.sleep(0.1)
        print("resize: OK")

        # Stop
        await backend.stop(sid)
        rc2 = subprocess.run(
            ["tmux", "has-session", "-t", sid], capture_output=True
        ).returncode
        assert rc2 != 0, "tmux session should be gone after stop"
        print("stop: OK")

    print("ALL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
