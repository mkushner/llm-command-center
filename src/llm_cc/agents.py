"""Agent system: backend protocol, PTY/API backends, registry."""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import shlex
import shutil
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

import pyte
from rich.style import Style
from rich.text import Text

from .health import AgentHealth, HealthScorer, SessionStore
from .models import AgentConfig, AgentMode, Task, TaskStatus


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
    """Terminal emulator buffer. Uses pyte to properly decode PTY output."""

    def __init__(self, log_path: Path | None = None, cols: int = 120, rows: int = 40) -> None:
        self._screen = pyte.HistoryScreen(cols, rows, history=5000)
        self._stream = pyte.Stream(self._screen)
        self._log_file = None
        self._last_content: str = ""
        self._stable_ticks: int = 0  # how many polls the screen hasn't changed
        self._total_bytes: int = 0
        self._last_output_time: float = 0.0
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = open(log_path, "a")

    def append(self, data: str) -> None:
        self._stream.feed(data)
        self._total_bytes += len(data)
        self._last_output_time = time.monotonic()
        if self._log_file:
            self._log_file.write(data)
            self._log_file.flush()

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
        """Get the current screen content as clean text."""
        lines = self._screen.display
        # Strip trailing whitespace from each line, drop trailing empty lines
        cleaned = [line.rstrip() for line in lines]
        while cleaned and not cleaned[-1]:
            cleaned.pop()
        return "\n".join(cleaned)

    @staticmethod
    def _pyte_color_to_rich(color: str, background: bool = False) -> str:
        """Convert a pyte color value to a Rich style string."""
        if color == "default" or not color:
            return ""
        # Named colors — Rich supports them directly
        named = {
            "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
            "bright_black", "bright_red", "bright_green", "bright_yellow",
            "bright_blue", "bright_magenta", "bright_cyan", "bright_white",
        }
        # pyte uses "brown" for yellow sometimes
        if color == "brown":
            color = "yellow"
        if color in named:
            return f"on {color}" if background else color
        # Hex color string (256-color or truecolor, e.g. "ff8700")
        if len(color) == 6:
            try:
                int(color, 16)
                return f"on #{color}" if background else f"#{color}"
            except ValueError:
                pass
        return ""

    def _row_to_rich(self, row: dict) -> Text:
        """Convert a pyte screen row (dict of col -> Char) to a Rich Text."""
        if not row:
            return Text("")
        max_col = max(row.keys()) if row else 0
        result = Text()
        span_chars: list[str] = []
        span_style: Style | None = None

        for col in range(max_col + 1):
            char = row.get(col)
            if char is None:
                ch = " "
                fg_str = ""
                bg_str = ""
                bold = False
                italic = False
            else:
                ch = char.data if char.data else " "
                fg_str = self._pyte_color_to_rich(char.fg)
                bg_str = self._pyte_color_to_rich(char.bg, background=True)
                bold = char.bold
                italic = char.italics

            parts = [s for s in (fg_str, bg_str) if s]
            if bold:
                parts.append("bold")
            if italic:
                parts.append("italic")
            style = Style.parse(" ".join(parts)) if parts else Style.null()

            if style == span_style:
                span_chars.append(ch)
            else:
                if span_chars:
                    result.append("".join(span_chars), span_style)
                span_chars = [ch]
                span_style = style

        if span_chars:
            result.append("".join(span_chars), span_style)

        # Strip trailing whitespace
        result.rstrip()
        return result

    def display_rich(self) -> Text:
        """Get current screen content as a Rich Text with ANSI colors preserved."""
        lines: list[Text] = []
        for row_idx in range(self._screen.lines):
            row = self._screen.buffer[row_idx]
            lines.append(self._row_to_rich(row))
        # Drop trailing empty lines
        while lines and not lines[-1].plain.strip():
            lines.pop()
        result = Text()
        for i, line in enumerate(lines):
            if i > 0:
                result.append("\n")
            result.append_text(line)
        return result

    def history_rich(self) -> list[Text]:
        """Get scrollback history lines as Rich Text objects."""
        result: list[Text] = []
        for row in self._screen.history.top:
            result.append(self._row_to_rich(row))
        return result

    @property
    def total_lines(self) -> int:
        """Total lines: history + active screen lines."""
        return len(self._screen.history.top) + self._screen.lines

    def resize(self, cols: int, rows: int) -> None:
        """Resize the virtual terminal."""
        self._screen.resize(rows, cols)

    @property
    def appears_stage_complete(self) -> bool:
        """Agent posted a stage completion marker (e.g., EXECUTE COMPLETE).

        Same stability check as appears_waiting — content must be settled.
        """
        if self._stable_ticks < 3:
            return False
        screen_text = self.display().lower()
        return any(p in screen_text for p in _COMPLETE_PATTERNS)

    @property
    def appears_waiting(self) -> bool:
        """Heuristic: agent seems to be waiting for user input.

        Triggers when the visible screen content hasn't changed for ~0.3s
        AND the screen matches known input prompt patterns.
        Searches full screen since CLI prompts can render anywhere.
        The board's 2-second poll interval naturally debounces brief flickers.
        Does NOT trigger for stage completion — that's a separate state.
        """
        if self._stable_ticks < 3:
            return False
        if self.appears_stage_complete:
            return False
        screen_text = self.display().lower()
        return any(p in screen_text for p in _INPUT_PATTERNS)

    def close(self) -> None:
        """Close log file if open."""
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None


