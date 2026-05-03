"""Modal dialogs (task input, diff) and embedded agent panel widget."""

from __future__ import annotations

import asyncio
import time

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static, TextArea

from llm_cc.models import Task


class TaskInputDialog(ModalScreen[Task | None]):
    """Create or edit a task."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save", priority=True),
    ]

    def __init__(self, task: Task | None = None) -> None:
        super().__init__()
        self._editing = task

    def compose(self) -> ComposeResult:
        with Vertical(id="task-input-container"):
            yield Static(
                "[b]Edit task[/]" if self._editing else "[b]New task[/]",
                id="task-input-title",
            )
            yield Static(
                "[b]Ctrl+S[/] save  ·  [b]Esc[/] cancel  ·  [b]Tab[/] next field",
                id="task-input-hint",
            )

            with VerticalScroll(id="task-input-form"):
                yield Label("Title")
                yield Input(
                    value=self._editing.title if self._editing else "",
                    placeholder="Short imperative summary (e.g. 'add OAuth login')",
                    id="task-title",
                )
                yield Static("", id="task-title-error", classes="form-error")

                yield Label("Spec  [dim](markdown supported)[/]")
                yield TextArea(
                    (self._editing.description or "") if self._editing else "",
                    id="task-desc",
                    language="markdown",
                )

                yield Label("How to verify  [dim](optional)[/]")
                yield TextArea(
                    (self._editing.verify or "") if self._editing else "",
                    id="task-verify",
                )

                yield Label("Done when  [dim](optional)[/]")
                yield TextArea(
                    (self._editing.done or "") if self._editing else "",
                    id="task-done",
                )

            with Horizontal(id="task-input-buttons"):
                yield Button("Cancel", id="cancel-btn")
                yield Button("Save", variant="primary", id="save-btn")

    def on_mount(self) -> None:
        self.query_one("#task-title", Input).focus()

    @on(Button.Pressed, "#save-btn")
    def handle_save(self) -> None:
        self._do_save()

    @on(Button.Pressed, "#cancel-btn")
    def handle_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#task-title")
    def handle_submit(self) -> None:
        self._do_save()

    @on(Input.Changed, "#task-title")
    def handle_title_changed(self, event: Input.Changed) -> None:
        """Split pasted multi-line content: first line → title, rest → spec."""
        value = event.value
        if "\n" not in value:
            return
        first, _, rest = value.partition("\n")
        title_input = self.query_one("#task-title", Input)
        title_input.value = first.strip()
        if rest.strip():
            desc_area = self.query_one("#task-desc", TextArea)
            existing = desc_area.text
            combined = rest.strip() if not existing else f"{existing}\n{rest.strip()}"
            desc_area.text = combined
        # Clear any existing error
        self.query_one("#task-title-error", Static).update("")

    def _do_save(self) -> None:
        title = self.query_one("#task-title", Input).value.strip()
        err_label = self.query_one("#task-title-error", Static)
        if not title:
            err_label.update("[#f87171]Title is required[/]")
            self.query_one("#task-title", Input).focus()
            return
        err_label.update("")
        desc = self.query_one("#task-desc", TextArea).text.strip() or None
        verify = self.query_one("#task-verify", TextArea).text.strip() or None
        done = self.query_one("#task-done", TextArea).text.strip() or None

        if self._editing:
            self._editing.title = title
            self._editing.description = desc
            self._editing.verify = verify
            self._editing.done = done
            self._editing.touch()
            self.dismiss(self._editing)
        else:
            task = Task(
                title=title,
                description=desc,
                verify=verify,
                done=done,
            )
            self.dismiss(task)

    def action_save(self) -> None:
        self._do_save()

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """Keyboard reference modal grouped by topic."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("question_mark", "close", "Close"),
        Binding("q", "close", "Close"),
    ]

    SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
        ("Navigation", (
            ("[  ]", "move column left / right"),
            ("j  k", "move down / up within column"),
            ("← → ↑ ↓", "arrow alternatives"),
        )),
        ("Tasks", (
            ("o", "new task"),
            ("e", "edit selected task"),
            ("x", "delete selected task"),
        )),
        ("Pipeline", (
            ("m", "advance to next stage"),
            ("b", "back to previous stage"),
            ("r", "restart agent for current stage"),
            ("s", "stop agent"),
            ("d", "show diff against base"),
        )),
        ("Tabs & Agent", (
            ("enter", "open agent tab for selected task"),
            ("ctrl+o", "back to overview"),
            ("ctrl+→", "next agent tab"),
            ("ctrl+←", "previous agent tab"),
        )),
        ("Inside an Agent Tab", (
            ("ctrl+c", "interrupt agent"),
            ("ctrl+t", "open text-input mode"),
            ("ctrl+v", "paste clipboard"),
            ("ctrl+y", "copy recent output"),
            ("shift+pgup/pgdn", "scroll history"),
            ("shift+end", "resume follow-tail"),
            ("esc", "back to overview"),
        )),
        ("Diff View", (
            ("j  k", "scroll line"),
            ("ctrl+d  ctrl+u", "page down / up"),
            ("g  G", "top / bottom"),
            ("q  esc", "close"),
        )),
        ("Misc", (
            ("?", "show this help"),
            ("q", "quit (confirms if agents running)"),
        )),
    )

    def compose(self) -> ComposeResult:
        with Vertical(id="help-container"):
            yield Static("[b]Keyboard Shortcuts[/]", id="help-title")
            yield Static(
                "[b]Esc[/] or [b]?[/] to close",
                id="help-hint",
            )
            yield VerticalScroll(Static(self._build_text(), id="help-body"))

    def _build_text(self) -> str:
        out: list[str] = []
        for title, rows in self.SECTIONS:
            out.append(f"\n[b #8b9eff]{title}[/]")
            for key, desc in rows:
                out.append(f"  [b #8b9eff]{key:<18}[/]  [dim]{desc}[/]")
        return "\n".join(out)

    def action_close(self) -> None:
        self.dismiss(None)


