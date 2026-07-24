"""Main Textual application: screen routing, lifecycle."""

from __future__ import annotations

import time
from pathlib import Path

from textual.app import App

from llm_cc.agents import AgentRegistry, is_clean_exit_mode
from llm_cc.git import GitWorkspace
from llm_cc.pipeline import PipelineEngine
from llm_cc.statusline import setup_statusline
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
        self.pipeline = PipelineEngine(self._config, self.registry, self.git, self.storage)
        setup_statusline(self.project_path)
        # Toast dedupe: drop identical messages emitted within 1.5s
        self._recent_notifies: dict[str, float] = {}

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: str = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        # Errors bypass dedupe — the user must see every failure.
        if severity != "error":
            key = f"{severity}:{timeout}:{message}"
            now = time.monotonic()
            last = self._recent_notifies.get(key)
            if last is not None and now - last < 1.5:
                return
            self._recent_notifies[key] = now
            if len(self._recent_notifies) > 64:
                cutoff = now - 5.0
                self._recent_notifies = {
                    k: t for k, t in self._recent_notifies.items() if t >= cutoff
                }
        super().notify(
            message,
            title=title,
            severity=severity,  # type: ignore[arg-type]
            timeout=timeout,
            markup=markup,
        )

    async def on_mount(self) -> None:
        reattached, orphans = await self.registry.reattach_existing(self.project_path)
        if reattached:
            self.notify(f"Reattached to {reattached} agent session(s)", timeout=3)
        if orphans:
            self.log(f"orphan tmux sessions (no matching task): {orphans}")
        self.push_screen(BoardScreen(self.storage, self.pipeline, self.registry))

    async def on_unmount(self) -> None:
        if self.registry.session_store:
            self.registry.session_store.flush_all()
        if is_clean_exit_mode():
            await self.registry.cleanup_all()
        else:
            await self.registry.detach_all()
