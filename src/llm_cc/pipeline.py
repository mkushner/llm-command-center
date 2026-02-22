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

    def _resolve_plan_path(self, task: Task) -> Path:
        """Resolve plan directory from config template + task fields.

        Template variables: {id}, {slug}, {branch}, {title}
        Returns absolute path to the plan file.
        """
        template = self.config.project.plan_dir
        plan_file = self.config.project.plan_file

        if "{branch}" in template and not task.branch_name:
            raise RuntimeError(
                f"plan_dir uses {{branch}} but task has no branch. "
                f"Set git.mode to 'worktree' or 'branch' in config."
            )

        try:
            plan_dir = template.format(
                id=task.id,
                slug=task.slug(),
                branch=task.branch_name or "",
                title=task.title,
            )
        except KeyError as e:
            raise RuntimeError(f"Unknown variable in plan_dir template: {e}")

        if not plan_dir:
            raise RuntimeError("plan_dir resolved to empty string")

        full_dir = (self.git.project_path / plan_dir).resolve()
        if not full_dir.is_relative_to(self.git.project_path.resolve()):
            raise RuntimeError(
                f"plan_dir resolved outside project: {full_dir}"
            )
        full_dir.mkdir(parents=True, exist_ok=True)
        return full_dir / plan_file

    async def advance(self, task: Task) -> Task:
        """Move task to next stage. Orchestrates agents and git."""
        next_status = self._next_status(task.status)
        if next_status is None:
            return task  # already at DONE

        # Stop current agent if running
        if task.session_id:
            # Capture output to file when leaving a stage
            if task.status in (TaskStatus.PLANNING, TaskStatus.REVIEW):
                await self._save_stage_output(task, task.status.value)
            await self._stop_current_agent(task)

        stage = self.config.stage_config(next_status)
        agent_config = self.config.agent_for_stage(next_status, task)
        flags = stage.cli_flags if stage else ""
        docs = self._ensure_task_docs(task)

        match next_status:
            case TaskStatus.PLANNING:
                await self.git.setup(task)  # sets task.branch_name
                plan_path = self._resolve_plan_path(task)
                prompt = self._build_planning_prompt(task, docs, plan_path)
                backend = self.agents.backend_for(agent_config.name, stage.mode_override if stage else None)
                task.session_id = await backend.start(agent_config, task, prompt, self.git.project_path, stage="planning", cli_flags=flags)

            case TaskStatus.EXECUTE:
                # No directory isolation — only one task in Execute at a time
                if self.config.project.git.mode in (GitMode.NONE, GitMode.BRANCH):
                    store = self.storage.load_tasks()
                    occupied = [t for t in store.by_status(TaskStatus.EXECUTE) if t.id != task.id]
                    if occupied:
                        raise RuntimeError(f"Execute slot occupied by: {occupied[0].title}")

                if not self._has_planning_stage():
                    await self.git.setup(task)  # git setup here when no planning stage
                plan_path = self._resolve_plan_path(task)
                prompt = self._build_execute_prompt(task, docs, plan_path)
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

                plan_path = self._resolve_plan_path(task)
                prompt = self._build_review_prompt(task, docs, plan_path)
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
        """Move task back one stage, skipping unconfigured stages."""
        prev = self._prev_status(task.status)
        if prev is None:
            return task  # already at BACKLOG

        # Capture output before stopping
        if task.session_id:
            await self._save_stage_output(task, task.status.value)
            await self._stop_current_agent(task)

        task.status = prev
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
        plan_path = self._resolve_plan_path(task)
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

        match task.status:
            case TaskStatus.PLANNING:
                prompt = self._build_planning_prompt(task, docs, plan_path)
            case TaskStatus.EXECUTE:
                prompt = self._build_execute_prompt(task, docs, plan_path)
            case TaskStatus.REVIEW:
                prompt = self._build_review_prompt(task, docs, plan_path)

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

    def _plan_rel(self, plan_path: Path) -> str:
        """Get plan file path relative to project root."""
        return str(plan_path.relative_to(self.git.project_path))

    def _docs_rel(self, docs: Path) -> str:
        """Get docs dir path relative to project root."""
        return str(docs.relative_to(self.git.project_path))

    def _build_planning_prompt(self, task: Task, docs: Path, plan_path: Path) -> str:
        docs_rel = self._docs_rel(docs)
        plan_rel = self._plan_rel(plan_path)
        return (
            f"PLANNING: {task.title}\n\n"
            f"Task description: {docs_rel}/task.md\n"
            f"Write your plan to: {plan_rel}\n\n"
            f"When finished, say: PLANNING COMPLETE"
        )

    def _build_execute_prompt(self, task: Task, docs: Path, plan_path: Path) -> str:
        docs_rel = self._docs_rel(docs)
        plan_rel = self._plan_rel(plan_path)
        return (
            f"EXECUTE: {task.title}\n\n"
            f"Task description: {docs_rel}/task.md\n"
            f"Plan: {plan_rel}\n\n"
            f"When finished, say: EXECUTE COMPLETE"
        )

    def _build_review_prompt(self, task: Task, docs: Path, plan_path: Path) -> str:
        docs_rel = self._docs_rel(docs)
        plan_rel = self._plan_rel(plan_path)
        review_hint = ""
        if self.config.project.review_file:
            review_path = plan_path.parent / self.config.project.review_file
            review_rel = str(review_path.relative_to(self.git.project_path))
            review_hint = f"Write your review to: {review_rel}\n"
        return (
            f"REVIEW: {task.title}\n\n"
            f"Task description: {docs_rel}/task.md\n"
            f"Plan: {plan_rel}\n"
            f"Diff: {docs_rel}/diff.md\n"
            f"{review_hint}\n"
            f"When finished, say: REVIEW COMPLETE"
        )

    def _has_planning_stage(self) -> bool:
        return TaskStatus.PLANNING in self._configured_stages()

    def _configured_stages(self) -> set[TaskStatus]:
        return {s.stage for s in self.config.pipeline}

    def _is_active(self, status: TaskStatus) -> bool:
        """True if this stage is in the pipeline (or is BACKLOG/DONE)."""
        return status in (TaskStatus.BACKLOG, TaskStatus.DONE) or status in self._configured_stages()

    def _next_status(self, current: TaskStatus) -> TaskStatus | None:
        """Next stage, skipping stages not in [[pipeline]] config.

        BACKLOG and DONE are always present. PLANNING, EXECUTE, REVIEW
        only appear if they have a [[pipeline]] entry.
        """
        idx = STAGE_ORDER.index(current)
        for i in range(idx + 1, len(STAGE_ORDER)):
            if self._is_active(STAGE_ORDER[i]):
                return STAGE_ORDER[i]
        return None

    def _prev_status(self, current: TaskStatus) -> TaskStatus | None:
        """Previous stage, skipping unconfigured stages."""
        idx = STAGE_ORDER.index(current)
        for i in range(idx - 1, -1, -1):
            if self._is_active(STAGE_ORDER[i]):
                return STAGE_ORDER[i]
        return None