class ConfirmDialog(ModalScreen[bool]):
    """Yes/no confirmation. Pass severity='warning' for reversible actions,
    'danger' for destructive ones."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
    ]

    def __init__(self, prompt: str, severity: str = "danger") -> None:
        super().__init__()
        self._prompt = prompt
        self._severity = severity if severity in ("warning", "danger") else "danger"

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-container", classes=f"-{self._severity}"):
            yield Static(self._prompt)
            with Horizontal(id="confirm-buttons"):
                btn_variant = "error" if self._severity == "danger" else "warning"
                yield Button("Yes", variant=btn_variant, id="confirm-yes")
                yield Button("No", variant="default", id="confirm-no")

    def on_mount(self) -> None:
        self.query_one("#confirm-no", Button).focus()

    @on(Button.Pressed, "#confirm-yes")
    def handle_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-no")
    def handle_no(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class AgentPanelView(Container):
    """Embedded live agent output viewer with raw key forwarding to PTY.

    Mounted inside a TabPane (no longer a ModalScreen). All keys are
    forwarded to the agent's tmux session unless they are reserved for
    UI navigation:

    - Esc                       → request close (parent switches to Overview)
    - Ctrl+C                    → send interrupt to agent
    - Ctrl+V                    → paste clipboard
    - Ctrl+Y                    → copy recent output to clipboard
    - Ctrl+T                    → toggle text input mode
    - Shift+PgUp/PgDn/Home/End  → scroll output history
    """

    DEFAULT_CSS = ""
    can_focus = True

    class CloseRequested(Message):
        """Emitted when the user wants to leave this agent tab."""

        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    def __init__(
        self,
        session_id: str,
        backend: object,
        *,
        title: str = "",
        agent: str = "",
        model: str = "",
        branch: str = "",
    ) -> None:
        super().__init__(id=_view_id(session_id))
        self._session_id = session_id
        self._backend = backend
        self._title = title
        self._agent = agent
        self._model = model
        self._branch = branch
        self._polling = True
        self._input_mode = False
        self._follow_tail = True
        self._glyph_cache: str = ""
        self._glyph_at: float = 0.0

    def _titlebar_text(self) -> str:
        parts: list[str] = []
        if self._agent:
            parts.append(f"[bold]{self._agent}[/]")
        if self._model:
            parts.append(f"[dim]{self._model}[/]")
        if self._branch:
            parts.append(f"[#8b9eff]{self._branch}[/]")
        if self._title:
            parts.append(f"[dim]{self._title}[/]")
        return "  ·  ".join(parts) if parts else f"[dim]{self._session_id}[/]"

    @staticmethod
    def _default_help() -> str:
        return (
            "[b]Esc[/] back  ·  [b]Ctrl+C[/] interrupt  ·  "
            "[b]Ctrl+T[/] type  ·  [b]Ctrl+V[/] paste  ·  "
            "[b]Ctrl+Y[/] copy  ·  [b]Shift+PgUp/Dn[/] scroll"
        )

    @staticmethod
    def _input_help() -> str:
        return "[b]Enter[/] send  ·  [b]Esc[/] exit text-input mode"

    def compose(self) -> ComposeResult:
        yield Static(self._titlebar_text(), classes="agent-titlebar")
        yield Static(
            "[b]⏸ Waiting for input[/] · [dim]Ctrl+T to type, then Enter[/]",
            classes="agent-prompt-banner",
        )
        yield VerticalScroll(
            Static("Loading...", classes="agent-output"),
            classes="agent-scroll",
        )
        # Bottom strip — Textual stacks dock:bottom siblings with the
        # first-composed widget closest to the bottom edge. Compose in
        # reverse so the visual order (top→bottom) is: pause, input, status, help.
        yield Static(self._default_help(), classes="agent-help")
        yield Static("[dim]waiting for agent…[/]", classes="agent-status")
        yield Input(
            placeholder="Type, Enter to send · Esc to exit input mode",
            classes="agent-input",
        )
        yield Static(
            "[b]PAUSED[/] · [dim]Shift+End to resume tail[/]",
            classes="agent-pause",
        )

    def on_mount(self) -> None:
        inp = self.query_one(".agent-input", Input)
        inp.display = False
        self.query_one(".agent-pause", Static).display = False
        self.query_one(".agent-prompt-banner", Static).display = False
        self.call_after_refresh(self._resize_pty)
        self.run_worker(
            self._poll_loop(),
            group=f"agent-poll-{self._session_id}",
            exclusive=True,
        )

    def on_unmount(self) -> None:
        self._polling = False

    def on_resize(self, event) -> None:
        self._resize_pty()

    def _resize_pty(self) -> None:
        try:
            scroll = self.query_one(".agent-scroll", VerticalScroll)
            cols = scroll.size.width - 2
            rows = scroll.size.height
            if cols > 0 and rows > 0 and hasattr(self._backend, "resize_session"):
                self._backend.resize_session(self._session_id, cols, rows)
        except Exception:
            pass

    async def on_key(self, event) -> None:
        if self._input_mode:
            if event.key == "escape":
                self._input_mode = False
                self.query_one(".agent-input", Input).display = False
                self.query_one(".agent-help", Static).update(self._default_help())
                event.prevent_default()
                event.stop()
            return

        if event.key == "escape":
            event.prevent_default()
            event.stop()
            self.post_message(self.CloseRequested(self._session_id))
            return

        scroll_keys = {
            "shift+pageup": "scroll_page_up",
            "shift+pagedown": "scroll_page_down",
            "shift+home": "scroll_home",
            "shift+end": "scroll_end",
        }
        if event.key in scroll_keys:
            scroll = self.query_one(".agent-scroll", VerticalScroll)
            getattr(scroll, scroll_keys[event.key])(animate=False)
            # Tail-follow: only resume on shift+end
            self._follow_tail = event.key == "shift+end"
            self._update_pause_chip()
            event.prevent_default()
            event.stop()
            return

        if event.key == "ctrl+c":
            await self._send_raw("\x03")
            event.prevent_default()
            event.stop()
            return

        if event.key == "ctrl+v":
            try:
                import subprocess
                clip = subprocess.run(
                    ["pbpaste"], capture_output=True, text=True, timeout=2
                )
                if clip.returncode == 0 and clip.stdout:
                    await self._send_raw(clip.stdout)
            except Exception:
                pass
            event.prevent_default()
            event.stop()
            return

        if event.key == "ctrl+y":
            try:
                import subprocess
                text = ""
                if hasattr(self._backend, "get_output"):
                    text = await self._backend.get_output(self._session_id) or ""
                if text:
                    proc = subprocess.run(
                        ["pbcopy"], input=text, text=True, timeout=2
                    )
                    if proc.returncode == 0:
                        self.app.notify("Copied agent output to clipboard")
                    else:
                        self.app.notify("Copy failed", severity="warning")
                else:
                    self.app.notify("No output to copy", severity="warning")
            except Exception as e:
                self.app.notify(f"Copy error: {e}", severity="error")
            event.prevent_default()
            event.stop()
            return

        if event.key == "ctrl+t":
            self._input_mode = True
            inp = self.query_one(".agent-input", Input)
            inp.display = True
            inp.focus()
            self.query_one(".agent-help", Static).update(self._input_help())
            event.prevent_default()
            event.stop()
            return

        key_map = {
            "enter": "\r",
            "up": "\x1b[A",
            "down": "\x1b[B",
            "left": "\x1b[D",
            "right": "\x1b[C",
            "tab": "\t",
            "backspace": "\x7f",
        }
        seq = key_map.get(event.key)
        if seq:
            await self._send_raw(seq)
            event.prevent_default()
            event.stop()
        elif event.character and len(event.character) == 1 and event.character.isprintable():
            await self._send_raw(event.character)
            event.prevent_default()
            event.stop()

    async def _send_raw(self, data: str) -> None:
        if hasattr(self._backend, "send_raw"):
            await self._backend.send_raw(self._session_id, data)

    def _update_pause_chip(self) -> None:
        try:
            self.query_one(".agent-pause", Static).display = not self._follow_tail
        except Exception:
            pass

    def _heartbeat_glyph(self) -> str:
        """Health-driven glyph: ● healthy, ◐ degraded, ▪ idle, ✕ dead.

        Cached for ~1s — `is_alive` calls fork tmux, so calling every
        poll tick (5Hz) would mean 5+ subprocesses/sec per open agent tab.
        """
        now = time.monotonic()
        if now - self._glyph_at < 1.0:
            return self._glyph_cache
        backend = self._backend
        if not hasattr(backend, "is_alive"):
            self._glyph_cache = ""
            self._glyph_at = now
            return self._glyph_cache
        try:
            alive = backend.is_alive(self._session_id)
        except Exception:
            alive = False
        if not alive:
            glyph = "[#f87171]✕[/]"
        else:
            score: int | None = None
            if hasattr(backend, "health"):
                try:
                    h = backend.health(self._session_id)
                    if h is not None:
                        score = h.score
                except Exception:
                    pass
            if score is None or score >= 75:
                glyph = "[#4ade80]●[/]"
            elif score >= 50:
                glyph = "[#fbbf24]◐[/]"
            else:
                glyph = "[#fb923c]▪[/]"
        self._glyph_cache = glyph
        self._glyph_at = now
        return glyph

    def _format_status_bar(self, status_data: dict) -> str:
        parts: list[str] = []
        ctx = status_data.get("context_window", {})
        usage = ctx.get("current_usage") or {}

        used = ctx.get("used_percentage")
        if used is not None:
            remaining = 100 - used
            if remaining >= 70:
                color = "green"
            elif remaining >= 30:
                color = "yellow"
            else:
                color = "red"
            parts.append(f"[{color}]{remaining}% ctx[/{color}]")

        for key, label in (
            ("input_tokens", "in"),
            ("output_tokens", "out"),
            ("cache_creation_input_tokens", "cache wr"),
            ("cache_read_input_tokens", "cache rd"),
        ):
            v = usage.get(key)
            if v is not None:
                parts.append(f"{v / 1000:.1f}k {label}")
        return " | ".join(parts)

    async def _poll_loop(self) -> None:
        from rich.text import Text

        output_widget = self.query_one(".agent-output", Static)
        scroll = self.query_one(".agent-scroll", VerticalScroll)
        status_widget = self.query_one(".agent-status", Static)
        while self._polling:
            try:
                at_bottom = (
                    scroll.max_scroll_y <= 0
                    or scroll.scroll_offset.y >= scroll.max_scroll_y - 2
                )
                # If user scrolled up since last tick, drop tail-follow
                if self._follow_tail and not at_bottom:
                    self._follow_tail = False
                    self._update_pause_chip()

                rich_content: Text | None = None
                history: list[Text] = []
                if hasattr(self._backend, "get_output_rich"):
                    rich_content = await self._backend.get_output_rich(self._session_id)
                    history = await self._backend.get_history_rich(self._session_id)

                if rich_content is not None:
                    if history:
                        combined = Text()
                        for i, line in enumerate(history):
                            if i > 0:
                                combined.append("\n")
                            combined.append_text(line)
                        combined.append("\n")
                        combined.append_text(rich_content)
                        output_widget.update(combined)
                    else:
                        output_widget.update(rich_content)
                else:
                    text = await self._backend.get_output(self._session_id)
                    if text:
                        output_widget.update(text)

                status_data = None
                if hasattr(self._backend, "status_data"):
                    status_data = self._backend.status_data(self._session_id)
                glyph = self._heartbeat_glyph()
                if status_data:
                    bar = self._format_status_bar(status_data)
                    status_widget.update(
                        f"{glyph}  {bar}" if glyph else bar
                    )
                elif glyph:
                    status_widget.update(f"{glyph}  [dim]waiting for telemetry…[/]")

                # Waiting-for-input banner
                waiting = False
                if hasattr(self._backend, "is_waiting_for_input"):
                    try:
                        waiting = bool(
                            self._backend.is_waiting_for_input(self._session_id)
                        )
                    except Exception:
                        waiting = False
                try:
                    self.query_one(".agent-prompt-banner", Static).display = waiting
                except Exception:
                    pass

                if self._follow_tail:
                    scroll.scroll_end(animate=False)
            except Exception:
                break
            await asyncio.sleep(0.2)

    @on(Input.Submitted, ".agent-input")
    async def handle_input(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if text and hasattr(self._backend, "send_input"):
            await self._backend.send_input(self._session_id, text)
        event.input.value = ""
        self._input_mode = False
        self.query_one(".agent-input", Input).display = False
        self.query_one(".agent-help", Static).update(self._default_help())


def _view_id(session_id: str) -> str:
    """Convert a tmux session_id into a Textual-safe widget id."""
    safe = "".join(c if (c.isalnum() or c == "-") else "_" for c in session_id)
    return f"agent-view-{safe}"


def agent_pane_id(session_id: str) -> str:
    """Tab pane id derived from session_id; stable across remounts."""
    safe = "".join(c if (c.isalnum() or c == "-") else "_" for c in session_id)
    return f"tab-{safe}"


class DiffView(ModalScreen[None]):
    """Scrollable git diff viewer with summary header and vim scroll keys."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("j", "scroll_down", "Down", show=True),
        Binding("k", "scroll_up", "Up", show=True),
        Binding("ctrl+d", "page_down", "Page Down", show=True),
        Binding("ctrl+u", "page_up", "Page Up", show=True),
        Binding("g", "scroll_home", "Top"),
        Binding("G", "scroll_end", "Bottom"),
    ]

    def __init__(self, diff_text: str, title: str = "Diff") -> None:
        super().__init__()
        self._diff = diff_text
        self._title = title

    def _summary(self) -> str:
        if not self._diff:
            return f"[dim]{self._title}  ·  no changes[/]"
        files = 0
        adds = 0
        dels = 0
        for line in self._diff.splitlines():
            if line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
                files += 1
            elif line.startswith("+") and not line.startswith("+++"):
                adds += 1
            elif line.startswith("-") and not line.startswith("---"):
                dels += 1
        plural = "" if files == 1 else "s"
        return (
            f"[dim]{self._title}[/]  ·  "
            f"[#4ade80]+{adds}[/] [#f87171]−{dels}[/] "
            f"[dim]in {files} file{plural}[/]"
        )

    def compose(self) -> ComposeResult:
        from rich.syntax import Syntax

        with Vertical(id="diff-view"):
            yield Static(self._summary(), id="diff-header")
            if self._diff:
                body = Syntax(
                    self._diff,
                    "diff",
                    theme="ansi_dark",
                    line_numbers=False,
                    word_wrap=False,
                    background_color="default",
                )
                yield VerticalScroll(Static(body, id="diff-content"), id="diff-scroll")
            else:
                yield VerticalScroll(
                    Static("[dim]No changes.[/]", id="diff-content"),
                    id="diff-scroll",
                )

    def _scroll(self):
        return self.query_one("#diff-scroll", VerticalScroll)

    def action_scroll_down(self) -> None:
        self._scroll().scroll_down(animate=False)

    def action_scroll_up(self) -> None:
        self._scroll().scroll_up(animate=False)

    def action_page_down(self) -> None:
        self._scroll().scroll_page_down(animate=False)

    def action_page_up(self) -> None:
        self._scroll().scroll_page_up(animate=False)

    def action_scroll_home(self) -> None:
        self._scroll().scroll_home(animate=False)

    def action_scroll_end(self) -> None:
        self._scroll().scroll_end(animate=False)

    def action_close(self) -> None:
        self.dismiss(None)
