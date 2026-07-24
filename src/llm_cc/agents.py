"""Agent system: backend protocol, tmux/API backends, registry."""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from rich.text import Text

from .health import AgentHealth, HealthScorer, SessionStore
from .models import AgentConfig, AgentMode, Task

# --- Protocol ---


@runtime_checkable
class AgentBackend(Protocol):
    async def start(self, config: AgentConfig, task: Task, prompt: str, cwd: Path, stage: str = "", cli_flags: str = "") -> str: ...
    async def resume(self, session_id: str, prompt: str) -> None: ...
    async def stop(self, session_id: str) -> None: ...
    async def send_input(self, session_id: str, text: str) -> None: ...
    async def send_raw(self, session_id: str, data: str) -> None: ...
    async def get_output(self, session_id: str) -> str: ...
    async def get_output_rich(self, session_id: str) -> Text | None: ...
    async def get_history_rich(self, session_id: str) -> list[Text]: ...
    def is_alive(self, session_id: str) -> bool: ...


# --- Output Buffer ---

# Patterns that indicate the agent is waiting for user input
_INPUT_PATTERNS = (
    # Claude CLI permission prompts
    "enter to confirm",
    "y/n",
    "yes/no",
    "(y)es/(n)o",
    "allow",
    "deny",
    "press enter",
    "esc to cancel",
    "do you want to proceed",
    "tab to amend",
    "i trust this",
    "yes, i trust",
    "interrupt received",
    # Agent idle / finished
    "what would you like",
    "how can i help",
)

# Stage completion markers — agent declares stage done
_COMPLETE_PATTERNS = (
    "planning complete",
    "execute complete",
    "review complete",
)


class OutputBuffer:
    """Terminal output buffer.

    Two write paths:
    - `set_capture(plain, viewport_ansi, history_ansi)` — used by TmuxBackend with
      tmux's own emulator output via capture-pane. tmux did the rendering.
    - `append(data)` — used by ApiBackend for plain-text completions; also the
      path for tests. Data is appended verbatim and the viewport is bounded to
      the configured row height so heuristics over a "screen" still work.
    """

    def __init__(self, log_path: Path | None = None, cols: int = 120, rows: int = 40) -> None:
        self._cols = cols
        self._rows = rows
        self._plain_viewport: str = ""
        self._ansi_viewport: str = ""
        self._ansi_history: str = ""
        self._log_file = None
        self._last_content: str = ""
        self._stable_ticks: int = 0
        self._total_bytes: int = 0
        self._last_output_time: float = 0.0
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = open(log_path, "a")

    def append(self, data: str) -> None:
        """Append plain text. Used by ApiBackend and by tests."""
        self._plain_viewport += data
        # Bound the viewport to last `rows` lines — mimics screen scrolling
        # so substring patterns scroll off correctly under append-only writes.
        lines = self._plain_viewport.split("\n")
        if len(lines) > self._rows:
            self._plain_viewport = "\n".join(lines[-self._rows:])
        self._ansi_viewport = self._plain_viewport
        self._total_bytes += len(data)
        self._last_output_time = time.monotonic()
        if self._log_file:
            self._log_file.write(data)
            self._log_file.flush()

    def set_capture(self, plain: str, viewport_ansi: str, history_ansi: str) -> None:
        """Replace internal state from a tmux capture-pane snapshot."""
        self._plain_viewport = plain
        self._ansi_viewport = viewport_ansi
        self._ansi_history = history_ansi
        self._last_output_time = time.monotonic()

    @property
    def stats(self) -> tuple[int, float, int]:
        """Return (total_bytes, last_output_time, stable_ticks)."""
        return (self._total_bytes, self._last_output_time, self._stable_ticks)

    def mark_idle(self) -> None:
        """Called each poll tick (~0.1s). Tracks if visible content has changed."""
        current = self.display()
        if current == self._last_content:
            self._stable_ticks += 1
        else:
            self._last_content = current
            self._stable_ticks = 0

    def display(self) -> str:
        """Current viewport as clean plain text, trailing whitespace trimmed."""
        text = self._plain_viewport.replace("\r\n", "\n").replace("\r", "\n")
        cleaned = [line.rstrip() for line in text.split("\n")]
        while cleaned and not cleaned[-1]:
            cleaned.pop()
        return "\n".join(cleaned)

    def display_rich(self) -> Text:
        """Viewport as Rich Text with ANSI colors preserved."""
        return Text.from_ansi(self._ansi_viewport.rstrip())

    def history_rich(self) -> list[Text]:
        """Scrollback history above the viewport, line by line."""
        if not self._ansi_history:
            return []
        return [Text.from_ansi(line) for line in self._ansi_history.splitlines()]

    @property
    def total_lines(self) -> int:
        """Total lines: history + viewport."""
        hist = self._ansi_history.count("\n") if self._ansi_history else 0
        view = self._plain_viewport.count("\n") + (1 if self._plain_viewport else 0)
        return hist + view

    def resize(self, cols: int, rows: int) -> None:
        self._cols = cols
        self._rows = rows

    @property
    def appears_stage_complete(self) -> bool:
        """Agent posted a stage completion marker. Requires settled content."""
        if self._stable_ticks < 3:
            return False
        screen_text = self.display().lower()
        return any(p in screen_text for p in _COMPLETE_PATTERNS)

    @property
    def appears_waiting(self) -> bool:
        """Heuristic: agent seems to be waiting for user input.

        Triggers when the visible screen content hasn't changed for ~0.3s
        AND the screen matches known input prompt patterns.
        Does NOT trigger for stage completion — that's a separate state.
        """
        if self._stable_ticks < 3:
            return False
        if self.appears_stage_complete:
            return False
        screen_text = self.display().lower()
        return any(p in screen_text for p in _INPUT_PATTERNS)

    def close(self) -> None:
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None


