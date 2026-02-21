"""Agent system: backend protocol, PTY/API backends, registry."""

from __future__ import annotations

import asyncio
import atexit
import shlex
import shutil
from pathlib import Path
from typing import Protocol, runtime_checkable

import pyte

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
    # Stage completion markers (injected into agent prompts)
    "planning complete",
    "execute complete",
    "review complete",
)


class OutputBuffer:
    """Terminal emulator buffer. Uses pyte to properly decode PTY output."""

    def __init__(self, log_path: Path | None = None, cols: int = 120, rows: int = 40) -> None:
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.Stream(self._screen)
        self._log_file = None
        self._last_content: str = ""
        self._stable_ticks: int = 0  # how many polls the screen hasn't changed
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = open(log_path, "a")

    def append(self, data: str) -> None:
        self._stream.feed(data)
        if self._log_file:
            self._log_file.write(data)
            self._log_file.flush()

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

    @property
    def appears_waiting(self) -> bool:
        """Heuristic: agent seems to be waiting for user input.

        Triggers when the visible screen content hasn't changed for ~0.3s
        AND the screen matches known input prompt patterns.
        Searches full screen since CLI prompts can render anywhere.
        """
        if self._stable_ticks < 3:  # screen still changing (~0.3s)
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

    async def start(self, config: AgentConfig, task: Task, prompt: str, cwd: Path, stage: str = "", cli_flags: str = "") -> str:
        import pexpect

        session_id = f"pty_{task.id}_{stage or task.status.value}"

        # Stop old session if same ID exists (prevents orphans)
        if session_id in self._sessions:
            await self.stop(session_id)

        if not config.command:
            raise ValueError(f"Agent '{config.name}' has no command configured for PTY mode")

        # Build command
        cmd_args = config.args_template.format(
            prompt=shlex.quote(prompt),
            session_id=task.id,
        )
        full_cmd = f"{config.command} {cli_flags} {cmd_args}".strip() if cli_flags else f"{config.command} {cmd_args}"

        # Log path
        log_path = cwd / ".llm-cc" / "logs" / f"{session_id}.log"

        # Spawn in PTY
        child = pexpect.spawn(
            full_cmd,
            cwd=str(cwd),
            encoding="utf-8",
            timeout=None,
            dimensions=(40, 120),
        )

        self._sessions[session_id] = child
        self._buffers[session_id] = OutputBuffer(log_path=log_path)

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

    def is_alive(self, session_id: str) -> bool:
        child = self._sessions.get(session_id)
        return child is not None and hasattr(child, "isalive") and child.isalive()

    def is_waiting_for_input(self, session_id: str) -> bool:
        # Ctrl+C interrupt — immediately waiting until agent responds
        if session_id in self._interrupted:
            return True
        buf = self._buffers.get(session_id)
        return buf.appears_waiting if buf else False

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
            model=config.api_model or "claude-sonnet-4-20250514",
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
            model=config.api_model or "gpt-4o",
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
            except Exception:
                pass
        self._children.clear()


# Singleton — shared between PtyBackend and atexit handler
_process_manager = _ProcessManager()
atexit.register(_process_manager.cleanup_all)


# --- Registry ---


class AgentRegistry:
    """Central agent manager. Creates backends on demand, tracks sessions."""

    def __init__(self, agents: dict[str, AgentConfig]) -> None:
        self._configs = agents
        self._pty = PtyBackend(process_manager=_process_manager)
        self._api = ApiBackend()

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

    async def cleanup_all(self) -> None:
        """Terminate all sessions. Called on app exit."""
        # Gather all session IDs, stop concurrently
        pty_sids = self._pty.active_session_ids()
        api_sids = self._api.active_session_ids()
        tasks = [self._pty.stop(sid) for sid in pty_sids]
        tasks += [self._api.stop(sid) for sid in api_sids]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
