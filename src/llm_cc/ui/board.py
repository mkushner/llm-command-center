"""Kanban board screen: columns, task cards, arrow-key navigation."""

from __future__ import annotations

import asyncio

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.events import Click
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Footer, Static, TabbedContent, TabPane

from llm_cc.agents import AgentRegistry, TmuxBackend
from llm_cc.models import MergedConfig, PipelineStage, Task, TaskStatus
from llm_cc.pipeline import PipelineEngine
from llm_cc.storage import Storage
from llm_cc.ui.panels import (
    AgentPanelView,
    ConfirmDialog,
    DiffView,
    HelpScreen,
    TaskInputDialog,
    agent_pane_id,
)

OVERVIEW_PANE_ID = "tab-overview"


ACTIVE_STAGES = {TaskStatus.PLANNING, TaskStatus.EXECUTE, TaskStatus.REVIEW}


class TaskCard(Static, can_focus=False):
    """A single task card in a kanban column."""

    class Clicked(Message):
        """Emitted when a card is clicked."""
        def __init__(self, column_idx: int, task_idx: int) -> None:
            super().__init__()
            self.column_idx = column_idx
            self.task_idx = task_idx

    def __init__(
        self,
        task_data: Task,
        column_idx: int,
        task_idx: int,
        *,
        agent_label: str = "",
        brainstorm_text: str | None = None,
    ) -> None:
        super().__init__()
        self.task_data = task_data
        self.column_idx = column_idx
        self.task_idx = task_idx
        self.agent_label = agent_label  # resolved stage agent (honors task override)
        self.brainstorm_text = brainstorm_text  # e.g. "cycle 2/3 · critic" or "summarizing"
        self.waiting_for_input = False
        self.stage_complete = False
        self.health_score: int | None = None
        self.health_color: str = "green"
        self.top_error: str | None = None
        self.context_remaining: int | None = None
        self.context_color: str | None = None
        self.total_tokens: int | None = None

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

    def _status_chip(self) -> tuple[str, str, str] | None:
        """Resolve the single status chip for this card.

        Returns (glyph, color, label) or None for idle/no-status.
        Priority: Error > Restart > Ready > Waiting > Running.
        """
        if self.top_error and self.task_data.session_id:
            return ("⚠", "#f87171", f"Error: {self.top_error}")
        if self.is_stale:
            return ("●", "#f87171", "Needs restart")
        if self.stage_complete:
            return ("●", "#fb923c", "Ready")
        if self.waiting_for_input:
            return ("●", "#fbbf24", "Waiting")
        if self.task_data.session_id:
            return ("●", "#4ade80", "Running")
        return None

    def render(self) -> str:
        t = self.task_data
        lines: list[str] = []

        chip = self._status_chip()
        # Line 1: title (+ status glyph and chip label only if there's a status)
        if chip:
            glyph, color, label = chip
            lines.append(
                f"[{color}]{glyph}[/] [bold]{t.title}[/]  [{color}]{label}[/]"
            )
        else:
            lines.append(f"[bold]{t.title}[/]")

        # Line 2: short description
        if t.description:
            d = t.description.replace("\n", " ")
            if len(d) > 50:
                d = d[:49] + "…"
            lines.append(f"[dim]{d}[/]")

        # Line 3: brainstorm cycle (only during active brainstorm)
        if self.brainstorm_text and t.session_id:
            lines.append(f"[#8b9eff]{self.brainstorm_text}[/]")

        # Line 4: telemetry (only when a live session exists)
        if t.session_id and not self.is_stale:
            parts: list[str] = []
            if self.agent_label:
                score_part = f"[dim]{self.agent_label}[/]"
            else:
                score_part = "[dim]agent[/]"
            if self.health_score is not None:
                score_part += f" [{self.health_color}]{self.health_score}[/]"
            parts.append(score_part)
            if self.context_remaining is not None:
                color = self.context_color or "green"
                bar = _progress_bar(self.context_remaining, 6)
                parts.append(f"[{color}]{bar} {self.context_remaining}%[/]")
            if self.total_tokens is not None:
                parts.append(f"[dim]{self.total_tokens / 1000:.0f}k[/]")
            lines.append("  ".join(parts))

        return "\n".join(lines)


