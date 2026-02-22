"""Kanban board screen: columns, task cards, vim-like navigation."""

from __future__ import annotations

import asyncio

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.events import Click
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Footer, Static

from llm_cc.agents import AgentRegistry, PtyBackend
from llm_cc.models import Task, TaskStatus
from llm_cc.pipeline import PipelineEngine
from llm_cc.storage import Storage
from llm_cc.ui.panels import AgentPanel, ConfirmDialog, DiffView, TaskInputDialog


ACTIVE_STAGES = {TaskStatus.PLANNING, TaskStatus.EXECUTE, TaskStatus.REVIEW}


class TaskCard(Static, can_focus=False):
    """A single task card in a kanban column."""

    class Clicked(Message):
        """Emitted when a card is clicked."""
        def __init__(self, column_idx: int, task_idx: int) -> None:
            super().__init__()
            self.column_idx = column_idx
            self.task_idx = task_idx

    def __init__(self, task_data: Task, column_idx: int, task_idx: int) -> None:
        super().__init__()
        self.task_data = task_data
        self.column_idx = column_idx
        self.task_idx = task_idx
        self.waiting_for_input = False

    def on_click(self, event: Click) -> None:
        self.post_message(self.Clicked(self.column_idx, self.task_idx))
        event.stop()

    @property
    def is_stale(self) -> bool:
        """Task is in an active stage but has no session — needs restart."""
        return (
            self.task_data.status in ACTIVE_STAGES
            and not self.task_data.session_id
        )

    def render(self) -> str:
        lines = [self.task_data.title]
        if self.task_data.description:
            desc = self.task_data.description[:60]
            if len(self.task_data.description) > 60:
                desc += "..."
            lines.append(desc)
        if self.is_stale:
            lines.append("[bold red]STALE — r restart[/]")
        elif self.waiting_for_input:
            lines.append("[bold yellow]WAITING FOR INPUT[/]")
        elif self.task_data.session_id:
            agent = self.task_data.agent_override or "agent"
            lines.append(f"[{agent}]")
        return "\n".join(lines)


class KanbanColumn(VerticalScroll, can_focus=False):
    """A single column in the kanban board."""

    def __init__(
        self, status: TaskStatus, label: str, col_idx: int = 0,
        agent_name: str = "", model_name: str = "",
    ) -> None:
        super().__init__()
        self.status = status
        self.label = label
        self.col_idx = col_idx
        self.agent_name = agent_name
        self.model_name = model_name
        self._tasks: list[Task] = []
        self._selected: int = 0

    def compose(self) -> ComposeResult:
        yield Static(self.label, classes="column-header")
        if self.agent_name:
            yield Static(f"[dim]{self.agent_name}[/]", classes="column-agent")
        if self.model_name:
            yield Static(f"[dim]{self.model_name}[/]", classes="column-model")
        yield Static("0 tasks", classes="column-count", id=f"count-{self.status.value}")

    @property
    def selected_index(self) -> int:
        return self._selected

    @selected_index.setter
    def selected_index(self, value: int) -> None:
        if self._tasks:
            self._selected = max(0, min(value, len(self._tasks) - 1))
        else:
            self._selected = 0
        self._update_selection()

    @property
    def selected_task(self) -> Task | None:
        if self._tasks and 0 <= self._selected < len(self._tasks):
            return self._tasks[self._selected]
        return None

    @property
    def task_count(self) -> int:
        return len(self._tasks)

    def set_tasks(self, tasks: list[Task]) -> None:
        """Replace all task cards in this column."""
        self._tasks = tasks
        for card in self.query(TaskCard):
            card.remove()
        count_widget = self.query_one(f"#count-{self.status.value}", Static)
        count_widget.update(f"{len(tasks)} task{'s' if len(tasks) != 1 else ''}")
        for i, task in enumerate(tasks):
            card = TaskCard(task, self.col_idx, i)
            self.mount(card)
        if self._tasks:
            self._selected = min(self._selected, len(self._tasks) - 1)
        else:
            self._selected = 0
        self._update_selection()

    def _update_selection(self) -> None:
        for i, card in enumerate(self.query(TaskCard)):
            card.set_class(i == self._selected, "-selected")
            if card.task_data.session_id:
                card.set_class(True, "-has-agent")
            card.set_class(card.is_stale, "-stale")
            card.set_class(card.waiting_for_input, "-waiting")


