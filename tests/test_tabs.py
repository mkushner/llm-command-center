"""Tests for M4 tabbed layout: opening/closing agent tabs."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from llm_cc.app import CommandCenterApp
from llm_cc.models import Task, TaskStatus
from llm_cc.storage import Storage


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=path, capture_output=True)
    (path / "README.md").write_text("# t\n")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path, capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t.com",
            "HOME": str(Path.home()),
            "PATH": subprocess.check_output(["bash", "-c", "echo $PATH"]).decode().strip(),
        },
    )


@pytest.fixture
def patched_tmux():
    """Pretend any session_id is alive so the UI thinks an agent is running."""
    live: set[str] = set()

    async def fake_async(*args, **kwargs):
        if not args:
            return 0, b"", b""
        if args[0] == "list-sessions":
            return 0, "\n".join(sorted(live)).encode(), b""
        return 0, b"", b""

    def fake_sync(*args, **kwargs):
        if not args:
            return 0, b""
        if args[0] == "has-session":
            try:
                idx = args.index("-t")
                return (0 if args[idx + 1] in live else 1, b"")
            except ValueError:
                return 1, b""
        return 0, b""

    with patch("llm_cc.agents._tmux", side_effect=fake_async), \
         patch("llm_cc.agents._tmux_sync", side_effect=fake_sync):
        yield live


async def test_open_agent_tab_via_enter(patched_tmux):
    """Pressing Enter on a task with an active session opens its tab."""
    from textual.widgets import TabbedContent

    from llm_cc.ui.board import OVERVIEW_PANE_ID
    from llm_cc.ui.panels import AgentPanelView, agent_pane_id

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        _init_git_repo(project)

        storage = Storage(project)
        storage.ensure_dirs()

        # Pre-seed a task pretending it has a live agent
        task = Task(
            id="aaa", title="task with agent",
            status=TaskStatus.EXECUTE,
            session_id="llmcc_aaa_execute",
        )
        storage.save_task(task)
        patched_tmux.add(task.session_id)

        app = CommandCenterApp(project_path=project)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            board = app.screen
            tabs = board.query_one(TabbedContent)

            # Move to EXECUTE column (3 right from BACKLOG)
            for _ in range(2):
                await pilot.press("]")
                await pilot.pause()
            # Skip past PLANNING; one more right to get to EXECUTE
            await pilot.press("]")
            await pilot.pause()

            # Find the column with our task
            # Active column should be EXECUTE — but to be robust, find by status
            for i, col in enumerate(board._columns):
                if col.status == TaskStatus.EXECUTE:
                    board._active_col = i
                    col._selected = 0
                    board._update_column_focus()
                    break

            await pilot.pause()
            assert tabs.active == OVERVIEW_PANE_ID, "should start on overview"

            # Press Enter to open the agent tab
            await pilot.press("enter")
            await pilot.pause()

            expected_pane = agent_pane_id(task.session_id)
            assert tabs.active == expected_pane, f"expected {expected_pane}, got {tabs.active}"
            assert task.session_id in board._open_tabs

            # Should contain an AgentPanelView
            views = board.query(AgentPanelView)
            assert len(views) == 1

            # Esc returns to overview
            await pilot.press("escape")
            await pilot.pause()
            assert tabs.active == OVERVIEW_PANE_ID

            # Ctrl+Right cycles to next tab
            await pilot.press("ctrl+right")
            await pilot.pause()
            assert tabs.active == expected_pane

            # Ctrl+O returns to overview
            await pilot.press("ctrl+o")
            await pilot.pause()
            assert tabs.active == OVERVIEW_PANE_ID

            await pilot.press("q")
            await pilot.pause()


async def test_kanban_keys_inert_on_agent_tab(patched_tmux):
    """When an agent tab is active, kanban nav keys do nothing to the column."""
    from textual.widgets import TabbedContent

    from llm_cc.ui.panels import agent_pane_id

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        _init_git_repo(project)
        storage = Storage(project)
        storage.ensure_dirs()

        task = Task(
            id="bbb", title="x", status=TaskStatus.EXECUTE,
            session_id="llmcc_bbb_execute",
        )
        storage.save_task(task)
        patched_tmux.add(task.session_id)

        app = CommandCenterApp(project_path=project)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            board = app.screen
            tabs = board.query_one(TabbedContent)

            # Open the agent tab directly
            board._open_agent_tab(task)
            await pilot.pause()
            assert tabs.active == agent_pane_id(task.session_id)

            initial_col = board._active_col
            # h/l should NOT change column when agent tab is active —
            # they get forwarded to the agent
            await pilot.press("]")
            await pilot.pause()
            assert board._active_col == initial_col

            await pilot.press("q")
            await pilot.pause()
