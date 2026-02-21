"""Modal dialogs: task input, confirmation, agent panel, diff view."""

from __future__ import annotations

import asyncio

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static, TextArea

from llm_cc.models import Task


class TaskInputDialog(ModalScreen[Task | None]):
    """Create or edit a task."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, task: Task | None = None) -> None:
        super().__init__()
        self._editing = task

    def compose(self) -> ComposeResult:
        with Vertical(id="task-input-container"):
            yield Label("Title:")
            yield Input(
                value=self._editing.title if self._editing else "",
                placeholder="Task title...",
                id="task-title",
            )
            yield Label("Description (optional):")
            yield TextArea(
                (self._editing.description or "") if self._editing else "",
                id="task-desc",
            )
            yield Button("Save", variant="primary", id="save-btn")

    def on_mount(self) -> None:
        self.query_one("#task-title", Input).focus()

    @on(Button.Pressed, "#save-btn")
    def handle_save(self) -> None:
        self._do_save()

    @on(Input.Submitted, "#task-title")
    def handle_submit(self) -> None:
        self._do_save()

    def _do_save(self) -> None:
        title = self.query_one("#task-title", Input).value.strip()
        if not title:
            self.notify("Title is required", severity="error")
            return
        desc = self.query_one("#task-desc", TextArea).text.strip() or None

        if self._editing:
            self._editing.title = title
            self._editing.description = desc
            self._editing.touch()
            self.dismiss(self._editing)
        else:
            task = Task(title=title, description=desc)
            self.dismiss(task)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmDialog(ModalScreen[bool]):
    """Simple yes/no confirmation."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
    ]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-container"):
            yield Static(self._prompt)
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes", variant="error", id="confirm-yes")
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


class AgentPanel(ModalScreen[None]):
    """Live agent output viewer with raw key forwarding to PTY.

    All keys forwarded to agent (including Tab, arrows).
    Esc: close panel
    Ctrl+C: send interrupt to agent
    Ctrl+T: toggle text input mode (for typing longer messages)
    """

    def __init__(self, session_id: str, backend: object) -> None:
        super().__init__()
        self._session_id = session_id
        self._backend = backend
        self._polling = True
        self._input_mode = False

    def compose(self) -> ComposeResult:
        with Vertical(id="agent-panel"):
            yield Static(f"Agent Session: {self._session_id}", classes="column-header")
            yield VerticalScroll(Static("Loading...", id="agent-output"), id="agent-scroll")
            yield Static(
                "[dim]All keys go to agent  |  [bold]Ctrl+C[/] interrupt  |  [bold]Ctrl+T[/] text input  |  [bold]Esc[/] close[/]",
                id="agent-help",
            )
            yield Input(placeholder="Type message, press Enter to send (Esc to exit)...", id="agent-input")

    def on_mount(self) -> None:
        self.query_one("#agent-input", Input).display = False
        self._poll_output()

    def on_unmount(self) -> None:
        self._polling = False

    async def on_key(self, event) -> None:
        """Forward raw keystrokes to the PTY agent."""
        # Input mode: Textual handles the Input widget, Esc exits
        if self._input_mode:
            if event.key == "escape":
                self._input_mode = False
                self.query_one("#agent-input", Input).display = False
                event.prevent_default()
                event.stop()
            # Let all other keys reach the Input widget naturally
            return

        # Close panel
        if event.key == "escape":
            self._polling = False
            self.dismiss(None)
            event.prevent_default()
            event.stop()
            return

        # Send interrupt to agent
        if event.key == "ctrl+c":
            await self._send_raw("\x03")
            event.prevent_default()
            event.stop()
            return

        # Toggle text input mode
        if event.key == "ctrl+t":
            self._input_mode = True
            inp = self.query_one("#agent-input", Input)
            inp.display = True
            inp.focus()
            event.prevent_default()
            event.stop()
            return

        # Forward everything else to PTY
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
        """Send raw bytes to the PTY through the backend's public API."""
        if hasattr(self._backend, "send_raw"):
            await self._backend.send_raw(self._session_id, data)

    @work(exclusive=True)
    async def _poll_output(self) -> None:
        """Continuously refresh agent output."""
        output_widget = self.query_one("#agent-output", Static)
        scroll = self.query_one("#agent-scroll", VerticalScroll)
        while self._polling:
            try:
                text = await self._backend.get_output(self._session_id)
                if text:
                    output_widget.update(text)
                    scroll.scroll_end(animate=False)
            except Exception:
                break
            await asyncio.sleep(0.2)

    @on(Input.Submitted, "#agent-input")
    async def handle_input(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if text and hasattr(self._backend, "send_input"):
            await self._backend.send_input(self._session_id, text)
        event.input.value = ""
        self._input_mode = False
        self.query_one("#agent-input", Input).display = False


class DiffView(ModalScreen[None]):
    """Scrollable git diff viewer."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
    ]

    def __init__(self, diff_text: str, title: str = "Diff") -> None:
        super().__init__()
        self._diff = diff_text
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="diff-view"):
            yield Static(self._title, classes="column-header")
            yield VerticalScroll(
                Static(self._diff or "No changes.", id="diff-content"),
            )

    def action_close(self) -> None:
        self.dismiss(None)