class BoardScreen(Screen):
    """Main kanban board screen with pipeline integration."""

    BINDINGS = [
        Binding("h", "move_left", "Left", show=True),
        Binding("l", "move_right", "Right", show=True),
        Binding("j", "move_down", "Down", show=True),
        Binding("k", "move_up", "Up", show=True),
        Binding("o", "new_task", "New Task", show=True),
        Binding("e", "edit_task", "Edit", show=True),
        Binding("enter", "open_agent", "Agent", show=True),
        Binding("m", "advance_task", "Advance", show=True),
        Binding("b", "revert_task", "Back", show=True),
        Binding("r", "restart_task", "Restart", show=True),
        Binding("s", "stop_agent", "Stop", show=True),
        Binding("d", "show_diff", "Diff", show=True),
        Binding("x", "delete_task", "Delete", show=True),
        Binding("q", "quit_app", "Quit", show=True),
    ]

    def __init__(
        self,
        storage: Storage,
        pipeline: PipelineEngine | None = None,
        registry: AgentRegistry | None = None,
    ) -> None:
        super().__init__()
        self.storage = storage
        self.pipeline = pipeline
        self.registry = registry
        self._config = storage.load_config()
        self._visible_stages = self._config.active_stages()
        self._active_col: int = 0
        self._columns: list[KanbanColumn] = []
        self._poll_timer = None

    def compose(self) -> ComposeResult:
        yield Static(
            f"LLM Command Center — {self._config.project.name}",
            id="board-header",
        )
        with Horizontal(id="kanban-board"):
            for idx, status in enumerate(self._visible_stages):
                label = self._config.label_for(status)
                agent_cfg = self._config.agent_for_stage(status) if self._config.stage_config(status) else None
                col = KanbanColumn(
                    status, label, col_idx=idx,
                    agent_name=agent_cfg.name if agent_cfg else "",
                    model_name=agent_cfg.model or "" if agent_cfg else "",
                )
                self._columns.append(col)
                yield col
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_board()
        self._poll_timer = self.set_interval(2.0, self._poll_agent_status)

    def on_unmount(self) -> None:
        if self._poll_timer:
            self._poll_timer.stop()

    def _poll_agent_status(self) -> None:
        """Check if any active agents are waiting for user input."""
        if not self.registry:
            return
        changed = False
        for col in self._columns:
            for card in col.query(TaskCard):
                task = card.task_data
                if task.session_id:
                    try:
                        agent_config = self._config.agent_for_stage(task.status, task)
                        backend = self.registry.backend_for(agent_config.name)
                        waiting = (
                            isinstance(backend, PtyBackend)
                            and backend.is_waiting_for_input(task.session_id)
                        )
                    except Exception:
                        waiting = False
                    if card.waiting_for_input != waiting:
                        card.waiting_for_input = waiting
                        changed = True
                elif card.waiting_for_input:
                    card.waiting_for_input = False
                    changed = True
        if changed:
            self._update_column_focus()

    def _refresh_board(self) -> None:
        store = self.storage.load_tasks()
        for col in self._columns:
            col.set_tasks(store.by_status(col.status))
        self._update_column_focus()

    def _fresh_task(self, task_id: str) -> Task | None:
        """Re-read a task from storage to avoid stale state."""
        store = self.storage.load_tasks()
        return store.get(task_id)

    @property
    def _active_column(self) -> KanbanColumn:
        return self._columns[self._active_col]

    def _update_column_focus(self) -> None:
        for i, col in enumerate(self._columns):
            is_active = i == self._active_col
            col.set_class(is_active, "-active")
            for card in col.query(TaskCard):
                card.set_class(False, "-selected")
            if is_active:
                col._update_selection()

    def _follow_task(self, task: Task) -> None:
        """Move active column to where the task is now."""
        if task.status not in self._visible_stages:
            return
        target_col = self._visible_stages.index(task.status)
        self._active_col = target_col
        col = self._columns[target_col]
        for i, t in enumerate(col._tasks):
            if t.id == task.id:
                col._selected = i
                break
        self._update_column_focus()

    # --- Click handling ---

    @on(TaskCard.Clicked)
    def handle_card_click(self, event: TaskCard.Clicked) -> None:
        if event.column_idx < len(self._columns):
            col = self._columns[event.column_idx]
            if event.task_idx < len(col._tasks):
                self._active_col = event.column_idx
                col._selected = event.task_idx
                self._update_column_focus()

    # --- Navigation ---

    def action_move_left(self) -> None:
        if self._active_col > 0:
            self._active_col -= 1
            self._update_column_focus()

    def action_move_right(self) -> None:
        if self._active_col < len(self._columns) - 1:
            self._active_col += 1
            self._update_column_focus()

    def action_move_down(self) -> None:
        col = self._active_column
        col.selected_index = col.selected_index + 1

    def action_move_up(self) -> None:
        col = self._active_column
        col.selected_index = col.selected_index - 1

    # --- Helpers ---

    def _has_live_agent(self, task: Task) -> bool:
        if not task.session_id or not self.registry:
            return False
        try:
            agent_config = self._config.agent_for_stage(task.status, task)
            backend = self.registry.backend_for(agent_config.name)
            return backend.is_alive(task.session_id)
        except Exception:
            return False

    # --- Task Operations ---

    def action_new_task(self) -> None:
        def on_result(task: Task | None) -> None:
            if task:
                self.storage.save_task(task)
                self._refresh_board()
                self.notify(f"Created: {task.title}")

        self.app.push_screen(TaskInputDialog(), on_result)

    def action_edit_task(self) -> None:
        task = self._active_column.selected_task
        if not task:
            return

        def on_result(updated: Task | None) -> None:
            if updated:
                self.storage.save_task(updated)
                self._refresh_board()
                self.notify(f"Updated: {updated.title}")

        self.app.push_screen(TaskInputDialog(task), on_result)

    # --- m: Advance ---

    def action_advance_task(self) -> None:
        task = self._active_column.selected_task
        if not task:
            return
        if task.status == TaskStatus.DONE:
            self.notify("Already at final stage", severity="warning")
            return
        if self.pipeline:
            self._do_advance(task.id)
        else:
            vs = self._visible_stages
            idx = vs.index(task.status)
            if idx >= len(vs) - 1:
                return
            next_status = vs[idx + 1]
            task.status = next_status
            task.touch()
            self.storage.save_task(task)
            self._refresh_board()
            self._follow_task(task)
            self.notify(f"Moved to {self._config.label_for(next_status)}: {task.title}")

    @work(exclusive=True, group="pipeline")
    async def _do_advance(self, task_id: str) -> None:
        try:
            task = self._fresh_task(task_id)
            if not task:
                self.notify("Task not found", severity="error")
                return
            if task.status == TaskStatus.DONE:
                self.notify("Already at final stage", severity="warning")
                return
            self.notify(f"Advancing {task.title} from {task.status.value}...")
            updated = await self.pipeline.advance(task)
            self._refresh_board()
            self._follow_task(updated)
            self.notify(f"Moved to {self._config.label_for(updated.status)}: {updated.title}")
        except asyncio.CancelledError:
            self.notify("Advance cancelled (worker conflict)", severity="warning")
        except Exception as e:
            self.notify(f"Advance error: {e}", severity="error", timeout=15)

    # --- b: Back (revert) ---

    def action_revert_task(self) -> None:
        task = self._active_column.selected_task
        if not task:
            return
        if task.status == TaskStatus.BACKLOG:
            self.notify("Already at first stage", severity="warning")
            return
        if self.pipeline:
            self._do_revert(task.id)
        else:
            vs = self._visible_stages
            idx = vs.index(task.status)
            if idx <= 0:
                return
            prev_status = vs[idx - 1]
            task.status = prev_status
            task.touch()
            self.storage.save_task(task)
            self._refresh_board()
            self._follow_task(task)
            self.notify(f"Back to {self._config.label_for(prev_status)}: {task.title}")

    @work(exclusive=True, group="pipeline")
    async def _do_revert(self, task_id: str) -> None:
        try:
            task = self._fresh_task(task_id)
            if not task:
                self.notify("Task not found", severity="error")
                return
            updated = await self.pipeline.revert(task)
            self._refresh_board()
            self._follow_task(updated)
            self.notify(f"Back to {self._config.label_for(updated.status)}: {updated.title}")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error", timeout=15)

    # --- r: Restart ---

    def action_restart_task(self) -> None:
        task = self._active_column.selected_task
        if not task:
            return
        if task.status not in ACTIVE_STAGES:
            self.notify("Nothing to restart in this stage", severity="warning")
            return
        if self.pipeline:
            self._do_restart(task.id)

    @work(exclusive=True, group="pipeline")
    async def _do_restart(self, task_id: str) -> None:
        try:
            task = self._fresh_task(task_id)
            if not task:
                self.notify("Task not found", severity="error")
                return
            label = self._config.label_for(task.status)
            self.notify(f"Restarting {label} agent...")
            updated = await self.pipeline.restart(task)
            self._refresh_board()
            self._follow_task(updated)
            self.notify(f"Restarted {label}: {updated.title}")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error", timeout=15)

    # --- Other actions ---

    def action_open_agent(self) -> None:
        task = self._active_column.selected_task
        if not task or not task.session_id or not self.registry:
            self.notify("No active agent session", severity="warning")
            return
        try:
            agent_config = self._config.agent_for_stage(task.status, task)
            backend = self.registry.backend_for(agent_config.name)
            self.app.push_screen(AgentPanel(task.session_id, backend))
        except Exception as e:
            self.notify(f"Error opening agent: {e}", severity="error")

    def action_show_diff(self) -> None:
        task = self._active_column.selected_task
        if not task or not task.branch_name:
            self.notify("No branch for this task", severity="warning")
            return
        if self.pipeline:
            self._do_show_diff(task.id)

    @work(exclusive=True, group="diff")
    async def _do_show_diff(self, task_id: str) -> None:
        task = self._fresh_task(task_id)
        if not task:
            return
        diff = await self.pipeline.git.diff_from_base(task)
        self.app.push_screen(DiffView(diff, title=f"Diff: {task.title}"))

    def action_stop_agent(self) -> None:
        task = self._active_column.selected_task
        if not task or not self._has_live_agent(task):
            self.notify("No active agent to stop", severity="warning")
            return
        task_id = task.id

        def on_confirm(confirmed: bool) -> None:
            if confirmed:
                self._do_stop(task_id)

        self.app.push_screen(
            ConfirmDialog(f"Stop agent for '{task.title}'?"),
            on_confirm,
        )

    @work(exclusive=True, group="pipeline")
    async def _do_stop(self, task_id: str) -> None:
        try:
            task = self._fresh_task(task_id)
            if not task:
                self.notify("Task not found", severity="error")
                return
            if task.session_id and self.registry:
                agent_config = self._config.agent_for_stage(task.status, task)
                backend = self.registry.backend_for(agent_config.name)
                await backend.stop(task.session_id)
            task.session_id = None
            task.touch()
            self.storage.save_task(task)
            self._refresh_board()
            self.notify(f"Stopped agent: {task.title}")
        except Exception as e:
            self.notify(f"Error stopping agent: {e}", severity="error")

    def action_delete_task(self) -> None:
        task = self._active_column.selected_task
        if not task:
            return
        task_id = task.id
        task_title = task.title

        def on_confirm(confirmed: bool) -> None:
            if confirmed:
                self._do_delete(task_id, task_title)

        self.app.push_screen(
            ConfirmDialog(f"Delete '{task_title}'?"),
            on_confirm,
        )

    @work(exclusive=True, group="pipeline")
    async def _do_delete(self, task_id: str, task_title: str) -> None:
        try:
            task = self._fresh_task(task_id)
            if task and task.session_id and self.registry:
                try:
                    agent_config = self._config.agent_for_stage(task.status, task)
                    backend = self.registry.backend_for(agent_config.name)
                    await backend.stop(task.session_id)
                except Exception:
                    pass
            self.storage.delete_task(task_id)
            self._refresh_board()
            self.notify(f"Deleted: {task_title}")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def action_quit_app(self) -> None:
        self.app.exit()