def _progress_bar(pct: int, width: int = 6) -> str:
    """Compact 8-step gradient bar (uses left-fill partial blocks for sub-cell precision)."""
    pct = max(0, min(100, pct))
    total = width * 8
    filled = total * pct // 100
    full = filled // 8
    partial_idx = filled % 8
    partials = " ▏▎▍▌▋▊▉"
    bar = "█" * full
    if full < width:
        bar += partials[partial_idx]
        bar += "░" * max(0, width - full - 1)
    return bar


class KanbanColumn(VerticalScroll, can_focus=False):
    """A single column in the kanban board."""

    def __init__(
        self, status: TaskStatus, label: str, col_idx: int = 0,
        agent_name: str = "", model_name: str = "",
        config: MergedConfig | None = None,
        stage: PipelineStage | None = None,
    ) -> None:
        super().__init__()
        self.status = status
        self.label = label
        self.col_idx = col_idx
        self.agent_name = agent_name
        self.model_name = model_name
        self._config = config
        self._stage = stage
        self._tasks: list[Task] = []
        self._selected: int = 0

    def _resolve_agent_label(self, task: Task) -> str:
        """Stage-resolved agent name (honors task override). Empty for non-active stages."""
        if not self._config or task.status not in ACTIVE_STAGES:
            return ""
        try:
            return self._config.agent_for_stage(task.status, task).name
        except Exception:
            return ""

    def _resolve_brainstorm_text(self, task: Task) -> str | None:
        """For brainstorm stages with a live session: 'cycle X/Y · agent' or 'summarizing'."""
        if not self._stage or not self._stage.is_brainstorm or not task.session_id:
            return None
        if task.brainstorm_summarizing:
            return "summarizing"
        cycle = task.loop_count + 1
        agent_name = self._stage.agent_at(task.sub_agent_idx)
        return f"cycle {cycle}/{self._stage.max_loops} · {agent_name}"

    def compose(self) -> ComposeResult:
        yield Static(
            self._header_text(0),
            classes="column-header",
            id=f"header-{self.status.value}",
        )
        if self.agent_name or self.model_name:
            meta_parts = []
            if self.agent_name:
                meta_parts.append(self.agent_name)
            if self.model_name:
                meta_parts.append(self.model_name)
            yield Static(
                f"[dim]{' · '.join(meta_parts)}[/]",
                classes="column-meta",
            )
        if self.status in (TaskStatus.BACKLOG, TaskStatus.DONE):
            yield Static(
                self._empty_text(),
                classes="column-empty",
                id=f"empty-{self.status.value}",
            )

    def _header_text(self, count: int) -> str:
        return f"[b]{self.label.upper()}[/]   [dim]{count}[/]"

    def _empty_text(self) -> str:
        if self.status == TaskStatus.BACKLOG:
            return "[dim]no tasks yet[/]\n[dim]press [b]o[/] to add[/]"
        return "[dim]nothing shipped yet[/]"

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

    def set_tasks(self, tasks: list[Task], *, show_empty: bool = True) -> None:
        """Replace all task cards in this column.

        `show_empty=False` hides per-column placeholders (e.g., when the
        first-launch overlay is already covering empty state).
        """
        self._tasks = tasks
        for card in self.query(TaskCard):
            card.remove()
        header_widget = self.query_one(f"#header-{self.status.value}", Static)
        header_widget.update(self._header_text(len(tasks)))
        for i, task in enumerate(tasks):
            card = TaskCard(
                task,
                self.col_idx,
                i,
                agent_label=self._resolve_agent_label(task),
                brainstorm_text=self._resolve_brainstorm_text(task),
            )
            self.mount(card)
        if self._tasks:
            self._selected = min(self._selected, len(self._tasks) - 1)
        else:
            self._selected = 0
        if self.status in (TaskStatus.BACKLOG, TaskStatus.DONE):
            try:
                empty = self.query_one(f"#empty-{self.status.value}", Static)
                empty.display = show_empty and not bool(self._tasks)
            except Exception:
                pass
        self._update_selection()

    def _update_selection(self) -> None:
        for i, card in enumerate(self.query(TaskCard)):
            card.set_class(i == self._selected, "-selected")
            card.set_class(bool(card.task_data.session_id), "-has-agent")
            card.set_class(card.is_stale, "-stale")
            card.set_class(card.stage_complete, "-stage-complete")
            card.set_class(card.waiting_for_input, "-waiting")
            card.set_class(
                card.health_score is not None and card.health_score < 50,
                "-degraded",
            )
            card.set_class(
                card.context_color == "red",
                "-ctx-critical",
            )