# --- PTY Backend ---


class PtyBackend:
    """Spawns CLI agents in native pseudo-terminals via pexpect."""

    def __init__(self, process_manager: _ProcessManager | None = None) -> None:
        self._sessions: dict[str, object] = {}  # session_id -> pexpect.spawn
        self._buffers: dict[str, OutputBuffer] = {}
        self._poll_tasks: dict[str, asyncio.Task[None]] = {}
        self._interrupted: set[str] = set()  # sessions with pending interrupt
        self._pm = process_manager
        self._health_scorers: dict[str, HealthScorer] = {}
        self._session_store: SessionStore | None = None
        self._status_files: dict[str, Path] = {}  # session_id -> status file path

    def set_session_store(self, store: SessionStore) -> None:
        self._session_store = store

    async def start(self, config: AgentConfig, task: Task, prompt: str, cwd: Path, stage: str = "", cli_flags: str = "", terminal_size: tuple[int, int] | None = None) -> str:
        import pexpect

        session_id = f"pty_{task.id}_{stage or task.status.value}"

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
        cmd_args = config.args_template.format(
            prompt=shlex.quote(prompt),
            session_id=task.id,
            model=config.model or "",
        )
        parts = [config.command, model_flag, allowed_flag, cli_flags, cmd_args]
        full_cmd = " ".join(p for p in parts if p).strip()

        # Log path
        log_path = cwd / ".llm-cc" / "logs" / f"{session_id}.log"

        # Terminal dimensions — use provided size, real terminal, or defaults
        if terminal_size:
            cols, rows = terminal_size
        else:
            try:
                ts = os.get_terminal_size()
                cols, rows = ts.columns, ts.lines
            except OSError:
                cols, rows = 120, 40

        # Clean env: allow nested Claude sessions from TUI
        spawn_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        spawn_env["LLM_CC_TASK_ID"] = task.id

        # Track status file for statusline data
        self._status_files[session_id] = cwd / ".llm-cc" / "status" / f"{task.id}.json"

        # Spawn in PTY
        child = pexpect.spawn(
            full_cmd,
            cwd=str(cwd),
            encoding="utf-8",
            timeout=None,
            dimensions=(rows, cols),
            env=spawn_env,
        )

        self._sessions[session_id] = child
        self._buffers[session_id] = OutputBuffer(log_path=log_path, cols=cols, rows=rows)
        self._health_scorers[session_id] = HealthScorer()

        # Create session context for persistence
        if self._session_store:
            self._session_store.get_or_create(
                session_id, task.id, stage or task.status.value, config.name,
            )

        # Register with process manager for crash cleanup
        if self._pm:
            self._pm.register(session_id, child)

        # Start polling output in background
        self._poll_tasks[session_id] = asyncio.create_task(
            self._poll_output(session_id, child)
        )

        return session_id

    async def resume(self, session_id: str, prompt: str) -> None:
        child = self._sessions.get(session_id)
        if child and hasattr(child, "sendline"):
            try:
                child.sendline(prompt)
            except Exception:
                pass

    async def stop(self, session_id: str) -> None:
        # Cancel poll task
        poll = self._poll_tasks.pop(session_id, None)
        if poll:
            poll.cancel()
            try:
                await poll
            except asyncio.CancelledError:
                pass

        # Terminate process (in thread to avoid blocking event loop)
        child = self._sessions.pop(session_id, None)
        if child and hasattr(child, "isalive") and child.isalive():
            await asyncio.to_thread(self._kill_child, child)

        # Unregister from process manager
        if self._pm:
            self._pm.unregister(session_id)

        # Clean up status file
        status_file = self._status_files.pop(session_id, None)
        if status_file and status_file.exists():
            try:
                status_file.unlink()
            except Exception:
                pass

        # Clean up health scorer and session context
        self._health_scorers.pop(session_id, None)
        if self._session_store:
            self._session_store.flush_force(session_id)
            self._session_store.remove(session_id)

        # Clean up buffer and interrupt flag
        self._interrupted.discard(session_id)
        buf = self._buffers.pop(session_id, None)
        if buf:
            buf.close()

    @staticmethod
    def _kill_child(child: object) -> None:
        """Terminate a pexpect child process. Runs in a thread."""
        try:
            child.terminate(force=True)
            child.wait()
        except Exception:
            pass

    async def send_input(self, session_id: str, text: str) -> None:
        child = self._sessions.get(session_id)
        if child and hasattr(child, "sendline") and child.isalive():
            try:
                child.sendline(text)
                self._interrupted.discard(session_id)
            except Exception:
                pass

    async def send_raw(self, session_id: str, data: str) -> None:
        """Send raw bytes to PTY. Used by AgentPanel for key forwarding."""
        child = self._sessions.get(session_id)
        if child and hasattr(child, "send") and child.isalive():
            try:
                child.send(data)
                if data == "\x03":
                    # Ctrl+C interrupt — mark as waiting immediately
                    self._interrupted.add(session_id)
                else:
                    # User responding — clear interrupt flag
                    self._interrupted.discard(session_id)
            except Exception:
                pass

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
        """Resize PTY and virtual terminal buffer for a session."""
        buf = self._buffers.get(session_id)
        if buf:
            buf.resize(cols, rows)
        child = self._sessions.get(session_id)
        if child and hasattr(child, "setwinsize"):
            try:
                child.setwinsize(rows, cols)
            except Exception:
                pass

    def is_alive(self, session_id: str) -> bool:
        child = self._sessions.get(session_id)
        return child is not None and hasattr(child, "isalive") and child.isalive()

    def is_stage_complete(self, session_id: str) -> bool:
        """True if agent posted a stage completion marker."""
        buf = self._buffers.get(session_id)
        return buf.appears_stage_complete if buf else False

    def is_waiting_for_input(self, session_id: str) -> bool:
        # Ctrl+C interrupt — immediately waiting until agent responds
        if session_id in self._interrupted:
            return True
        buf = self._buffers.get(session_id)
        return buf.appears_waiting if buf else False

    def health(self, session_id: str) -> AgentHealth | None:
        """Compute current health for a session."""
        scorer = self._health_scorers.get(session_id)
        buf = self._buffers.get(session_id)
        if not scorer or not buf:
            return None
        alive = self.is_alive(session_id)
        _, _, stable_ticks = buf.stats
        screen_text = buf.display()
        status_data = self._read_status_file(session_id)
        h = scorer.compute(alive, stable_ticks, screen_text, status_data)

        # Record health event in session context
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
        """Public access to statusline data for a session."""
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
        """List all active session IDs."""
        return list(self._sessions.keys())

    async def _poll_output(self, session_id: str, child: object) -> None:
        """Read PTY output and feed into buffer."""
        import pexpect

        buf = self._buffers.get(session_id)
        if not buf:
            return
        while True:
            try:
                # Check if child is dead — break instead of spinning forever
                if hasattr(child, "isalive") and not child.isalive():
                    # Read remaining output
                    try:
                        remaining = child.read_nonblocking(size=4096, timeout=0)
                        if remaining:
                            buf.append(remaining)
                    except Exception:
                        pass
                    break

                # Non-blocking read
                if hasattr(child, "read_nonblocking"):
                    data = child.read_nonblocking(size=4096, timeout=0)
                    if data:
                        buf.append(data)
                        # Record for health scoring
                        scorer = self._health_scorers.get(session_id)
                        if scorer:
                            scorer.record_output(len(data))
                        # Record output event in session context
                        if self._session_store:
                            ctx = self._session_store.get(session_id)
                            if ctx:
                                ctx.add_event("output", {"text": data[-200:]})
                        # Respond to cursor position queries (DSR).
                        # CLIs like codex send \x1b[6n to detect terminal size.
                        if "\x1b[6n" in data:
                            row = buf._screen.cursor.y + 1
                            col = buf._screen.cursor.x + 1
                            child.send(f"\x1b[{row};{col}R")
                # Always check screen stability — even when raw data flows
                # (cursor blinks, escape codes), the rendered content may be stable
                buf.mark_idle()

            except pexpect.TIMEOUT:
                buf.mark_idle()
            except pexpect.EOF:
                break
            except Exception:
                break
            await asyncio.sleep(0.1)



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
            model=config.api_model or config.model or "claude-sonnet-4-6",
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
    """Track child processes for clean shutdown on crash/signal."""

    def __init__(self) -> None:
        self._children: dict[str, object] = {}

    def register(self, session_id: str, child: object) -> None:
        self._children[session_id] = child

    def unregister(self, session_id: str) -> None:
        self._children.pop(session_id, None)

    def cleanup_all(self) -> None:
        """Terminate all tracked child processes. Safe to call from atexit."""
        for sid, child in list(self._children.items()):
            try:
                if hasattr(child, "isalive") and child.isalive():
                    child.terminate(force=True)
                    try:
                        child.wait()
                    except Exception:
                        pass
            except Exception:
                pass
        self._children.clear()


# Singleton — shared between PtyBackend and atexit handler
_process_manager = _ProcessManager()
atexit.register(_process_manager.cleanup_all)


# --- Registry ---


class AgentRegistry:
    """Central agent manager. Creates backends on demand, tracks sessions."""

    def __init__(self, agents: dict[str, AgentConfig], sessions_dir: Path | None = None) -> None:
        self._configs = agents
        self._pty = PtyBackend(process_manager=_process_manager)
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
        """Terminate all sessions. Called on app exit."""
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
