"""Agent system: backend protocol, tmux/API backends, registry."""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import IO, Any, Protocol, runtime_checkable

from rich.text import Text

from .health import AgentHealth, HealthScorer, SessionStore
from .log import logger
from .models import AgentConfig, AgentMode, Task

# --- Protocol ---


@runtime_checkable
class AgentBackend(Protocol):
    """Everything the UI layer may call on a backend.

    Both `TmuxBackend` and `ApiBackend` implement all of it — API sessions return
    inert values for the terminal-only parts rather than omitting the methods, so
    callers never have to probe with `hasattr`.
    """

    async def start(
        self,
        config: AgentConfig,
        task: Task,
        prompt: str,
        cwd: Path,
        stage: str = "",
        cli_flags: str = "",
        terminal_size: tuple[int, int] | None = None,
    ) -> str: ...
    async def resume(self, session_id: str, prompt: str) -> None: ...
    async def stop(self, session_id: str) -> None: ...
    async def send_input(self, session_id: str, text: str) -> None: ...
    async def send_raw(self, session_id: str, data: str) -> None: ...
    async def get_output(self, session_id: str) -> str: ...
    async def get_output_rich(self, session_id: str) -> Text | None: ...
    async def get_history_rich(self, session_id: str) -> list[Text]: ...
    def is_alive(self, session_id: str) -> bool: ...
    def has_session(self, session_id: str) -> bool: ...
    def is_stage_complete(self, session_id: str) -> bool: ...
    def is_waiting_for_input(self, session_id: str) -> bool: ...
    def health(self, session_id: str) -> AgentHealth | None: ...
    def status_data(self, session_id: str) -> dict[str, Any] | None: ...
    def resize_session(self, session_id: str, cols: int, rows: int) -> None: ...
    def active_session_ids(self) -> list[str]: ...


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

# The marker an agent prints to declare its stage finished.
#
# Matched whole-line, not as a substring. The prompt that *asks* for this marker
# necessarily contains it too — prompts are rendered into the agent's own
# transcript — so a substring match fires on our own instruction as soon as the
# screen settles, before any work happens. Requiring the marker to be the entire
# line separates the agent's declaration from our request for it, because the
# instruction always carries "When done, output: " on the same line.
#
# `pipeline._DONE_INSTRUCTION` is built from this constant so the two can't drift,
# and is kept short enough that a pane can't wrap the marker onto a line of its
# own (which would look like a declaration).
STAGE_COMPLETE_MARKER = "<<<LLM-CC-DONE>>>"

# Pre-marker phrasing, still honoured under the same whole-line rule so sessions
# started before this existed — and agents that paraphrase — aren't stranded.
_LEGACY_COMPLETE_MARKERS = (
    "planning complete",
    "execute complete",
    "review complete",
)

_COMPLETE_MARKERS = (STAGE_COMPLETE_MARKER.lower(), *_LEGACY_COMPLETE_MARKERS)

# Gutter decoration a CLI may print before the marker (bullets, quote bars).
# Stripped before comparing; `<` is deliberately absent so the marker's own
# leading brackets survive.
_GUTTER_CHARS = " \t>⏺•·│┃*-"


