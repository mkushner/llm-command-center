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
            yield Label("Verify (optional):")
            yield Input(
                value=(self._editing.verify or "") if self._editing else "",
                placeholder="How to verify completion (e.g., 'curl returns 200')",
                id="task-verify",
            )
            yield Label("Done when (optional):")
            yield Input(
                value=(self._editing.done or "") if self._editing else "",
                placeholder="Definition of done (e.g., 'login works with valid/invalid creds')",
                id="task-done",
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
        verify = self.query_one("#task-verify", Input).value.strip() or None
        done = self.query_one("#task-done", Input).value.strip() or None

        if self._editing:
            self._editing.title = title
            self._editing.description = desc
            self._editing.verify = verify
            self._editing.done = done
            self._editing.touch()
            self.dismiss(self._editing)
        else:
            task = Task(title=title, description=desc, verify=verify, done=done)
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
    Shift+PageUp/PageDown: scroll output history
    Shift+End: resume auto-scroll
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
            yield Static("", id="agent-status")
            yield Static(
                "[dim]All keys go to agent  |  [bold]Ctrl+C[/] interrupt  |  [bold]Ctrl+T[/] text input  |  [bold]Shift+PgUp/PgDn[/] scroll  |  [bold]Esc[/] close[/]",
                id="agent-help",
            )
            yield Input(placeholder="Type message, press Enter to send (Esc to exit)...", id="agent-input")

    def on_mount(self) -> None:
        self.query_one("#agent-input", Input).display = False
        # Resize PTY after layout completes (on_mount fires before sizing)
        self.call_after_refresh(self._resize_pty)
        self._poll_output()

    def on_unmount(self) -> None:
        self._polling = False

    def on_resize(self, event) -> None:
        """Re-fit PTY when terminal/panel is resized."""
        self._resize_pty()

    def _resize_pty(self) -> None:
        """Resize the PTY buffer to match the agent output area."""
        try:
            scroll = self.query_one("#agent-scroll", VerticalScroll)
            cols = scroll.size.width - 2  # account for padding
            rows = scroll.size.height
            if cols > 0 and rows > 0 and hasattr(self._backend, "resize_session"):
                self._backend.resize_session(self._session_id, cols, rows)
        except Exception:
            pass

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

        # Scroll controls (Shift+PageUp/PageDown/Home/End)
        if event.key == "shift+pageup":
            self.query_one("#agent-scroll", VerticalScroll).scroll_page_up(animate=False)
            event.prevent_default()
            event.stop()
            return

        if event.key == "shift+pagedown":
            self.query_one("#agent-scroll", VerticalScroll).scroll_page_down(animate=False)
            event.prevent_default()
            event.stop()
            return

        if event.key == "shift+home":
            self.query_one("#agent-scroll", VerticalScroll).scroll_home(animate=False)
            event.prevent_default()
            event.stop()
            return

        if event.key == "shift+end":
            self.query_one("#agent-scroll", VerticalScroll).scroll_end(animate=False)
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

    def _format_status_bar(self, status_data: dict) -> str:
        """Format status data into a compact status bar string."""
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

        input_tok = usage.get("input_tokens")
        if input_tok is not None:
            parts.append(f"{input_tok / 1000:.1f}k in")

        output_tok = usage.get("output_tokens")
        if output_tok is not None:
            parts.append(f"{output_tok / 1000:.1f}k out")

        cache_create = usage.get("cache_creation_input_tokens")
        if cache_create is not None:
            parts.append(f"{cache_create / 1000:.1f}k cache wr")

        cache_read = usage.get("cache_read_input_tokens")
        if cache_read is not None:
            parts.append(f"{cache_read / 1000:.1f}k cache rd")

        return " | ".join(parts)

    @work(exclusive=True)
    async def _poll_output(self) -> None:
        """Continuously refresh agent output with Rich colors and scrollback."""
        from rich.text import Text

        output_widget = self.query_one("#agent-output", Static)
        scroll = self.query_one("#agent-scroll", VerticalScroll)
        status_widget = self.query_one("#agent-status", Static)
        while self._polling:
            try:
                # Check if user is at/near bottom BEFORE updating content
                at_bottom = scroll.max_scroll_y <= 0 or scroll.scroll_offset.y >= scroll.max_scroll_y - 2

                # Try rich output first (PtyBackend), fall back to plain text
                rich_content: Text | None = None
                history: list[Text] = []
                if hasattr(self._backend, "get_output_rich"):
                    rich_content = await self._backend.get_output_rich(self._session_id)
                    history = await self._backend.get_history_rich(self._session_id)

                if rich_content is not None:
                    # Combine history + current screen
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
                    # Fallback for API backend
                    text = await self._backend.get_output(self._session_id)
                    if text:
                        output_widget.update(text)

                # Update status bar from backend's statusline data
                status_data = None
                if hasattr(self._backend, "status_data"):
                    status_data = self._backend.status_data(self._session_id)
                if status_data:
                    status_widget.update(self._format_status_bar(status_data))

                # Only auto-scroll if user was at bottom before content update
                if at_bottom:
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
