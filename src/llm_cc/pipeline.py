"""Pipeline engine: stage transitions, agent handoffs."""

from __future__ import annotations

from pathlib import Path

from .agents import AgentRegistry
from .git import GitWorkspace
from .models import (
    GitMode,
    MergedConfig,
    Task,
    TaskStatus,
    STAGE_ORDER,
)
from .storage import Storage


class PipelineEngine:
    """Drives tasks through pipeline stages with agent orchestration."""

    def __init__(
        self,
        config: MergedConfig,
        registry: AgentRegistry,
        git: GitWorkspace,
        storage: Storage,
    ) -> None:
        self.config = config
        self.agents = registry
        self.git = git
        self.storage = storage

    def _task_docs_dir(self, task: Task) -> Path:
        """Per-task docs directory: .llm-cc/tasks/<id>/"""
        d = self.storage.llm_cc_dir / "tasks" / task.id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _ensure_task_docs(self, task: Task) -> Path:
        """Create task docs dir, write task.md with title/description."""
        docs = self._task_docs_dir(task)
        task_md = docs / "task.md"
        if not task_md.exists():
            lines = [f"# {task.title}", ""]
            if task.description:
                lines.extend([task.description, ""])
            task_md.write_text("\n".join(lines))
        task.docs_path = str(docs)
        return docs

    async def advance(self, task: Task) -> Task:
        """Move task to next stage. Orchestrates agents and git."""
        next_status = self._next_status(task.status)
        if next_status is None:
            return task  # already at DONE

        # Stop current agent if running
        if task.session_id:
            # Capture review output to file when leaving REVIEW
            if task.status == TaskStatus.REVIEW:
                await self._save_stage_output(task, "review")
            await self._stop_current_agent(task)

        stage = self.config.stage_config(next_status)
        agent_config = self.config.agent_for_stage(next_status, task)
        flags = stage.cli_flags if stage else ""
        docs = self._ensure_task_docs(task)

        match next_status:
            case TaskStatus.PLANNING:
                await self.git.setup(task)
                prompt = self._build_prompt(task, docs, "planning")
                backend = self.agents.backend_for(agent_config.name, stage.mode_override if stage else None)
                task.session_id = await backend.start(agent_config, task, prompt, self.git.project_path, stage="planning", cli_flags=flags)

            case TaskStatus.EXECUTE:
                # No isolation — only one task in Execute at a time
                if self.config.project.git.mode == GitMode.NONE:
                    store = self.storage.load_tasks()
                    occupied = [t for t in store.by_status(TaskStatus.EXECUTE) if t.id != task.id]
                    if occupied:
                        raise RuntimeError(f"Execute slot occupied by: {occupied[0].title}")

                prompt = self._build_prompt(task, docs, "execute")
                backend = self.agents.backend_for(agent_config.name, stage.mode_override if stage else None)
                task.session_id = await backend.start(agent_config, task, prompt, self.git.project_path, stage="execute", cli_flags=flags)

            case TaskStatus.REVIEW:
                # Write diff to docs for the review agent to read
                diff = await self.git.diff_from_base(task)
                changed = await self.git.changed_files(task)
                diff_md = docs / "diff.md"
                diff_md.write_text(
                    f"# Changes for: {task.title}\n\n"
                    f"## Changed Files\n{chr(10).join(changed) or 'None'}\n\n"
                    f"## Diff\n```\n{diff or 'No changes yet.'}\n```\n"
                )

                prompt = self._build_prompt(task, docs, "review")
                backend = self.agents.backend_for(agent_config.name, stage.mode_override if stage else None)
                task.session_id = await backend.start(agent_config, task, prompt, self.git.project_path, stage="review", cli_flags=flags)

            case TaskStatus.DONE:
                task.session_id = None
                await self.git.cleanup(task)

        task.status = next_status
        task.touch()
        self.storage.save_task(task)
        return task

    async def revert(self, task: Task) -> Task:
        """Move task back one stage."""
        idx = STAGE_ORDER.index(task.status)
        if idx <= 0:
            return task  # already at BACKLOG

        # Capture output before stopping
        if task.session_id:
            await self._save_stage_output(task, task.status.value)
            await self._stop_current_agent(task)

        task.status = STAGE_ORDER[idx - 1]
        task.touch()
        self.storage.save_task(task)
        return task

    async def restart(self, task: Task) -> Task:
        """Re-run the current stage's agent."""
        if task.status not in (TaskStatus.PLANNING, TaskStatus.EXECUTE, TaskStatus.REVIEW):
            return task  # BACKLOG/DONE — nothing to restart

        # Stop current agent if running
        if task.session_id:
            await self._stop_current_agent(task)

        docs = self._ensure_task_docs(task)
        stage = self.config.stage_config(task.status)
        agent_config = self.config.agent_for_stage(task.status, task)
        backend = self.agents.backend_for(agent_config.name, stage.mode_override if stage else None)
        flags = stage.cli_flags if stage else ""

        # Re-generate diff for review restarts
        if task.status == TaskStatus.REVIEW:
            diff = await self.git.diff_from_base(task)
            changed = await self.git.changed_files(task)
            diff_md = docs / "diff.md"
            diff_md.write_text(
                f"# Changes for: {task.title}\n\n"
                f"## Changed Files\n{chr(10).join(changed) or 'None'}\n\n"
                f"## Diff\n```\n{diff or 'No changes yet.'}\n```\n"
            )

        prompt = self._build_prompt(task, docs, task.status.value)
        task.session_id = await backend.start(agent_config, task, prompt, self.git.project_path, stage=task.status.value, cli_flags=flags)

        task.touch()
        self.storage.save_task(task)
        return task

    def needs_restart(self, task: Task) -> bool:
        """True if task is in an active stage but has no living agent."""
        if task.status not in (TaskStatus.PLANNING, TaskStatus.EXECUTE, TaskStatus.REVIEW):
            return False
        if not task.session_id:
            return True
        try:
            agent_config = self.config.agent_for_stage(task.status, task)
            backend = self.agents.backend_for(agent_config.name)
            return not backend.is_alive(task.session_id)
        except Exception:
            return True

    async def _stop_current_agent(self, task: Task) -> None:
        """Stop the agent for the task's current stage."""
        if not task.session_id:
            return
        try:
            agent_config = self.config.agent_for_stage(task.status, task)
            backend = self.agents.backend_for(agent_config.name)
            await backend.stop(task.session_id)
        except Exception:
            pass
        task.session_id = None

    async def _save_stage_output(self, task: Task, stage_name: str) -> None:
        """Save current agent output to docs/<stage>-output.md."""
        if not task.session_id:
            return
        try:
            agent_config = self.config.agent_for_stage(task.status, task)
            backend = self.agents.backend_for(agent_config.name)
            output = await backend.get_output(task.session_id)
            if output:
                docs = self._task_docs_dir(task)
                out_file = docs / f"{stage_name}-output.md"
                out_file.write_text(output)
        except Exception:
            pass

    # --- Prompt Building ---

    def _build_prompt(self, task: Task, docs: Path, stage: str) -> str:
        """Minimal prompt: task + docs path + completion marker.

        All context lives in files under the task docs directory.
        The agent reads what it needs.
        """
        docs_rel = docs.relative_to(self.git.project_path)
        return (
            f"{stage.upper()}: {task.title}\n\n"
            f"Task docs: {docs_rel}/\n"
            f"Read {docs_rel}/task.md for the task description.\n\n"
            f"When finished, say: {stage.upper()} COMPLETE"
        )

    @staticmethod
    def _next_status(current: TaskStatus) -> TaskStatus | None:
        idx = STAGE_ORDER.index(current)
        return STAGE_ORDER[idx + 1] if idx < len(STAGE_ORDER) - 1 else None