class BoardScreen(Screen):
    """Main screen: aggregate header, tabbed layout (overview + agent tabs)."""

    BINDINGS = [
        # Kanban navigation (overview tab only)
        Binding("left", "move_left", "Left", show=True, priority=True),
        Binding("right", "move_right", "Right", show=True, priority=True),
        Binding("down", "move_down", "Down", show=True, priority=True),
        Binding("up", "move_up", "Up", show=True, priority=True),
        Binding("o", "new_task", "New Task", show=True),
        Binding("e", "edit_task", "Edit", show=True),
        Binding("enter", "open_agent", "Agent", show=True),
        Binding("m", "advance_task", "Advance", show=True),
        Binding("b", "revert_task", "Back", show=True),
        Binding("r", "restart_task", "Restart", show=True),
        Binding("s", "stop_agent", "Stop", show=True),
        Binding("d", "show_diff", "Diff", show=True),
        Binding("x", "delete_task", "Delete", show=True),
        Binding("question_mark", "show_help", "Help", show=True),
        Binding("q", "quit_app", "Quit", show=True),
        # Tab navigation (always active)
        Binding("ctrl+o", "show_overview", "Overview", show=True),
        Binding("ctrl+right", "next_tab", "Next Agent", show=True),
        Binding("ctrl+left", "prev_tab", "Prev Agent", show=True),
        Binding("ctrl+w", "close_tab", "Close Tab", show=True, priority=True),
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
        self._context_restarted: set[str] = set()  # session_ids already auto-restarted
        self._inflight_tasks: set[str] = set()  # task_ids with a queued pipeline op
        self._open_tabs: dict[str, str] = {}  # session_id -> pane_id

    def compose(self) -> ComposeResult:
        with Horizontal(id="board-header"):
            yield Static(
                f"[b #8b9eff]LCC[/]  [b]{self._config.project.name}[/]",
                id="board-brand",
            )
            yield Static("", id="board-stats")
        with TabbedContent(initial=OVERVIEW_PANE_ID, id="main-tabs"):
            with TabPane("Overview", id=OVERVIEW_PANE_ID):
                with Horizontal(id="kanban-board"):
                    for idx, status in enumerate(self._visible_stages):
                        label = self._config.label_for(status)
                        stage_cfg = self._config.stage_config(status)
                        agent_cfg = (
                            self._config.agent_for_stage(status)
                            if stage_cfg
                            else None
                        )
                        col = KanbanColumn(
                            status, label, col_idx=idx,
                            agent_name=agent_cfg.name if agent_cfg else "",
                            model_name=agent_cfg.model or "" if agent_cfg else "",
                            config=self._config,
                            stage=stage_cfg,
                        )
                        self._columns.append(col)
                        yield col
                yield Static(
                    "[b #8b9eff]Welcome to LLM Command Center[/]\n\n"
                    "[dim]Press[/] [b]o[/] [dim]to create your first task[/]\n"
                    "[dim]Press[/] [b]?[/] [dim]for keyboard shortcuts[/]",
                    id="first-launch",
                )
        yield Footer()

    @property
    def _is_overview_active(self) -> bool:
        try:
            return self.query_one(TabbedContent).active == OVERVIEW_PANE_ID
        except Exception:
            return True

    def on_mount(self) -> None:
        self._refresh_board()
        self._poll_timer = self.set_interval(2.0, self._poll_agent_status)

    def on_unmount(self) -> None:
        if self._poll_timer:
            self._poll_timer.stop()

    def _poll_agent_status(self) -> None:
        """Check active agents: auto-advance brainstorm sub-agents, detect input waits."""
        if not self.registry:
            return
        changed = False
        for col in self._columns:
            for card in col.query(TaskCard):
                task = card.task_data
                if not task.session_id:
                    if card.waiting_for_input or card.stage_complete:
                        card.waiting_for_input = False
                        card.stage_complete = False
                        changed = True
                    continue

                try:
                    agent_config = self._config.agent_for_stage(task.status, task)
                    backend = self.registry.backend_for(agent_config.name)
                except Exception:
                    continue

                # Auto-advance brainstorm sub-agents when process exits or goes idle
                if (
                    self.pipeline
                    and self.pipeline.is_brainstorm_stage(task)
                ):
                    dead = not backend.is_alive(task.session_id)
                    idle = isinstance(backend, TmuxBackend) and (
                        backend.is_waiting_for_input(task.session_id)
                        or backend.is_stage_complete(task.session_id)
                    )
                    if (dead or idle) and task.id not in self._inflight_tasks:
                        self._do_brainstorm_advance(task.id)
                        changed = True
                        continue

                # Fetch health score
                h = None
                if hasattr(backend, "health"):
                    h = backend.health(task.session_id)
                    if h is not None:
                        card.health_score = h.score
                        card.health_color = h.color
                        card.context_remaining = h.context_remaining
                        card.context_color = h.context_color
                        # Current context window token count
                        token_sum = (h.input_tokens or 0) + (h.cache_creation_tokens or 0) + (h.cache_read_tokens or 0)
                        card.total_tokens = token_sum if token_sum > 0 else None
                        # Top error from recent errors
                        if h.errors:
                            worst = max(h.errors, key=lambda e: e.severity)
                            card.top_error = worst.pattern_name
                        else:
                            card.top_error = None
                        card.refresh()
                        changed = True

                # Auto-restart on critical context pressure
                if (
                    h is not None
                    and self._config.project.context_restart_threshold > 0
                    and h.context_remaining is not None
                    and h.context_remaining <= self._config.project.context_restart_threshold
                    and task.session_id not in self._context_restarted
                    and self.pipeline
                    and not self.pipeline.is_brainstorm_stage(task)
                ):
                    self._context_restarted.add(task.session_id)
                    self.app.notify(
                        f"{task.title} — context critical ({h.context_remaining}%), restarting",
                        severity="warning",
                    )
                    self.app.bell()
                    self._do_context_restart(task.id)
                    changed = True
                    continue

                # Detect stage completion vs input waiting
                if isinstance(backend, TmuxBackend):
                    complete = backend.is_stage_complete(task.session_id)
                    waiting = backend.is_waiting_for_input(task.session_id)
                else:
                    complete = False
                    waiting = False
                if card.stage_complete != complete:
                    if complete:
                        # Auto-advance if stage.auto is enabled
                        stage_cfg = self._config.stage_config(task.status)
                        if (
                            stage_cfg and stage_cfg.auto and self.pipeline
                            and task.id not in self._inflight_tasks
                        ):
                            self.app.notify(
                                f"{task.title} — auto-advancing from {task.status.value}",
                            )
                            self._do_advance(task.id)
                            changed = True
                            continue
                        self.app.notify(f"{task.title} — stage complete", severity="warning")
                        self.app.bell()
                    card.stage_complete = complete
                    changed = True
                if card.waiting_for_input != waiting:
                    if waiting:
                        self.app.notify(f"{task.title} — waiting for input")
                        self.app.bell()
                    card.waiting_for_input = waiting
                    changed = True

        if changed:
            self._update_column_focus()

        self._sync_agent_tabs()
        self._update_aggregate_header()

    def _sync_agent_tabs(self) -> None:
        """Open tabs for active agents; close tabs whose sessions ended."""
        if not self.registry:
            return

        store = self.storage.load_tasks()
        active_sessions: dict[str, Task] = {}
        for task in store.tasks:
            if task.session_id and task.status in ACTIVE_STAGES:
                active_sessions[task.session_id] = task

        # Auto-open tabs for live agents (off by default to avoid clutter; opt-in
        # via project.auto_open_agent_tabs). Users can always open with Enter.
        if self._config.project.auto_open_agent_tabs:
            for sid, task in active_sessions.items():
                if sid not in self._open_tabs:
                    self._open_agent_tab(task, focus=False)

        # Close tabs whose sessions are gone
        for sid in list(self._open_tabs.keys()):
            if sid not in active_sessions:
                self._close_agent_tab(sid)

        # Refresh tab labels with status dots
        self._refresh_tab_labels(active_sessions)

    def _tab_label(self, task: Task, dot: str = "") -> str:
        """Compact tab label: '● #a3f Title…' or just '#a3f Title…'."""
        short_id = task.id[:3]
        title = task.title
        if len(title) > 22:
            title = title[:21] + "…"
        prefix = f"{dot} " if dot else ""
        return f"{prefix}[dim]#{short_id}[/] {title}"

    def _refresh_tab_labels(self, active_sessions: dict[str, Task]) -> None:
        for col in self._columns:
            for card in col.query(TaskCard):
                sid = card.task_data.session_id
                if not sid or sid not in self._open_tabs:
                    continue
                pane_id = self._open_tabs[sid]
                if card.top_error:
                    dot = "[red]●[/]"
                elif card.waiting_for_input or card.stage_complete:
                    dot = "[yellow]●[/]"
                else:
                    dot = "[green]●[/]"
                label = self._tab_label(card.task_data, dot=dot)
                try:
                    tabs = self.query_one(TabbedContent)
                    tab = tabs.get_tab(pane_id)
                    tab.label = label
                except Exception:
                    pass

    def _update_aggregate_header(self) -> None:
        running = waiting = errors = ready = 0
        execute_active = 0
        for col in self._columns:
            for card in col.query(TaskCard):
                if not card.task_data.session_id:
                    continue
                running += 1
                if col.status == TaskStatus.EXECUTE:
                    execute_active += 1
                if card.top_error:
                    errors += 1
                elif card.stage_complete:
                    ready += 1
                elif card.waiting_for_input:
                    waiting += 1
        branch = self._config.project.git.base_branch or "—"
        left = f"[dim]{branch}  ·  exec {execute_active}[/]"
        right = (
            f"[#4ade80]{running} running[/]  "
            f"[#fbbf24]{waiting} waiting[/]  "
            f"[#fb923c]{ready} ready[/]  "
            f"[#f87171]{errors} error[/]"
        )
        try:
            stats = self.query_one("#board-stats", Static)
            stats.update(f"{left}    {right}")
        except Exception:
            pass

    def _refresh_board(self) -> None:
        store = self.storage.load_tasks()
        is_empty = len(store.tasks) == 0
        for col in self._columns:
            # When the first-launch banner is showing, the per-column
            # "no tasks yet" hints would be redundant — hide them.
            col.set_tasks(store.by_status(col.status), show_empty=not is_empty)
        try:
            overlay = self.query_one("#first-launch", Static)
            overlay.display = is_empty
        except Exception:
            pass
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
        if not self._is_overview_active:
            return
        if self._active_col > 0:
            self._active_col -= 1
            self._update_column_focus()

    def action_move_right(self) -> None:
        if not self._is_overview_active:
            return
        if self._active_col < len(self._columns) - 1:
            self._active_col += 1
            self._update_column_focus()

    def action_move_down(self) -> None:
        if not self._is_overview_active:
            return
        col = self._active_column
        col.selected_index = col.selected_index + 1

    def action_move_up(self) -> None:
        if not self._is_overview_active:
            return
        col = self._active_column
        col.selected_index = col.selected_index - 1

    # --- Tab navigation ---

    def action_show_help(self) -> None:
        self.app.push_screen(HelpScreen())

    def action_show_overview(self) -> None:
        try:
            self.query_one(TabbedContent).active = OVERVIEW_PANE_ID
        except Exception:
            pass

    def action_next_tab(self) -> None:
        self._cycle_tab(+1)

    def action_prev_tab(self) -> None:
        self._cycle_tab(-1)

    def action_close_tab(self) -> None:
        """Close the active agent tab without stopping the underlying tmux session.

        No-op when Overview is active. Reopen any time with Enter on the task.
        """
        try:
            tabs = self.query_one(TabbedContent)
            active = tabs.active
        except Exception:
            return
        if active == OVERVIEW_PANE_ID:
            return
        # Find the session_id whose pane matches the active tab
        for sid, pane_id in list(self._open_tabs.items()):
            if pane_id == active:
                self._close_agent_tab(sid)
                return

    def _cycle_tab(self, delta: int) -> None:
        try:
            tabs = self.query_one(TabbedContent)
            pane_ids = [OVERVIEW_PANE_ID] + list(self._open_tabs.values())
            if not pane_ids:
                return
            current = tabs.active
            try:
                idx = pane_ids.index(current)
            except ValueError:
                idx = 0
            tabs.active = pane_ids[(idx + delta) % len(pane_ids)]
        except Exception:
            pass

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
        if not self._is_overview_active:
            return

        def on_result(task: Task | None) -> None:
            if task:
                self.storage.save_task(task)
                self._refresh_board()
                self.notify(f"Created: {task.title}")

        self.app.push_screen(TaskInputDialog(), on_result)

    def action_edit_task(self) -> None:
        if not self._is_overview_active:
            return
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
        if not self._is_overview_active:
            return
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
        self._inflight_tasks.add(task_id)
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
        finally:
            self._inflight_tasks.discard(task_id)

    # --- b: Back (revert) ---

    def action_revert_task(self) -> None:
        if not self._is_overview_active:
            return
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
        if not self._is_overview_active:
            return
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

    # --- Brainstorm auto-advance ---

    @work(exclusive=True, group="pipeline")
    async def _do_brainstorm_advance(self, task_id: str) -> None:
        self._inflight_tasks.add(task_id)
        try:
            task = self._fresh_task(task_id)
            if not task:
                return
            done = await self.pipeline.advance_sub_agent(task)
            self._refresh_board()
            if done:
                self.notify(f"Brainstorm complete: {task.title}")
            else:
                if task.brainstorm_summarizing:
                    self.notify(f"Brainstorm: summarizing {task.title}")
                else:
                    stage = self._config.stage_config(task.status)
                    if stage:
                        agent_name = stage.agent_at(task.sub_agent_idx)
                        cycle = task.loop_count + 1
                        self.notify(f"Brainstorm: {agent_name} (cycle {cycle}/{stage.max_loops})")
        except Exception as e:
            self.notify(f"Brainstorm error: {e}", severity="error")
        finally:
            self._inflight_tasks.discard(task_id)

    # --- Context restart ---

    @work(exclusive=True, group="pipeline")
    async def _do_context_restart(self, task_id: str) -> None:
        old_session = None
        try:
            task = self._fresh_task(task_id)
            if not task:
                return
            old_session = task.session_id
            self.notify(f"Compressing context for {task.title}...")
            updated = await self.pipeline.context_restart(task)
            self._refresh_board()
            self._follow_task(updated)
            self.notify(f"Context restart complete: {updated.title}")
        except Exception as e:
            self.notify(f"Context restart error: {e}", severity="error", timeout=15)
        finally:
            # Always clear the guard so future auto-restarts can trigger
            if old_session:
                self._context_restarted.discard(old_session)

    # --- Other actions ---

    def action_open_agent(self) -> None:
        if not self._is_overview_active:
            return
        task = self._active_column.selected_task
        if not task or not task.session_id or not self.registry:
            self.notify("No active agent session", severity="warning")
            return
        self._open_agent_tab(task)

    def _open_agent_tab(self, task: Task, focus: bool = True) -> None:
        if not task.session_id or not self.registry:
            return
        try:
            agent_config = self._config.agent_for_stage(task.status, task)
            backend = self.registry.backend_for(agent_config.name)
        except Exception as e:
            self.notify(f"Error resolving agent: {e}", severity="error")
            return

        pane_id = agent_pane_id(task.session_id)
        tabs = self.query_one(TabbedContent)
        view: AgentPanelView | None = None
        if task.session_id not in self._open_tabs:
            label = self._tab_label(task)
            view = AgentPanelView(
                task.session_id,
                backend,
                title=task.title,
                agent=agent_config.name,
                model=agent_config.model or "",
                branch=task.branch_name or "",
            )
            pane = TabPane(label, view, id=pane_id)
            tabs.add_pane(pane)
            self._open_tabs[task.session_id] = pane_id
        if focus:
            tabs.active = pane_id
            try:
                if view is None:
                    view = tabs.query_one(f"#{pane_id} AgentPanelView", AgentPanelView)
                self.call_after_refresh(view.focus)
            except Exception:
                pass

    def _close_agent_tab(self, session_id: str) -> None:
        pane_id = self._open_tabs.pop(session_id, None)
        if not pane_id:
            return
        try:
            tabs = self.query_one(TabbedContent)
            was_active = tabs.active == pane_id
            tabs.remove_pane(pane_id)
            if was_active:
                tabs.active = OVERVIEW_PANE_ID
        except Exception:
            pass

    @on(AgentPanelView.CloseRequested)
    def handle_agent_close(self, event: AgentPanelView.CloseRequested) -> None:
        try:
            self.query_one(TabbedContent).active = OVERVIEW_PANE_ID
        except Exception:
            pass
        event.stop()

    @on(TabbedContent.TabActivated)
    def handle_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Focus the agent panel when its tab becomes active (click or key)."""
        pane_id = event.pane.id if event.pane else None
        if not pane_id or pane_id == OVERVIEW_PANE_ID:
            return
        try:
            view = event.pane.query_one(AgentPanelView)
            self.call_after_refresh(view.focus)
        except Exception:
            pass

    def action_show_diff(self) -> None:
        if not self._is_overview_active:
            return
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
        if not self._is_overview_active:
            return
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
            stopped_session = task.session_id
            if task.session_id and self.registry:
                agent_config = self._config.agent_for_stage(task.status, task)
                backend = self.registry.backend_for(agent_config.name)
                await backend.stop(task.session_id)
            task.session_id = None
            task.touch()
            self.storage.save_task(task)
            if stopped_session:
                self._close_agent_tab(stopped_session)
            self._refresh_board()
            self.notify(f"Stopped agent: {task.title}")
        except Exception as e:
            self.notify(f"Error stopping agent: {e}", severity="error")

    def action_delete_task(self) -> None:
        if not self._is_overview_active:
            return
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
            stopped_session = None
            if task and task.session_id and self.registry:
                stopped_session = task.session_id
                try:
                    agent_config = self._config.agent_for_stage(task.status, task)
                    backend = self.registry.backend_for(agent_config.name)
                    await backend.stop(task.session_id)
                except Exception:
                    pass
            self.storage.delete_task(task_id)
            if stopped_session:
                self._close_agent_tab(stopped_session)
            self._refresh_board()
            self.notify(f"Deleted: {task_title}")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def action_quit_app(self) -> None:
        active = len(self._open_tabs)
        if active <= 0:
            self.app.exit()
            return

        from llm_cc.agents import is_clean_exit_mode

        if is_clean_exit_mode():
            verb = "kill"
            note = "(--clean-exit: tmux sessions will be killed)"
        else:
            verb = "leave running"
            note = "(tmux sessions persist; reattach on next start)"
        prompt = f"Quit with {active} active agent(s)?\n{note}\nAgents will {verb}."

        def _on_confirm(yes: bool | None) -> None:
            if yes:
                self.app.exit()

        self.app.push_screen(ConfirmDialog(prompt, severity="warning"), _on_confirm)
