"""Interactive terminal bridge: `tmux attach` over a WebSocket.

The focused agent terminal in the browser is a real terminal attachment, not a
polled snapshot. On connect we spawn `tmux attach-session -t <id>` inside a PTY
and relay bytes both ways: PTY output → WebSocket (binary), keystrokes/resize →
PTY. Killing the attach client on disconnect detaches WITHOUT killing the tmux
session, so the agent keeps running.

Only one interactive attach per session should be mounted at a time by the
frontend — tmux sizes a window to its smallest client, so a second tiny client
would shrink the agent's view. Grid previews use read-only snapshots instead
(see state.build_state), never an attach.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import pty
import struct
import termios

from starlette.websockets import WebSocket, WebSocketDisconnect


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _preexec() -> None:
    # New session + make the pty slave (fd 0 by now) our controlling terminal,
    # so `tmux attach` gets a real tty and renders the alternate screen.
    os.setsid()
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError:
        pass


async def attach(ws: WebSocket, session_id: str, cols: int = 120, rows: int = 32) -> None:
    """Run the bidirectional bridge until the socket or tmux client closes.

    Caller is responsible for `await ws.accept()` and for verifying the session
    exists before calling this.
    """
    # Hide tmux's own green status line — the web UI supplies its own chrome.
    with contextlib.suppress(Exception):
        off = await asyncio.create_subprocess_exec(
            "tmux", "set-option", "-t", session_id, "status", "off",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await off.wait()

    master, slave = pty.openpty()
    _set_winsize(master, rows, cols)

    env = {**os.environ, "TERM": "xterm-256color"}
    env.pop("CLAUDECODE", None)  # let a nested claude client run cleanly
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "attach-session",
        "-t",
        session_id,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        preexec_fn=_preexec,
        env=env,
    )
    os.close(slave)

    loop = asyncio.get_running_loop()
    out_q: asyncio.Queue[bytes | None] = asyncio.Queue()

    def _on_readable() -> None:
        try:
            data = os.read(master, 65536)
        except OSError:
            data = b""
        if not data:  # EOF — tmux client exited (session gone or detached)
            loop.remove_reader(master)
            out_q.put_nowait(None)
            return
        out_q.put_nowait(data)

    loop.add_reader(master, _on_readable)

    async def _sender() -> None:
        while True:
            data = await out_q.get()
            if data is None:
                break
            try:
                await ws.send_bytes(data)
            except Exception:
                break

    sender = asyncio.create_task(_sender())

    try:
        while True:
            msg = await ws.receive_text()
            try:
                obj = json.loads(msg)
            except (ValueError, TypeError):
                continue
            kind = obj.get("t")
            if kind == "i":  # input keystrokes
                data = obj.get("d", "")
                if data:
                    os.write(master, data.encode("utf-8"))
            elif kind == "r":  # resize
                try:
                    _set_winsize(master, int(obj["r"]), int(obj["c"]))
                except (KeyError, ValueError, OSError):
                    pass
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            loop.remove_reader(master)
        except (ValueError, OSError):
            pass
        sender.cancel()
        # Terminate only the attach client — the tmux session (and agent) survive.
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            os.close(master)
        except OSError:
            pass
