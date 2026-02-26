"""Main Textual application: screen routing, lifecycle."""

from __future__ import annotations

from pathlib import Path

from textual.app import App

from llm_cc.agents import AgentRegistry
from llm_cc.git import GitWorkspace
from llm_cc.models import TaskStatus
from llm_cc.pipeline import PipelineEngine
from llm_cc.storage import Storage
from llm_cc.ui.board import BoardScreen


class CommandCenterApp(App):
    """LLM Command Center — Multi-agent pipeline orchestration TUI."""

    CSS_PATH = "ui/styles.tcss"
    TITLE = "LLM Command Center"

    def __init__(self, project_path: Path) -> None:
        super().__init__()
        self.project_path = project_path
        self.storage = Storage(project_path)
        self._config = self.storage.load_config()
        self.registry = AgentRegistry(
            self._config.agents,
            sessions_dir=project_path / ".llm-cc" / "sessions",
        )

        self.git = GitWorkspace(project_path, self._config.project.git)
        self.pipeline = PipelineEngine(
            self._config, self.registry, self.git, self.storage
        )

    def on_mount(self) -> None:
        self._cleanup_stale_sessions()
        self.push_screen(BoardScreen(self.storage, self.pipeline, self.registry))

    def _cleanup_stale_sessions(self) -> None:
        """Clear session_id from tasks whose processes are no longer running."""
        store = self.storage.load_tasks()
        changed = False
        for task in store.tasks:
            if task.session_id and task.status in (TaskStatus.PLANNING, TaskStatus.EXECUTE, TaskStatus.REVIEW):
                # Check if any backend recognizes this session as alive
                alive = False
                try:
                    agent_config = self._config.agent_for_stage(task.status, task)
                    backend = self.registry.backend_for(agent_config.name)
                    alive = backend.is_alive(task.session_id)
                except Exception:
                    pass
                if not alive:
                    task.session_id = None
                    changed = True
        if changed:
            self.storage.save_tasks(store)

    async def on_unmount(self) -> None:
        if self.registry.session_store:
            self.registry.session_store.flush_all()
        await self.registry.cleanup_all()