class OutputBuffer:
    """Terminal output buffer.

    Two write paths:
    - `set_capture(plain, viewport_ansi, history_ansi)` — used by TmuxBackend with
      tmux's own emulator output via capture-pane. tmux did the rendering.
    - `append(data)` — used by ApiBackend for plain-text completions; also the
      path for tests. Data is appended verbatim and the viewport is bounded to
      the configured row height so heuristics over a "screen" still work.
    """

    def __init__(self, log_path: Path | None = None, rows: int = 40) -> None:
        self._rows = rows
        self._plain_viewport: str = ""
        self._ansi_viewport: str = ""
        self._ansi_history: str = ""
        self._log_file: IO[str] | None = None
        self._last_content: str = ""
        self._stable_ticks: int = 0
        # Derived views, rebuilt lazily and invalidated on every write. The poll
        # loop reads these several times per tick per session, so recomputing
        # them each time is the difference between free and ~1ms of string work.
        self._display: str | None = None
        self._display_lower: str | None = None
        self._complete_marker: bool | None = None
        self._rich_key: str | None = None
        self._rich: Text | None = None
        self._history_key: str | None = None
        self._history: list[Text] = []
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = open(log_path, "a")

    def _invalidate(self) -> None:
        self._display = None
        self._display_lower = None
        self._complete_marker = None

    def append(self, data: str) -> None:
        """Append plain text. Used by ApiBackend and by tests."""
        self._plain_viewport += data
        # Bound the viewport to last `rows` lines — mimics screen scrolling
        # so substring patterns scroll off correctly under append-only writes.
        lines = self._plain_viewport.split("\n")
        if len(lines) > self._rows:
            self._plain_viewport = "\n".join(lines[-self._rows:])
        self._ansi_viewport = self._plain_viewport
        self._invalidate()
        if self._log_file is not None:
            self._log_file.write(data)
            self._log_file.flush()

    def set_capture(
        self, plain: str, viewport_ansi: str, history_ansi: str | None = None
    ) -> None:
        """Replace internal state from a tmux capture-pane snapshot.

        `history_ansi=None` keeps the existing scrollback — the poll loop only
        re-captures it every few ticks.
        """
        self._plain_viewport = plain
        self._ansi_viewport = viewport_ansi
        if history_ansi is not None:
            self._ansi_history = history_ansi
        self._invalidate()

    @property
    def stable_ticks(self) -> int:
        """Consecutive poll ticks with unchanged visible content."""
        return self._stable_ticks

    def mark_idle(self) -> None:
        """Called each poll tick (~0.2s). Tracks if visible content has changed."""
        current = self.display()
        if current == self._last_content:
            self._stable_ticks += 1
        else:
            self._last_content = current
            self._stable_ticks = 0

    def display(self) -> str:
        """Current viewport as clean plain text, trailing whitespace trimmed."""
        if self._display is None:
            text = self._plain_viewport.replace("\r\n", "\n").replace("\r", "\n")
            cleaned = [line.rstrip() for line in text.split("\n")]
            while cleaned and not cleaned[-1]:
                cleaned.pop()
            self._display = "\n".join(cleaned)
        return self._display

    def _display_lowered(self) -> str:
        if self._display_lower is None:
            self._display_lower = self.display().lower()
        return self._display_lower

    def display_rich(self) -> Text:
        """Viewport as Rich Text with ANSI colors preserved."""
        if self._rich_key != self._ansi_viewport:
            self._rich = Text.from_ansi(self._ansi_viewport.rstrip())
            self._rich_key = self._ansi_viewport
        assert self._rich is not None
        return self._rich

    def history_rich(self) -> list[Text]:
        """Scrollback history above the viewport, line by line.

        Cached on the raw capture: this parses up to 5000 lines and the viewer
        asks for it 5x/second, but it only changes when lines scroll off.
        """
        if self._history_key != self._ansi_history:
            self._history = (
                [Text.from_ansi(line) for line in self._ansi_history.splitlines()]
                if self._ansi_history
                else []
            )
            self._history_key = self._ansi_history
        return self._history

    def resize(self, rows: int) -> None:
        self._rows = rows

    def _has_complete_marker(self) -> bool:
        """True if any visible line is *only* a completion marker.

        Cached with the rest of the derived views — this walks the whole viewport
        and the poll loops ask for it several times a second per session.
        """
        if self._complete_marker is None:
            self._complete_marker = any(
                line.strip().lstrip(_GUTTER_CHARS).strip() in _COMPLETE_MARKERS
                for line in self._display_lowered().split("\n")
            )
        return self._complete_marker

    @property
    def appears_stage_complete(self) -> bool:
        """Agent posted a stage completion marker. Requires settled content."""
        return self._stable_ticks >= 3 and self._has_complete_marker()

    @property
    def appears_waiting(self) -> bool:
        """Heuristic: agent seems to be waiting for user input.

        Triggers when the visible screen content hasn't changed for ~0.6s
        AND the screen matches known input prompt patterns.
        Does NOT trigger for stage completion — that's a separate state.
        """
        if self._stable_ticks < 3:
            return False
        if self.appears_stage_complete:
            return False
        screen_text = self._display_lowered()
        return any(p in screen_text for p in _INPUT_PATTERNS)

    def close(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError as e:
                logger.debug("output buffer close failed: %s", e)
            self._log_file = None


# --- Tmux Backend ---


_TMUX_NAME_BAD = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_session_name(raw: str) -> str:
    """Reduce a string to tmux's session-name charset: alnum + `_` + `-`.

    Also the single source of truth for Textual widget ids derived from session
    ids (`ui/panels.py`), which need the same restriction.
    """
    return _TMUX_NAME_BAD.sub("_", raw)


async def _tmux(*args: str) -> tuple[int, bytes, bytes]:
    """Run a tmux command via execFile semantics. Returns (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "tmux", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out, err


def render_args(config: AgentConfig, prompt: str, task_id: str) -> str:
    """Render an agent's args template.

    An empty prompt means "just open the CLI" (one-off sessions): rendering the
    template would hand the agent a literal empty argument instead.
    """
    if not prompt:
        return ""
    return config.args_template.format(
        prompt=shlex.quote(prompt),
        session_id=task_id,
        model=config.model or "",
    )


def build_command(config: AgentConfig, prompt: str, task_id: str, cli_flags: str = "") -> str:
    """Assemble the shell command that runs an agent with its prompt.

    Argument order matters: `--allowedTools <tools...>` is variadic, so anything
    positional following it is swallowed as another tool name. That is silent —
    the CLI just opens with no prompt at all — so the flag has to stay *after*
    the prompt. Keep any list-valued flag added here at the tail for the same
    reason.
    """
    if not config.command:
        raise ValueError(f"Agent '{config.name}' has no command configured for PTY mode")

    # Inject --model if model is set and not already in args_template
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
    parts = [
        config.command,
        model_flag,
        full_auto_flag,
        cli_flags,
        render_args(config, prompt, task_id),
        allowed_flag,
    ]
    return " ".join(p for p in parts if p).strip()


def _tmux_sync(*args: str) -> tuple[int, bytes]:
    """Blocking tmux call. Only for atexit cleanup and cold `is_alive` misses.

    Forking costs ~9ms, which is why the poll loop never calls this — it takes
    liveness from the async `_capture` it already performs.
    """
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


# How long a sampled liveness result stays usable. The poll loop refreshes every
# 0.2s, so external callers effectively always hit the cache.
_ALIVE_TTL = 1.0

# Scrollback only changes when lines scroll off the pane, so re-capturing 5000
# lines at the full poll rate is waste. Every Nth tick is plenty.
_HISTORY_EVERY_N_TICKS = 5


class TmuxBackend:
    """Spawns CLI agents in tmux sessions. tmux owns the PTY layer."""

    def __init__(self, process_manager: _ProcessManager | None = None) -> None:
        self._sessions: set[str] = set()
        self._buffers: dict[str, OutputBuffer] = {}
        self._log_paths: dict[str, Path] = {}
        self._poll_tasks: dict[str, asyncio.Task[None]] = {}
        self._interrupted: set[str] = set()
        self._pm = process_manager
        self._health_scorers: dict[str, HealthScorer] = {}
        self._session_store: SessionStore | None = None
        self._status_files: dict[str, Path] = {}
        self._alive: dict[str, tuple[bool, float]] = {}  # session_id -> (alive, sampled_at)
        self._resize_tasks: set[asyncio.Task[tuple[int, bytes, bytes]]] = set()

    def set_session_store(self, store: SessionStore) -> None:
        self._session_store = store

    def _record_alive(self, session_id: str, alive: bool) -> None:
        self._alive[session_id] = (alive, time.monotonic())

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
        session_id = sanitize_session_name(
            f"llmcc_{task.id}_{stage or task.status.value}"
        )

        # Stop old session if same ID exists (prevents orphans)
        if session_id in self._sessions:
            await self.stop(session_id)

        full_cmd = build_command(config, prompt, task.id, cli_flags)

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

        self._sessions.add(session_id)
        # OutputBuffer log_path is None — pipe-pane is the sole writer.
        self._buffers[session_id] = OutputBuffer(log_path=None, rows=rows)
        self._log_paths[session_id] = log_path
        self._health_scorers[session_id] = HealthScorer()
        self._record_alive(session_id, True)

        # Create session context for persistence
        if self._session_store:
            self._session_store.get_or_create(
                session_id, task.id, stage or task.status.value, config.name,
            )

        # Register with process manager only in --clean-exit mode; otherwise
        # the session is allowed to outlive llm-cc.
        if self._pm and _clean_exit_mode:
            self._pm.register(session_id)

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
            rows = os.get_terminal_size().lines
        except OSError:
            rows = 40

        self._sessions.add(session_id)
        self._buffers[session_id] = OutputBuffer(log_path=None, rows=rows)
        self._log_paths[session_id] = log_path
        self._health_scorers[session_id] = HealthScorer()
        self._status_files[session_id] = cwd / ".llm-cc" / "status" / f"{task.id}.json"
        self._record_alive(session_id, True)

        if self._session_store:
            self._session_store.get_or_create(
                session_id, task.id, stage or task.status.value, agent_name,
            )

        if self._pm and _clean_exit_mode:
            self._pm.register(session_id)

        self._poll_tasks[session_id] = asyncio.create_task(
            self._poll_output(session_id, log_path)
        )
        return True

    async def detach(self, session_id: str) -> None:
        """Tear down local state without killing the tmux session.

        Used at app shutdown so sessions persist for the next launch.
        """
        await self._teardown(session_id, kill=False)

    async def stop(self, session_id: str) -> None:
        """Tear down local state and kill the tmux session."""
        await self._teardown(session_id, kill=True)

    async def _teardown(self, session_id: str, *, kill: bool) -> None:
        """Release everything held for a session.

        `kill=True` also destroys the tmux session and the status file;
        `kill=False` leaves both alone because the agent process is still
        running and still writing to them.
        """
        poll = self._poll_tasks.pop(session_id, None)
        if poll:
            poll.cancel()
            try:
                await poll
            except asyncio.CancelledError:
                pass

        existed = session_id in self._sessions
        self._sessions.discard(session_id)
        if kill and existed:
            # Idempotent — tmux returns non-zero if the session is already gone.
            await _tmux("kill-session", "-t", session_id)

        if self._pm:
            self._pm.unregister(session_id)

        status_file = self._status_files.pop(session_id, None)
        if kill and status_file and status_file.exists():
            try:
                status_file.unlink()
            except OSError as e:
                logger.debug("could not remove status file %s: %s", status_file, e)

        self._health_scorers.pop(session_id, None)
        if self._session_store:
            self._session_store.flush_force(session_id)
            self._session_store.remove(session_id)

        self._interrupted.discard(session_id)
        self._alive.pop(session_id, None)
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
            buf.resize(rows)
        if session_id in self._sessions:
            task = asyncio.create_task(
                _tmux("resize-window", "-t", session_id, "-x", str(cols), "-y", str(rows))
            )
            # Keep a reference so the task isn't garbage-collected mid-flight.
            self._resize_tasks.add(task)
            task.add_done_callback(self._resize_tasks.discard)

    def has_session(self, session_id: str) -> bool:
        """True if this backend is tracking the session (no tmux call)."""
        return session_id in self._sessions

    def is_alive(self, session_id: str) -> bool:
        """Liveness, served from the poll loop's sample when it is fresh.

        The poll loop records a result every 0.2s from the capture it already
        performs, so this normally costs nothing. Only a cold or stale entry
        (no poll loop running yet, or one that has stopped) pays for a fork.
        """
        if session_id not in self._sessions:
            return False
        cached = self._alive.get(session_id)
        if cached is not None and time.monotonic() - cached[1] < _ALIVE_TTL:
            return cached[0]
        rc, _ = _tmux_sync("has-session", "-t", session_id)
        alive = rc == 0
        self._record_alive(session_id, alive)
        return alive

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
        screen_text = buf.display()
        status_data = self._read_status_file(session_id)
        h = scorer.compute(alive, buf.stable_ticks, screen_text, status_data)

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

    def status_data(self, session_id: str) -> dict[str, Any] | None:
        return self._read_status_file(session_id)

    def _read_status_file(self, session_id: str) -> dict[str, Any] | None:
        status_file = self._status_files.get(session_id)
        if not status_file or not status_file.exists():
            return None
        try:
            data = json.loads(status_file.read_text())
        except (OSError, ValueError) as e:
            # Expected transiently: the statusline hook rewrites this file
            # non-atomically, so a read can land mid-write.
            logger.debug("status file unreadable for %s: %s", session_id, e)
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def context_monitor_warning(scorer: HealthScorer) -> str | None:
        return scorer.context_monitor.warning_level

    def active_session_ids(self) -> list[str]:
        return list(self._sessions)

    def _drain_activity(self, session_id: str, log_fd: IO[bytes] | None) -> None:
        """Read whatever pipe-pane appended; feed byte counts to health scoring.

        The bytes are not rendered — capture-pane is the source of truth for the
        screen. This only measures that the agent is producing *something*, and
        keeps a short excerpt for the session handoff file.
        """
        if log_fd is None:
            return
        try:
            data = log_fd.read()
        except OSError as e:
            logger.debug("activity log read failed for %s: %s", session_id, e)
            return
        if not data:
            return
        scorer = self._health_scorers.get(session_id)
        if scorer:
            scorer.record_output(len(data))
        if self._session_store:
            ctx = self._session_store.get(session_id)
            if ctx:
                ctx.add_event(
                    "output", {"text": data[-200:].decode("utf-8", errors="replace")}
                )

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
            log_fd: IO[bytes] | None = log_path.open("rb")
        except OSError as e:
            logger.debug("could not open activity log %s: %s", log_path, e)
            log_fd = None

        tick = 0
        try:
            while True:
                self._drain_activity(session_id, log_fd)

                # Liveness comes from the capture itself: capture-pane fails once
                # the session is gone, so a separate blocking has-session probe
                # would be a second fork telling us what we already know.
                with_history = tick % _HISTORY_EVERY_N_TICKS == 0
                captured = await self._capture(session_id, with_history=with_history)
                alive = captured is not None
                self._record_alive(session_id, alive)
                if captured is not None:
                    viewport_ansi, history_ansi = captured
                    plain = Text.from_ansi(viewport_ansi).plain
                    buf.set_capture(plain, viewport_ansi, history_ansi)

                buf.mark_idle()

                if not alive:
                    self._drain_activity(session_id, log_fd)  # final drain
                    break

                tick += 1
                await asyncio.sleep(0.2)
        finally:
            if log_fd is not None:
                try:
                    log_fd.close()
                except OSError as e:
                    logger.debug("activity log close failed for %s: %s", session_id, e)

    async def _capture(
        self, session_id: str, *, with_history: bool = True
    ) -> tuple[str, str | None] | None:
        """Snapshot the pane. Returns (ansi_viewport, ansi_history) or None if gone.

        A `None` history means "unchanged, keep what you have" — scrollback is
        only re-read every few ticks because it costs a 5000-line capture and
        only changes when lines scroll off the pane.

        Plain text is derived from the ANSI capture by the caller rather than
        fetched separately: it saves a fork per tick, and it guarantees the plain
        and styled views describe the same instant (two captures could not).
        """
        rc, ansi_b, _ = await _tmux("capture-pane", "-t", session_id, "-e", "-p")
        if rc != 0:
            return None
        viewport_ansi = ansi_b.decode("utf-8", errors="replace")
        if not with_history:
            return viewport_ansi, None
        rc_hist, hist_b, _ = await _tmux(
            "capture-pane", "-t", session_id, "-e", "-p", "-S", "-5000", "-E", "-1",
        )
        history_ansi = hist_b.decode("utf-8", errors="replace") if rc_hist == 0 else ""
        return viewport_ansi, history_ansi



# --- API Backend ---


class ApiBackend:
    """Calls AI SDKs directly for automated stages. Lazy imports."""

    def __init__(self) -> None:
        self._results: dict[str, str] = {}
        self._running: dict[str, asyncio.Task[None]] = {}
        self._buffers: dict[str, OutputBuffer] = {}

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
        # Content is a list of blocks; only text blocks carry a response.
        for block in msg.content:
            if hasattr(block, "text"):
                return str(block.text)
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

    def has_session(self, session_id: str) -> bool:
        return session_id in self._running

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

    # --- Terminal-only protocol members ---
    # API sessions have no PTY: there is nothing to style, scroll, resize, or
    # watch for prompts. These return inert values so callers can treat every
    # backend uniformly instead of probing for capabilities.

    async def get_output_rich(self, session_id: str) -> Text | None:
        return None

    async def get_history_rich(self, session_id: str) -> list[Text]:
        return []

    def is_stage_complete(self, session_id: str) -> bool:
        return False

    def is_waiting_for_input(self, session_id: str) -> bool:
        return False

    def status_data(self, session_id: str) -> dict[str, Any] | None:
        return None

    def resize_session(self, session_id: str, cols: int, rows: int) -> None:
        return None


# --- Process Manager (crash cleanup) ---


class _ProcessManager:
    """Track tmux session names for clean shutdown on crash/signal."""

    def __init__(self) -> None:
        self._sessions: set[str] = set()

    def register(self, session_id: str) -> None:
        self._sessions.add(session_id)

    def unregister(self, session_id: str) -> None:
        self._sessions.discard(session_id)

    def cleanup_all(self) -> None:
        """Kill all tracked tmux sessions. Safe to call from atexit."""
        for name in list(self._sessions):
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
        if self._pty.has_session(session_id):
            await self._pty.stop(session_id)
        elif self._api.has_session(session_id):
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

        return reattached, orphans