# --- Tmux Backend ---


_TMUX_NAME_BAD = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_session_name(raw: str) -> str:
    """Tmux disallows `.` and `:` in session names; keep alnum + _ + -."""
    return _TMUX_NAME_BAD.sub("_", raw)


async def _tmux(*args: str) -> tuple[int, bytes, bytes]:
    """Run a tmux command via execFile semantics. Returns (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "tmux", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out, err


def _tmux_sync(*args: str) -> tuple[int, bytes]:
    """Synchronous tmux call (no shell) for is_alive checks and atexit cleanup."""
    import subprocess
    try:
        result = subprocess.run(
            ["tmux", *args],
            capture_output=True,
            check=False,
        )
        return result.returncode, result.stdout
    except FileNotFoundError:
        return 127, b""


# Map raw escape bytes (as written by AgentPanel) to tmux send-keys names
_RAW_TO_TMUX_KEY = {
    "\x03": "C-c",
    "\x04": "C-d",
    "\x1a": "C-z",
    "\r": "Enter",
    "\n": "Enter",
    "\t": "Tab",
    "\x7f": "BSpace",
    "\x08": "BSpace",
    "\x1b": "Escape",
    "\x1b[A": "Up",
    "\x1b[B": "Down",
    "\x1b[C": "Right",
    "\x1b[D": "Left",
    "\x1b[Z": "BTab",
    "\x1b[H": "Home",
    "\x1b[F": "End",
    "\x1b[1~": "Home",
    "\x1b[4~": "End",
    "\x1b[3~": "DC",
    "\x1b[5~": "PPage",
    "\x1b[6~": "NPage",
}


class TmuxBackend:
    """Spawns CLI agents in tmux sessions. tmux owns the PTY layer."""

    def __init__(self, process_manager: _ProcessManager | None = None) -> None:
        self._sessions: dict[str, str] = {}  # session_id -> tmux session name (same value)
        self._buffers: dict[str, OutputBuffer] = {}
        self._log_paths: dict[str, Path] = {}
        self._poll_tasks: dict[str, asyncio.Task[None]] = {}
        self._interrupted: set[str] = set()
        self._pm = process_manager
        self._health_scorers: dict[str, HealthScorer] = {}
        self._session_store: SessionStore | None = None
        self._status_files: dict[str, Path] = {}

    def set_session_store(self, store: SessionStore) -> None:
        self._session_store = store

    async def start(
        self,
        config: AgentConfig,
        task: Task,
        prompt: str,
        cwd: Path,
        stage: str = "",
        cli_flags: str = "",
        terminal_size: tuple[int, int] | None = None,
    ) -> str:
        session_id = _sanitize_session_name(
            f"llmcc_{task.id}_{stage or task.status.value}"
        )

        # Stop old session if same ID exists (prevents orphans)
        if session_id in self._sessions:
            await self.stop(session_id)

        if not config.command:
            raise ValueError(f"Agent '{config.name}' has no command configured for PTY mode")

        # Build command — inject --model if model is set and not in args_template
        model_flag = ""
        if config.model and "{model}" not in config.args_template:
            model_flag = f"--model {config.model}"
        allowed_flag = ""
        if config.allowed_tools and config.command == "claude":
            allowed_flag = "--allowedTools " + shlex.quote(",".join(config.allowed_tools))
        # Codex --full-auto: skip approval prompts inside isolated workspace.
        # Skipped if the user has already specified an approval/sandbox flag.
        full_auto_flag = ""
        if (
            config.auto_full_auto
            and config.command == "codex"
            and "--full-auto" not in cli_flags
            and "--ask-for-approval" not in cli_flags
            and " -a " not in f" {cli_flags} "
            and "--sandbox" not in cli_flags
            and " -s " not in f" {cli_flags} "
            and "--dangerously-bypass-approvals-and-sandbox" not in cli_flags
        ):
            full_auto_flag = "--full-auto"
        cmd_args = config.args_template.format(
            prompt=shlex.quote(prompt),
            session_id=task.id,
            model=config.model or "",
        )
        parts = [config.command, model_flag, allowed_flag, full_auto_flag, cli_flags, cmd_args]
        full_cmd = " ".join(p for p in parts if p).strip()

        # Log path — pipe-pane streams the pane's raw output here
        log_path = cwd / ".llm-cc" / "logs" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate any prior log so the tailer starts fresh at offset 0
        log_path.write_bytes(b"")

        # Terminal dimensions
        if terminal_size:
            cols, rows = terminal_size
        else:
            try:
                ts = os.get_terminal_size()
                cols, rows = ts.columns, ts.lines
            except OSError:
                cols, rows = 120, 40

        # Track status file for statusline data
        self._status_files[session_id] = cwd / ".llm-cc" / "status" / f"{task.id}.json"

        # Compose env — pass LLM_CC_TASK_ID, drop CLAUDECODE so nested claude works
        env_args: list[str] = []
        for k, v in os.environ.items():
            if k == "CLAUDECODE":
                continue
            env_args += ["-e", f"{k}={v}"]
        env_args += ["-e", f"LLM_CC_TASK_ID={task.id}"]

        # Spawn detached tmux session running the agent command
        rc, _, err = await _tmux(
            "new-session",
            "-d",
            "-s", session_id,
            "-x", str(cols),
            "-y", str(rows),
            "-c", str(cwd),
            *env_args,
            full_cmd,
        )
        if rc != 0:
            raise RuntimeError(f"tmux new-session failed: {err.decode(errors='replace').strip()}")

        # Stream pane output to the log file (raw, including ANSI escapes)
        pipe_cmd = f"cat >> {shlex.quote(str(log_path))}"
        await _tmux("pipe-pane", "-t", session_id, "-o", pipe_cmd)

        self._sessions[session_id] = session_id
        # OutputBuffer log_path is None — pipe-pane is the sole writer.
        self._buffers[session_id] = OutputBuffer(log_path=None, cols=cols, rows=rows)
        self._log_paths[session_id] = log_path
        self._health_scorers[session_id] = HealthScorer()

        # Create session context for persistence
        if self._session_store:
            self._session_store.get_or_create(
                session_id, task.id, stage or task.status.value, config.name,
            )

        # Register with process manager only in --clean-exit mode; otherwise
        # the session is allowed to outlive llm-cc.
        if self._pm and _clean_exit_mode:
            self._pm.register(session_id, session_id)

        # Tail the pipe-pane log into OutputBuffer
        self._poll_tasks[session_id] = asyncio.create_task(
            self._poll_output(session_id, log_path)
        )

        return session_id

    async def resume(self, session_id: str, prompt: str) -> None:
        if session_id in self._sessions:
            await self.send_input(session_id, prompt)

    async def reattach(
        self,
        session_id: str,
        task: Task,
        cwd: Path,
        stage: str = "",
        agent_name: str = "",
    ) -> bool:
        """Re-register an existing tmux session that survived an llm-cc restart.

        Returns True if the tmux session is alive and was reattached, False if
        it's gone (caller should clear `task.session_id`).
        """
        if session_id in self._sessions:
            return True
        rc, _ = _tmux_sync("has-session", "-t", session_id)
        if rc != 0:
            return False

        log_path = cwd / ".llm-cc" / "logs" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if not log_path.exists():
            log_path.write_bytes(b"")
        # Re-arm pipe-pane in case the previous llm-cc process owned it
        pipe_cmd = f"cat >> {shlex.quote(str(log_path))}"
        await _tmux("pipe-pane", "-t", session_id, "-o", pipe_cmd)

        try:
            ts = os.get_terminal_size()
            cols, rows = ts.columns, ts.lines
        except OSError:
            cols, rows = 120, 40

        self._sessions[session_id] = session_id
        self._buffers[session_id] = OutputBuffer(log_path=None, cols=cols, rows=rows)
        self._log_paths[session_id] = log_path
        self._health_scorers[session_id] = HealthScorer()
        self._status_files[session_id] = cwd / ".llm-cc" / "status" / f"{task.id}.json"

        if self._session_store:
            self._session_store.get_or_create(
                session_id, task.id, stage or task.status.value, agent_name,
            )

        if self._pm and _clean_exit_mode:
            self._pm.register(session_id, session_id)

        self._poll_tasks[session_id] = asyncio.create_task(
            self._poll_output(session_id, log_path)
        )
        return True

    async def detach(self, session_id: str) -> None:
        """Tear down local state without killing the tmux session.

        Used at app shutdown so sessions persist for the next launch.
        """
        poll = self._poll_tasks.pop(session_id, None)
        if poll:
            poll.cancel()
            try:
                await poll
            except asyncio.CancelledError:
                pass

        self._sessions.pop(session_id, None)
        if self._pm:
            self._pm.unregister(session_id)

        # Status file stays on disk — the underlying agent process is still
        # writing to it.
        self._status_files.pop(session_id, None)
        self._health_scorers.pop(session_id, None)
        if self._session_store:
            self._session_store.flush_force(session_id)
            self._session_store.remove(session_id)

        self._interrupted.discard(session_id)
        self._log_paths.pop(session_id, None)
        buf = self._buffers.pop(session_id, None)
        if buf:
            buf.close()

    async def stop(self, session_id: str) -> None:
        # Cancel poll task
        poll = self._poll_tasks.pop(session_id, None)
        if poll:
            poll.cancel()
            try:
                await poll
            except asyncio.CancelledError:
                pass

        # Kill tmux session (idempotent — tmux returns non-zero if already gone)
        name = self._sessions.pop(session_id, None)
        if name:
            await _tmux("kill-session", "-t", name)

        if self._pm:
            self._pm.unregister(session_id)

        status_file = self._status_files.pop(session_id, None)
        if status_file and status_file.exists():
            try:
                status_file.unlink()
            except Exception:
                pass

        self._health_scorers.pop(session_id, None)
        if self._session_store:
            self._session_store.flush_force(session_id)
            self._session_store.remove(session_id)

        self._interrupted.discard(session_id)
        self._log_paths.pop(session_id, None)
        buf = self._buffers.pop(session_id, None)
        if buf:
            buf.close()

    async def send_input(self, session_id: str, text: str) -> None:
        if session_id not in self._sessions or not self.is_alive(session_id):
            return
        if text:
            # -l sends the literal bytes (no key-name interpretation)
            await _tmux("send-keys", "-t", session_id, "-l", text)
        await _tmux("send-keys", "-t", session_id, "Enter")
        self._interrupted.discard(session_id)

    async def send_raw(self, session_id: str, data: str) -> None:
        """Translate raw escape bytes from AgentPanel to tmux send-keys."""
        if session_id not in self._sessions or not self.is_alive(session_id):
            return

        key_name = _RAW_TO_TMUX_KEY.get(data)
        if key_name:
            await _tmux("send-keys", "-t", session_id, key_name)
        elif data.isprintable():
            # Single char or pasted text — send literally
            await _tmux("send-keys", "-t", session_id, "-l", data)
        else:
            # Unknown escape sequence — send each byte by hex
            hex_args = [f"{b:02x}" for b in data.encode("utf-8")]
            if hex_args:
                await _tmux("send-keys", "-t", session_id, "-H", *hex_args)

        if data == "\x03":
            self._interrupted.add(session_id)
        else:
            self._interrupted.discard(session_id)

    async def get_output(self, session_id: str) -> str:
        buf = self._buffers.get(session_id)
        return buf.display() if buf else ""

    async def get_output_rich(self, session_id: str) -> Text | None:
        buf = self._buffers.get(session_id)
        return buf.display_rich() if buf else None

    async def get_history_rich(self, session_id: str) -> list[Text]:
        buf = self._buffers.get(session_id)
        return buf.history_rich() if buf else []

    def resize_session(self, session_id: str, cols: int, rows: int) -> None:
        """Resize tmux window and the virtual terminal buffer."""
        buf = self._buffers.get(session_id)
        if buf:
            buf.resize(cols, rows)
        if session_id in self._sessions:
            asyncio.create_task(
                _tmux("resize-window", "-t", session_id, "-x", str(cols), "-y", str(rows))
            )

    def is_alive(self, session_id: str) -> bool:
        if session_id not in self._sessions:
            return False
        rc, _ = _tmux_sync("has-session", "-t", session_id)
        return rc == 0

    def is_stage_complete(self, session_id: str) -> bool:
        buf = self._buffers.get(session_id)
        return buf.appears_stage_complete if buf else False

    def is_waiting_for_input(self, session_id: str) -> bool:
        if session_id in self._interrupted:
            return True
        buf = self._buffers.get(session_id)
        return buf.appears_waiting if buf else False

    def health(self, session_id: str) -> AgentHealth | None:
        scorer = self._health_scorers.get(session_id)
        buf = self._buffers.get(session_id)
        if not scorer or not buf:
            return None
        alive = self.is_alive(session_id)
        _, _, stable_ticks = buf.stats
        screen_text = buf.display()
        status_data = self._read_status_file(session_id)
        h = scorer.compute(alive, stable_ticks, screen_text, status_data)

        if self._session_store:
            ctx = self._session_store.get(session_id)
            if ctx:
                ctx.add_event("health", {
                    "score": h.score,
                    "context_remaining": h.context_remaining,
                    "context_warning": self.context_monitor_warning(scorer),
                })
                self._session_store.flush(session_id)
        return h

    def status_data(self, session_id: str) -> dict | None:
        return self._read_status_file(session_id)

    def _read_status_file(self, session_id: str) -> dict | None:
        status_file = self._status_files.get(session_id)
        if not status_file or not status_file.exists():
            return None
        try:
            return json.loads(status_file.read_text())
        except Exception:
            return None

    @staticmethod
    def context_monitor_warning(scorer: HealthScorer) -> str | None:
        return scorer.context_monitor.warning_level

    def active_session_ids(self) -> list[str]:
        return list(self._sessions.keys())

    async def _poll_output(self, session_id: str, log_path: Path) -> None:
        """Periodic capture-pane snapshots → OutputBuffer; tail log for activity."""
        buf = self._buffers.get(session_id)
        if not buf:
            return

        # Wait briefly for pipe-pane to attach so log_path exists
        for _ in range(20):
            if log_path.exists():
                break
            await asyncio.sleep(0.05)

        try:
            log_fd = log_path.open("rb")
        except OSError:
            log_fd = None

        try:
            while True:
                # Activity tracking: read whatever pipe-pane has appended.
                # We don't feed it into OutputBuffer (capture-pane is the source
                # of truth for rendering); we just count bytes for health scoring
                # and record an excerpt for the session log.
                if log_fd is not None:
                    try:
                        data = log_fd.read()
                    except Exception:
                        data = b""
                    if data:
                        scorer = self._health_scorers.get(session_id)
                        if scorer:
                            scorer.record_output(len(data))
                        if self._session_store:
                            ctx = self._session_store.get(session_id)
                            if ctx:
                                excerpt = data[-200:].decode("utf-8", errors="replace")
                                ctx.add_event("output", {"text": excerpt})

                alive = self.is_alive(session_id)
                if alive:
                    plain, viewport_ansi, history_ansi = await self._capture(session_id)
                    if plain is not None:
                        buf.set_capture(plain, viewport_ansi or "", history_ansi or "")

                buf.mark_idle()

                if not alive:
                    # Final drain of activity log, then stop
                    if log_fd is not None:
                        try:
                            tail = log_fd.read()
                        except Exception:
                            tail = b""
                        if tail:
                            scorer = self._health_scorers.get(session_id)
                            if scorer:
                                scorer.record_output(len(tail))
                    break

                await asyncio.sleep(0.2)
        finally:
            if log_fd is not None:
                try:
                    log_fd.close()
                except Exception:
                    pass

    async def _capture(self, session_id: str) -> tuple[str | None, str | None, str | None]:
        """Return (plain_viewport, ansi_viewport, ansi_history) via capture-pane."""
        rc1, plain_b, _ = await _tmux("capture-pane", "-t", session_id, "-p")
        if rc1 != 0:
            return None, None, None
        rc2, ansi_b, _ = await _tmux("capture-pane", "-t", session_id, "-e", "-p")
        rc3, hist_b, _ = await _tmux(
            "capture-pane", "-t", session_id, "-e", "-p", "-S", "-5000", "-E", "-1",
        )
        plain = plain_b.decode("utf-8", errors="replace")
        viewport_ansi = ansi_b.decode("utf-8", errors="replace") if rc2 == 0 else plain
        history_ansi = hist_b.decode("utf-8", errors="replace") if rc3 == 0 else ""
        return plain, viewport_ansi, history_ansi



# --- API Backend ---


class ApiBackend:
    """Calls AI SDKs directly for automated stages. Lazy imports."""

    def __init__(self) -> None:
        self._results: dict[str, str] = {}
        self._running: dict[str, asyncio.Task[None]] = {}
        self._buffers: dict[str, OutputBuffer] = {}

    async def start(self, config: AgentConfig, task: Task, prompt: str, cwd: Path, stage: str = "", cli_flags: str = "") -> str:
        session_id = f"api_{task.id}_{stage or task.status.value}"

        # Stop old session if same ID exists
        if session_id in self._running:
            await self.stop(session_id)

        log_path = cwd / ".llm-cc" / "logs" / f"{session_id}.log"
        self._buffers[session_id] = OutputBuffer(log_path=log_path)
        self._results[session_id] = ""

        async def _run() -> None:
            result = ""
            try:
                if config.api_provider == "anthropic":
                    result = await self._run_anthropic(config, prompt)
                elif config.api_provider == "openai":
                    result = await self._run_openai(config, prompt)
                else:
                    result = f"Unknown API provider: {config.api_provider}"
            except Exception as e:
                result = f"API error: {e}"

            self._results[session_id] = result
            buf = self._buffers.get(session_id)
            if buf:
                buf.append(result)

        self._running[session_id] = asyncio.create_task(_run())
        return session_id

    async def _run_anthropic(self, config: AgentConfig, prompt: str) -> str:
        import anthropic

        client = anthropic.AsyncAnthropic()
        msg = await client.messages.create(
            model=config.api_model or config.model or "claude-opus-5",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        # Handle different content block types
        for block in msg.content:
            if hasattr(block, "text"):
                return block.text
        return ""

    async def _run_openai(self, config: AgentConfig, prompt: str) -> str:
        import openai

        client = openai.AsyncOpenAI()
        resp = await client.chat.completions.create(
            model=config.api_model or config.model or "gpt-4o",
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.choices:
            return resp.choices[0].message.content or ""
        return ""

    async def resume(self, session_id: str, prompt: str) -> None:
        # API sessions can't resume — no-op
        pass

    async def stop(self, session_id: str) -> None:
        task = self._running.pop(session_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Clean up buffer and results
        buf = self._buffers.pop(session_id, None)
        if buf:
            buf.close()
        self._results.pop(session_id, None)

    async def send_input(self, session_id: str, text: str) -> None:
        # API sessions don't accept input — no-op
        pass

    async def send_raw(self, session_id: str, data: str) -> None:
        # API sessions don't accept raw input — no-op
        pass

    async def get_output(self, session_id: str) -> str:
        buf = self._buffers.get(session_id)
        if buf:
            return buf.display()
        return self._results.get(session_id, "Running...")

    def is_alive(self, session_id: str) -> bool:
        task = self._running.get(session_id)
        return task is not None and not task.done()

    def health(self, session_id: str) -> AgentHealth | None:
        """Static health for API backend: 75 if alive, 0 if dead."""
        alive = self.is_alive(session_id)
        score = 75 if alive else 0
        return AgentHealth(
            score=score,
            liveness=25 if alive else 0,
            activity=25 if alive else 0,
            stability=25,
            responsiveness=0 if alive else 0,
        )

    def active_session_ids(self) -> list[str]:
        """List all active session IDs."""
        return list(self._running.keys())


# --- Process Manager (crash cleanup) ---


class _ProcessManager:
    """Track tmux session names for clean shutdown on crash/signal."""

    def __init__(self) -> None:
        self._sessions: dict[str, str] = {}  # session_id -> tmux session name

    def register(self, session_id: str, tmux_name: str) -> None:
        self._sessions[session_id] = tmux_name

    def unregister(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def cleanup_all(self) -> None:
        """Kill all tracked tmux sessions. Safe to call from atexit."""
        for _sid, name in list(self._sessions.items()):
            _tmux_sync("kill-session", "-t", name)
        self._sessions.clear()


# Singleton — shared between TmuxBackend and atexit handler.
# Sessions are registered with this manager only when --clean-exit is on,
# so by default tmux sessions outlive an llm-cc shutdown and can be
# reattached on the next startup.
_process_manager = _ProcessManager()
atexit.register(_process_manager.cleanup_all)

_clean_exit_mode: bool = False


def set_clean_exit_mode(enabled: bool) -> None:
    """When enabled, tmux sessions are killed at llm-cc shutdown.

    Off by default so sessions persist across crashes / quits and can be
    reattached on the next startup.
    """
    global _clean_exit_mode
    _clean_exit_mode = enabled


def is_clean_exit_mode() -> bool:
    return _clean_exit_mode


async def list_llmcc_sessions() -> list[str]:
    """Return tmux session names matching the llm-cc prefix."""
    rc, out, _ = await _tmux("list-sessions", "-F", "#{session_name}")
    if rc != 0:
        return []
    names = out.decode("utf-8", errors="replace").splitlines()
    return [n for n in names if n.startswith("llmcc_")]


# --- Registry ---


class AgentRegistry:
    """Central agent manager. Creates backends on demand, tracks sessions."""

    def __init__(self, agents: dict[str, AgentConfig], sessions_dir: Path | None = None) -> None:
        self._configs = agents
        self._pty = TmuxBackend(process_manager=_process_manager)
        self._api = ApiBackend()
        self._session_store: SessionStore | None = None
        if sessions_dir:
            self._session_store = SessionStore(sessions_dir)
            self._pty.set_session_store(self._session_store)

    @property
    def session_store(self) -> SessionStore | None:
        return self._session_store

    def backend_for(
        self, agent_name: str, mode_override: AgentMode | None = None
    ) -> AgentBackend:
        """Get the appropriate backend for an agent."""
        config = self._configs.get(agent_name)
        if not config:
            raise KeyError(f"Unknown agent: {agent_name}")
        mode = mode_override or config.mode
        if mode == AgentMode.PTY:
            return self._pty
        return self._api

    def config_for(self, agent_name: str) -> AgentConfig:
        config = self._configs.get(agent_name)
        if not config:
            raise KeyError(f"Unknown agent: {agent_name}")
        return config

    def is_available(self, agent_name: str) -> bool:
        """Check if an agent's CLI tool is installed."""
        config = self._configs.get(agent_name)
        if not config:
            return False
        if config.mode == AgentMode.API:
            return True  # API agents always "available"
        cmd = config.detect_command or config.command
        if not cmd:
            return False
        return shutil.which(cmd) is not None

    def available_agents(self) -> list[str]:
        return [name for name in self._configs if self.is_available(name)]

    async def stop_session(self, session_id: str) -> None:
        """Stop a specific session, routing to the correct backend."""
        if session_id in self._pty._sessions:
            await self._pty.stop(session_id)
        elif session_id in self._api._running:
            await self._api.stop(session_id)

    async def cleanup_all(self) -> None:
        """Terminate all sessions. Called on app exit when --clean-exit is set."""
        # Flush session store before stopping
        if self._session_store:
            self._session_store.flush_all()
        # Gather all session IDs, stop concurrently
        pty_sids = self._pty.active_session_ids()
        api_sids = self._api.active_session_ids()
        tasks = [self._pty.stop(sid) for sid in pty_sids]
        tasks += [self._api.stop(sid) for sid in api_sids]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def detach_all(self) -> None:
        """Release local handles without killing tmux sessions.

        Default app-shutdown path so agents survive an llm-cc restart.
        ApiBackend has no out-of-process equivalent, so its sessions are
        stopped (cancelled) regardless.
        """
        if self._session_store:
            self._session_store.flush_all()
        pty_sids = self._pty.active_session_ids()
        api_sids = self._api.active_session_ids()
        coros = [self._pty.detach(sid) for sid in pty_sids]
        coros += [self._api.stop(sid) for sid in api_sids]
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)

    async def reattach_existing(self, project_path: Path) -> tuple[int, list[str]]:
        """Scan tmux for live llmcc-* sessions and re-register them.

        Returns (reattached_count, orphan_session_names). Orphans are tmux
        sessions whose name doesn't match any task's session_id (left alone).
        """
        from llm_cc.storage import Storage

        storage = Storage(project_path)
        store = storage.load_tasks()
        tasks_by_session = {
            t.session_id: t for t in store.tasks if t.session_id
        }

        live_names = await list_llmcc_sessions()
        live_set = set(live_names)

        reattached = 0
        orphans: list[str] = []
        cleared = False

        for name in live_names:
            task = tasks_by_session.get(name)
            if task is None:
                orphans.append(name)
                continue
            ok = await self._pty.reattach(name, task, project_path, stage=task.status.value)
            if ok:
                reattached += 1

        # Clear session_id on tasks whose tmux session is gone
        for task in store.tasks:
            if task.session_id and task.session_id not in live_set:
                task.session_id = None
                storage.save_task(task)
                cleared = True

        if cleared:
            # save_task already persisted; no-op marker for callers that care
            pass

        return reattached, orphans
