"""Pipeline engine: stage transitions, agent handoffs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from .agents import STAGE_COMPLETE_MARKER, AgentRegistry
from .git import GitWorkspace
from .log import logger
from .models import (
    STAGE_ORDER,
    AgentConfig,
    GitMode,
    MergedConfig,
    PipelineStage,
    Task,
    TaskStatus,
)
from .storage import Storage

# Stages that run an agent, and can therefore be restarted.
_AGENT_STAGES = (TaskStatus.PLANNING, TaskStatus.EXECUTE, TaskStatus.REVIEW)

# How we ask an agent to declare a stage finished. Detection is whole-line
# (see `agents.STAGE_COMPLETE_MARKER`), so this line must keep text alongside the
# marker — that is precisely what stops our own instruction from being read back
# as a completion. Keep it short so a narrow pane can't wrap the marker onto a
# line by itself.
_DONE_INSTRUCTION = f"When done, output: {STAGE_COMPLETE_MARKER}"


async def _compress_log(log_text: str, task_title: str, stage: str) -> str:
    """Compress a PTY session log into a structured summary using Haiku.

    Returns markdown summary, or empty string if anthropic SDK unavailable.
    """
    try:
        import anthropic
    except ImportError:
        return ""

    # Truncate to last ~100k chars to stay within Haiku context
    max_chars = 100_000
    if len(log_text) > max_chars:
        log_text = log_text[-max_chars:]

    prompt = (
        f"Summarize this coding agent session log for task: {task_title} (stage: {stage})\n\n"
        "Produce a structured markdown summary:\n"
        "## Completed Work\nWhat was accomplished (files modified, features implemented).\n"
        "## Current State\nWhere the agent left off, what's partially done.\n"
        "## Key Decisions\nImportant implementation decisions made.\n"
        "## Errors Encountered\nErrors hit and resolution status.\n"
        "## Next Steps\nWhat should be done next.\n\n"
        "Be concise but preserve file paths, function names, and error messages.\n\n"
        "--- SESSION LOG ---\n" + log_text
    )

    client = anthropic.AsyncAnthropic()
    msg = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    for block in msg.content:
        if hasattr(block, "text"):
            return str(block.text)
    return ""


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
        # Parked brainstorm sessions: task_id → {sub_agent_idx: session_id}
        self._brainstorm_sessions: dict[str, dict[int, str]] = {}

    @staticmethod
    def _merge_stage_tools(
        agent_config: AgentConfig, stage: PipelineStage | None
    ) -> AgentConfig:
        """Layer a stage's extra allowed_tools onto the agent's own.

        Accumulates rather than replaces, and preserves order while dropping
        duplicates — same contract as `storage._merge_agents`.
        """
        if not stage or not stage.allowed_tools:
            return agent_config
        merged = list(dict.fromkeys(agent_config.allowed_tools + stage.allowed_tools))
        return agent_config.model_copy(update={"allowed_tools": merged})

    def _agent_config_for_stage(self, status: TaskStatus, task: Task) -> AgentConfig:
        """Resolve agent config with per-stage allowed_tools merged in."""
        return self._merge_stage_tools(
            self.config.agent_for_stage(status, task), self.config.stage_config(status)
        )

    def _task_cwd(self, task: Task) -> Path:
        """Working directory for agent: worktree path if available, else project root."""
        if task.worktree_path:
            wt = Path(task.worktree_path)
            if wt.exists():
                return wt
        return self.git.project_path

    def _in_worktree(self, task: Task) -> bool:
        """True if this task runs inside a worktree (separate from project root)."""
        if task.worktree_path:
            wt = Path(task.worktree_path)
            return wt.exists() and wt != self.git.project_path
        return False

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
                "plan_dir uses {branch} but task has no branch. "
                "Set git.mode to 'worktree' or 'branch' in config."
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

        # Plan dir resolves against project root (default is .llm-cc/tasks/{id}).
        # Worktree agents get absolute paths to reach it.
        full_dir = (self.git.project_path / plan_dir).resolve()
        if not full_dir.is_relative_to(self.git.project_path.resolve()):
            raise RuntimeError(
                f"plan_dir resolved outside project: {full_dir}"
            )
        full_dir.mkdir(parents=True, exist_ok=True)
        return full_dir / plan_file

    def _can_resume(self, task: Task, next_status: TaskStatus) -> bool:
        """Check if current session can be reused for the next stage.

        Conditions: same agent, same backend mode, same cli_flags,
        same allowed_tools, process alive, not brainstorm, not DONE.
        """
        if not task.session_id:
            return False
        if next_status == TaskStatus.DONE:
            return False

        current_stage = self.config.stage_config(task.status)
        next_stage = self.config.stage_config(next_status)

        # Both must be non-brainstorm
        if current_stage and current_stage.is_brainstorm:
            return False
        if next_stage and next_stage.is_brainstorm:
            return False

        # Resolve agents for both stages
        current_agent = self.config.agent_for_stage(task.status, task)
        next_agent = self.config.agent_for_stage(next_status, task)

        # Same agent name
        if current_agent.name != next_agent.name:
            return False

        # Same effective mode
        cur_override = current_stage.mode_override if current_stage else None
        nxt_override = next_stage.mode_override if next_stage else None
        current_mode = cur_override or current_agent.mode
        next_mode = nxt_override or next_agent.mode
        if current_mode != next_mode:
            return False

        # Same cli_flags
        current_flags = current_stage.effective_cli_flags if current_stage else ""
        next_flags = next_stage.effective_cli_flags if next_stage else ""
        if current_flags != next_flags:
            return False

        # Same effective allowed_tools
        cur_extra = current_stage.allowed_tools if current_stage else []
        nxt_extra = next_stage.allowed_tools if next_stage else []
        current_tools = sorted(set(current_agent.allowed_tools + cur_extra))
        next_tools = sorted(set(next_agent.allowed_tools + nxt_extra))
        if current_tools != next_tools:
            return False

        # Process must be alive
        try:
            backend = self.agents.backend_for(
                current_agent.name,
                current_stage.mode_override if current_stage else None,
            )
            if not backend.is_alive(task.session_id):
                return False
        except Exception:
            return False

        return True

    def _build_resume_prompt(self, task: Task, next_status: TaskStatus, docs: Path) -> str:
        """Build a slim prompt for session resume (same agent, new stage)."""
        plan_path = self._resolve_plan_path(task)
        plan_rel = self._plan_path_for_prompt(plan_path, task)

        match next_status:
            case TaskStatus.EXECUTE:
                lines = [
                    f"EXECUTE: {task.title}",
                    "",
                    "Continue in the same session. You already have the plan in context.",
                    f"Plan: {plan_rel}",
                ]
                if task.verify:
                    lines.append(f"Verify: {task.verify}")
                if task.done:
                    lines.append(f"Done when: {task.done}")
                lines.extend(["", _DONE_INSTRUCTION])

            case TaskStatus.REVIEW:
                lines = [
                    f"REVIEW: {task.title}",
                    "",
                    "Continue in the same session. Review the work from the previous stage.",
                    f"Plan: {plan_rel}",
                ]
                if task.verify:
                    lines.append(f"Verify: {task.verify}")
                if task.done:
                    lines.append(f"Done when: {task.done}")
                lines.extend(["", _DONE_INSTRUCTION])

            case _:
                lines = [
                    f"{next_status.value.upper()}: {task.title}",
                    _DONE_INSTRUCTION,
                ]

        return "\n".join(lines)

    def _shares_root_checkout(self, task: Task) -> bool:
        """True if this task works in the project root instead of its own worktree."""
        return task.one_off or self.config.project.git.mode in (GitMode.NONE, GitMode.BRANCH)

    def _require_execute_slot(self, task: Task) -> None:
        """Guard the single EXECUTE slot when nothing isolates the working tree.

        Worktree mode gives every task its own directory, so concurrent execution
        is fine there; NONE, BRANCH and one-off tasks share one checkout and must
        not overlap. Only root-sharing tasks contend for the slot.
        """
        if not self._shares_root_checkout(task):
            return
        store = self.storage.load_tasks()
        occupied = [
            t
            for t in store.by_status(TaskStatus.EXECUTE)
            if t.id != task.id and self._shares_root_checkout(t)
        ]
        if occupied:
            raise RuntimeError(f"Execute slot occupied by: {occupied[0].title}")

    async def _start_stage_agent(
        self, task: Task, status: TaskStatus, docs: Path, extra_context: str = ""
    ) -> None:
        """Spawn the single agent for `status` and record its session on the task."""
        stage = self.config.stage_config(status)
        agent_config = self._agent_config_for_stage(status, task)
        backend = self.agents.backend_for(
            agent_config.name, stage.mode_override if stage else None
        )
        plan_path = self._resolve_plan_path(task)
        prompt = self._build_stage_prompt(task, status, docs, plan_path) + extra_context
        task.session_id = await backend.start(
            agent_config,
            task,
            prompt,
            self._task_cwd(task),
            stage=status.value,
            cli_flags=stage.effective_cli_flags if stage else "",
        )

    async def _enter_stage(self, task: Task, status: TaskStatus, docs: Path) -> None:
        """Start whatever `status` requires: a brainstorm cycle or a single agent."""
        stage = self.config.stage_config(status)
        if stage and stage.is_brainstorm:
            task.sub_agent_idx = 0
            task.loop_count = 0
            task.brainstorm_summarizing = False
            await self._spawn_brainstorm_agent(task, stage, docs)
        else:
            await self._start_stage_agent(task, status, docs)

    async def advance(self, task: Task) -> Task:
        """Move task to next stage. Orchestrates agents and git."""
        next_status = self._next_status(task.status)
        if next_status is None:
            return task  # already at DONE

        # Check if we can resume the current session for the next stage
        can_resume = self._can_resume(task, next_status)

        # Save stage output and stop current agent (unless resuming)
        prev_session_id = task.session_id
        if task.session_id:
            if task.status in (TaskStatus.PLANNING, TaskStatus.REVIEW):
                await self._save_stage_output(task, task.status.value)
            if not can_resume:
                await self._stop_current_agent(task)

        if can_resume and prev_session_id:
            # Resume existing session with slim prompt — session_id stays the same
            docs = self._ensure_task_docs(task)
            if next_status == TaskStatus.EXECUTE:
                self._require_execute_slot(task)
            prompt = self._build_resume_prompt(task, next_status, docs)
            stage_cfg = self.config.stage_config(next_status)
            agent_config = self.config.agent_for_stage(next_status, task)
            backend = self.agents.backend_for(
                agent_config.name,
                stage_cfg.mode_override if stage_cfg else None,
            )
            await backend.resume(prev_session_id, prompt)
        else:
            # Normal flow: stop was already done above, start new agent
            docs = self._ensure_task_docs(task)

            match next_status:
                case TaskStatus.PLANNING:
                    await self.git.setup(task)  # sets task.branch_name
                    await self._enter_stage(task, next_status, docs)

                case TaskStatus.EXECUTE:
                    self._require_execute_slot(task)
                    if not self._has_planning_stage():
                        await self.git.setup(task)  # git setup here when no planning stage
                    await self._enter_stage(task, next_status, docs)

                case TaskStatus.REVIEW:
                    # Compress execute log for review context
                    if prev_session_id and task.status == TaskStatus.EXECUTE:
                        await self._compress_session_for_next(
                            task, prev_session_id, "execute-summary.md",
                        )
                    await self._enter_stage(task, next_status, docs)

                case TaskStatus.DONE:
                    task.session_id = None
                    await self.git.cleanup(task)

        task.status = next_status
        task.touch()
        self.storage.save_task(task)
        return task

    async def start_one_off(self, task: Task) -> Task:
        """Open a bare agent session in the project root, straight in EXECUTE.

        A one-off has no spec, no plan and no task docs, so there is nothing for
        a stage prompt to point at: the agent starts empty and the user drives it
        from the terminal. BACKLOG and PLANNING are skipped entirely.
        """
        task.status = TaskStatus.EXECUTE
        self._require_execute_slot(task)
        await self.git.setup(task)  # no worktree — just records the checked-out branch
        stage = self.config.stage_config(TaskStatus.EXECUTE)
        agent_config = self._agent_config_for_stage(TaskStatus.EXECUTE, task)
        backend = self.agents.backend_for(
            agent_config.name, stage.mode_override if stage else None
        )
        task.session_id = await backend.start(
            agent_config,
            task,
            "",
            self._task_cwd(task),
            stage=TaskStatus.EXECUTE.value,
            cli_flags=stage.effective_cli_flags if stage else "",
        )
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

        # Clean up any parked brainstorm sessions
        await self._stop_all_brainstorm_sessions(task.id)

        # Reset brainstorm counters
        task.sub_agent_idx = 0
        task.loop_count = 0
        task.brainstorm_summarizing = False

        task.status = prev
        task.touch()
        self.storage.save_task(task)
        return task

    async def _respawn(self, task: Task, docs: Path, extra_context: str) -> Task:
        """Start a fresh agent for the task's *current* stage, and persist.

        Shared tail of `restart` and `context_restart`; they differ only in what
        continuity they append to the prompt.
        """
        await self._start_stage_agent(task, task.status, docs, extra_context)
        task.touch()
        self.storage.save_task(task)
        return task

    async def restart(self, task: Task) -> Task:
        """Re-run the current stage's agent."""
        if task.status not in _AGENT_STAGES:
            return task  # BACKLOG/DONE — nothing to restart

        # Stop current agent if running
        if task.session_id:
            await self._stop_current_agent(task)

        docs = self._ensure_task_docs(task)

        # Include handoff context if available from previous run
        extra = ""
        if (docs / "handoff.md").exists():
            docs_rel = self._docs_path_for_prompt(docs, task)
            extra = f"\n\nPrevious session handoff: {docs_rel}/handoff.md"
        return await self._respawn(task, docs, extra)

    def needs_restart(self, task: Task) -> bool:
        """True if task is in an active stage but has no living agent."""
        if task.status not in _AGENT_STAGES:
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
        """Stop the agent for the task's current stage. Generates handoff file."""
        if not task.session_id:
            return
        # Generate handoff before stopping
        if self.agents.session_store:
            ctx = self.agents.session_store.get(task.session_id)
            if ctx:
                try:
                    handoff = ctx.generate_handoff(task.title, task.verify, task.done)
                    (self._task_docs_dir(task) / "handoff.md").write_text(handoff)
                except OSError as e:
                    logger.warning("handoff write failed for %s: %s", task.id, e)
        try:
            agent_config = self.config.agent_for_stage(task.status, task)
            backend = self.agents.backend_for(agent_config.name)
            await backend.stop(task.session_id)
        except (KeyError, OSError, RuntimeError) as e:
            # Session is dropped either way — the task must not stay pinned to a
            # session we failed to kill.
            logger.warning("stopping agent for %s failed: %s", task.id, e)
        task.session_id = None

    async def _save_stage_output(self, task: Task, stage_name: str) -> None:
        """Save current agent output to docs/<stage>-output.md."""
        if not task.session_id:
            return
        try:
            agent_config = self.config.agent_for_stage(task.status, task)
            backend = self.agents.backend_for(agent_config.name)
            output = await asyncio.wait_for(
                backend.get_output(task.session_id), timeout=5.0,
            )
            if output:
                docs = self._task_docs_dir(task)
                out_file = docs / f"{stage_name}-output.md"
                out_file.write_text(output)
        except (TimeoutError, KeyError, OSError) as e:
            logger.warning("could not save %s output for %s: %s", stage_name, task.id, e)

    # --- Context Compression ---

    async def _compress_session_for_next(
        self, task: Task, session_id: str, out_name: str,
    ) -> None:
        """Compress a session's PTY log and write to task docs."""
        log_path = self.git.project_path / ".llm-cc" / "logs" / f"{session_id}.log"
        if not log_path.exists():
            return
        try:
            log_text = log_path.read_text(errors="replace")
            if not log_text.strip():
                return
            summary = await _compress_log(log_text, task.title, task.status.value)
            if summary:
                docs = self._task_docs_dir(task)
                (docs / out_name).write_text(summary)
        except Exception as e:
            # Compression is an API call — anything from network errors to auth
            # failures. Best-effort by design: the stage proceeds without it.
            logger.warning("context compression to %s failed for %s: %s", out_name, task.id, e)

    async def context_restart(self, task: Task) -> Task:
        """Restart agent due to context pressure, with compressed session summary."""
        if task.status not in _AGENT_STAGES:
            return task

        session_id = task.session_id
        if session_id:
            await self._stop_current_agent(task)

        docs = self._ensure_task_docs(task)

        # Compress log for continuity
        if session_id:
            await self._compress_session_for_next(task, session_id, "context-summary.md")

        docs_rel = self._docs_path_for_prompt(docs, task)
        extra = ""
        if (docs / "context-summary.md").exists():
            extra += f"\n\nContext summary from previous session: {docs_rel}/context-summary.md"
        if (docs / "handoff.md").exists():
            extra += f"\nSession handoff: {docs_rel}/handoff.md"
        extra += "\n\nRead the context summary first to continue where you left off."
        return await self._respawn(task, docs, extra)

    # --- Brainstorm ---

    def is_brainstorm_stage(self, task: Task) -> bool:
        """True if task is on a stage with multiple agents (brainstorm)."""
        stage = self.config.stage_config(task.status)
        return stage is not None and stage.is_brainstorm

    async def advance_sub_agent(self, task: Task) -> bool:
        """Advance to next sub-agent within a brainstorm stage.

        Returns True if brainstorm is complete (all loops + summary done).
        Parked sessions are kept alive between cycles for token savings.
        """
        stage = self.config.stage_config(task.status)
        if not stage or not stage.is_brainstorm:
            return True

        # Summarizer just finished — brainstorm complete
        if task.brainstorm_summarizing:
            await self._stop_current_agent(task)
            task.brainstorm_summarizing = False
            task.sub_agent_idx = 0
            task.loop_count = 0
            task.session_id = None
            task.touch()
            self.storage.save_task(task)
            return True

        # Save current sub-agent output
        agent_name = stage.agent_at(task.sub_agent_idx)
        await self._save_brainstorm_output(task, agent_name, task.loop_count)

        # Park current agent instead of killing
        self._park_brainstorm_session(task)

        # Advance to next sub-agent
        task.sub_agent_idx += 1
        if task.sub_agent_idx >= len(stage.agents):
            # Finished all agents in this cycle
            task.loop_count += 1
            task.sub_agent_idx = 0
            if task.loop_count >= stage.max_loops:
                # All cycles done — clean up all parked sessions
                await self._stop_all_brainstorm_sessions(task.id)
                if stage.summarizer and stage.summarizer in self.config.agents:
                    await self._spawn_brainstorm_summarizer(task, stage)
                    task.touch()
                    self.storage.save_task(task)
                    return False
                # No summarizer configured — done
                task.sub_agent_idx = 0
                task.loop_count = 0
                task.session_id = None
                task.touch()
                self.storage.save_task(task)
                return True

        # Spawn next sub-agent (may resume a parked session)
        docs = self._task_docs_dir(task)
        await self._spawn_brainstorm_agent(task, stage, docs)
        task.touch()
        self.storage.save_task(task)
        return False

    async def _spawn_brainstorm_agent(
        self, task: Task, stage: PipelineStage, docs: Path
    ) -> None:
        """Spawn the current brainstorm sub-agent, resuming a parked session if available."""
        agent_name = stage.agent_at(task.sub_agent_idx)
        agent_config = self._merge_stage_tools(self.config.agents[agent_name], stage)
        backend = self.agents.backend_for(agent_config.name, stage.mode_override)

        # Check for parked session from a previous cycle
        parked_id = self._brainstorm_sessions.get(task.id, {}).get(task.sub_agent_idx)
        if parked_id and backend.is_alive(parked_id):
            # Resume parked session with slim prompt
            prompt = self._build_brainstorm_resume_prompt(task, agent_name, stage, docs)
            await backend.resume(parked_id, prompt)
            task.session_id = parked_id
            # Remove from parked
            self._brainstorm_sessions[task.id].pop(task.sub_agent_idx, None)
            return

        # Clean up dead parked session reference if any
        if parked_id:
            self._brainstorm_sessions.get(task.id, {}).pop(task.sub_agent_idx, None)

        # Start fresh (first cycle or parked session died)
        prompt = self._build_brainstorm_prompt(task, agent_name, stage, docs)
        session_stage = f"{stage.stage.value}_{agent_name}_c{task.loop_count}"
        task.session_id = await backend.start(
            agent_config, task, prompt, self._task_cwd(task),
            stage=session_stage, cli_flags=stage.effective_cli_flags,
        )

    async def _save_brainstorm_output(self, task: Task, agent_name: str, cycle: int) -> None:
        """Save brainstorm sub-agent output to {agent}-cycle{N}.md.

        If the agent already wrote the file (preferred), skip terminal capture.
        """
        docs = self._task_docs_dir(task)
        out_file = docs / f"{agent_name}-cycle{cycle + 1}.md"
        if out_file.exists() and out_file.stat().st_size > 0:
            return  # Agent wrote it directly — clean output
        if not task.session_id:
            return
        try:
            agent_config = self.config.agent_for_stage(task.status, task)
            backend = self.agents.backend_for(agent_config.name)
            output = await asyncio.wait_for(
                backend.get_output(task.session_id), timeout=5.0,
            )
            if output:
                out_file.write_text(output)
        except (TimeoutError, KeyError, OSError) as e:
            logger.warning("could not save brainstorm output for %s: %s", task.id, e)

    def _park_brainstorm_session(self, task: Task) -> None:
        """Park the current brainstorm session instead of killing it."""
        if not task.session_id:
            return
        if task.id not in self._brainstorm_sessions:
            self._brainstorm_sessions[task.id] = {}
        self._brainstorm_sessions[task.id][task.sub_agent_idx] = task.session_id
        task.session_id = None  # clear so next spawn can set it

    async def _stop_all_brainstorm_sessions(self, task_id: str) -> None:
        """Stop all parked brainstorm sessions for a task."""
        sessions = self._brainstorm_sessions.pop(task_id, {})
        for session_id in sessions.values():
            try:
                await self.agents.stop_session(session_id)
            except (KeyError, OSError, RuntimeError) as e:
                logger.warning("could not stop parked session %s: %s", session_id, e)

    async def cleanup_task(self, task: Task) -> None:
        """Clean up all sessions for a task (on delete or full stop)."""
        if task.session_id:
            await self._stop_current_agent(task)
        await self._stop_all_brainstorm_sessions(task.id)

    def _build_brainstorm_resume_prompt(
        self, task: Task, agent_name: str, stage: PipelineStage, docs: Path,
    ) -> str:
        """Build a slim resume prompt for a parked brainstorm agent.

        The agent already has prior context — only point it at the latest
        output file from the other participant(s).
        """
        docs_rel = self._docs_path_for_prompt(docs, task)
        cycle = task.loop_count + 1  # 1-based for display
        total = stage.max_loops

        # Identify the file just written by the previous agent
        if task.sub_agent_idx > 0:
            prev_agent = stage.agent_at(task.sub_agent_idx - 1)
            prev_cycle = task.loop_count + 1  # same loop, 1-based
        else:
            # Wrapped around: last agent in list wrote in the previous loop
            prev_agent = stage.agent_at(len(stage.agents) - 1)
            prev_cycle = task.loop_count  # loop_count already incremented

        latest_file = f"{prev_agent}-cycle{prev_cycle}.md"

        lines = [
            f"Continue BRAINSTORM: {task.title}",
            f"Your role: {agent_name}",
            f"Cycle: {cycle}/{total}",
            "",
            f"Read the latest output: {docs_rel}/{latest_file}",
        ]

        out_file = f"{docs_rel}/{agent_name}-cycle{cycle}.md"
        lines.append(f"Write your analysis to: {out_file}")

        if cycle == total:
            lines.append("")
            lines.append("FINAL CYCLE. Converge on actionable conclusions.")

        return "\n".join(lines)

    async def _spawn_brainstorm_summarizer(self, task: Task, stage: PipelineStage) -> None:
        """Spawn the summarizer agent after all brainstorm loops."""
        task.brainstorm_summarizing = True
        agent_config = self._merge_stage_tools(self.config.agents[stage.summarizer], stage)
        docs = self._task_docs_dir(task)
        summary_dir = self.git.project_path / "brainstorm" / task.id
        summary_dir.mkdir(parents=True, exist_ok=True)
        prompt = self._build_summary_prompt(task, stage, docs, summary_dir)
        backend = self.agents.backend_for(agent_config.name, stage.mode_override)
        session_stage = f"{stage.stage.value}_summarizer"
        task.session_id = await backend.start(
            agent_config, task, prompt, self._task_cwd(task),
            stage=session_stage, cli_flags=stage.effective_cli_flags,
        )

    def _build_summary_prompt(
        self, task: Task, stage: PipelineStage, docs: Path, summary_dir: Path
    ) -> str:
        """Build prompt for the brainstorm summarizer."""
        docs_rel = self._docs_path_for_prompt(docs, task)
        if self._in_worktree(task):
            summary_rel = str(summary_dir)
        else:
            summary_rel = str(summary_dir.relative_to(self.git.project_path))

        cycle_files = sorted(
            f.name for f in docs.glob("*-cycle*.md")
        )

        lines = [
            f"BRAINSTORM SUMMARY: {task.title}",
            "You are the summarizer. All brainstorm cycles are complete.",
            f"Participants were: {', '.join(stage.agents)}",
            f"Total cycles: {stage.max_loops}",
            "",
            f"Task: {docs_rel}/task.md",
            "",
            "Discussion logs (read all of these):",
        ]
        for f in cycle_files:
            lines.append(f"  - {docs_rel}/{f}")

        lines.extend([
            "",
            f"Write your summary to: {summary_rel}/summary.md",
            "",
            "Your summary MUST include:",
            "1. Executive Summary — key conclusions in 3-5 bullets",
            "2. Points of Agreement — what strategist and critic converged on",
            "3. Points of Tension — unresolved disagreements or tradeoffs",
            "4. Recommended Actions — concrete next steps",
            "",
            "Also copy the discussion logs to the summary folder:",
        ])
        for f in cycle_files:
            lines.append(f"  - Copy {docs_rel}/{f} to {summary_rel}/{f}")

        return "\n".join(lines)

    def _build_brainstorm_prompt(
        self, task: Task, agent_name: str, stage: PipelineStage, docs: Path
    ) -> str:
        """Build prompt for a brainstorm sub-agent with cycle context."""
        docs_rel = self._docs_path_for_prompt(docs, task)
        cycle = task.loop_count + 1  # 1-based for display
        total = stage.max_loops

        existing = sorted(
            f.name for f in docs.glob("*.md")
            if f.name != "task.md"
        )

        lines = [
            f"BRAINSTORM: {task.title}",
            f"Your role: {agent_name}",
            f"Cycle: {cycle}/{total}",
            f"Participants: {', '.join(stage.agents)}",
            "",
            f"Task: {docs_rel}/task.md",
        ]

        if existing:
            lines.append("")
            lines.append("Previous outputs (read these files):")
            for f in existing:
                lines.append(f"  - {docs_rel}/{f}")

        if cycle == total:
            lines.append("")
            lines.append("FINAL CYCLE. Converge on actionable conclusions.")

        out_file = f"{docs_rel}/{agent_name}-cycle{cycle}.md"
        lines.append("")
        lines.append(f"Write your analysis to: {out_file}")
        lines.append(
            "Do NOT use Read tool on previous output files — use cat or just reference them."
        )
        return "\n".join(lines)

    # --- Prompt Building ---

    def _plan_path_for_prompt(self, plan_path: Path, task: Task) -> str:
        """Plan file path as the agent will see it.

        Default plan_dir is .llm-cc/tasks/{id} (in the main tree, not worktree).
        Worktree agents need absolute paths. If user configures plan_dir outside
        .llm-cc/ (e.g. plans/{branch}), it's still under project root.
        """
        if self._in_worktree(task):
            return str(plan_path)
        return str(plan_path.relative_to(self.git.project_path))

    def _docs_path_for_prompt(self, docs: Path, task: Task) -> str:
        """Docs dir path as the agent will see it.

        Task docs live in the main tree's .llm-cc/ (not in the worktree),
        so worktree agents need absolute paths to reach them.
        """
        if self._in_worktree(task):
            return str(docs)
        return str(docs.relative_to(self.git.project_path))

    def _build_stage_prompt(
        self, task: Task, status: TaskStatus, docs: Path, plan_path: Path
    ) -> str:
        """Dispatch to the prompt builder for an agent stage.

        Only ever called for `_AGENT_STAGES`; callers guard on that.
        """
        builders: dict[TaskStatus, Callable[[Task, Path, Path], str]] = {
            TaskStatus.PLANNING: self._build_planning_prompt,
            TaskStatus.EXECUTE: self._build_execute_prompt,
            TaskStatus.REVIEW: self._build_review_prompt,
        }
        return builders[status](task, docs, plan_path)

    def _build_planning_prompt(self, task: Task, docs: Path, plan_path: Path) -> str:
        docs_rel = self._docs_path_for_prompt(docs, task)
        plan_rel = self._plan_path_for_prompt(plan_path, task)
        return (
            f"PLANNING: {task.title}\n\n"
            f"Task description: {docs_rel}/task.md\n"
            f"Write your plan to: {plan_rel}\n\n"
            f"{_DONE_INSTRUCTION}"
        )

    def _build_execute_prompt(self, task: Task, docs: Path, plan_path: Path) -> str:
        docs_rel = self._docs_path_for_prompt(docs, task)
        plan_rel = self._plan_path_for_prompt(plan_path, task)
        lines = [
            f"EXECUTE: {task.title}",
            "",
            f"Task description: {docs_rel}/task.md",
            f"Plan: {plan_rel}",
        ]
        if task.verify:
            lines.append(f"Verify: {task.verify}")
        if task.done:
            lines.append(f"Done when: {task.done}")
        lines.extend(["", _DONE_INSTRUCTION])
        return "\n".join(lines)

    def _build_review_prompt(self, task: Task, docs: Path, plan_path: Path) -> str:
        docs_rel = self._docs_path_for_prompt(docs, task)
        plan_rel = self._plan_path_for_prompt(plan_path, task)
        review_hint = ""
        if self.config.project.review_file:
            review_path = plan_path.parent / self.config.project.review_file
            if self._in_worktree(task):
                review_rel = str(review_path)
            else:
                review_rel = str(review_path.relative_to(self.git.project_path))
            review_hint = f"Write your review to: {review_rel}\n"
        lines = [
            f"REVIEW: {task.title}",
            "",
            f"Task description: {docs_rel}/task.md",
            f"Plan: {plan_rel}",
        ]
        # Include execute summary if available
        summary_file = docs / "execute-summary.md"
        if summary_file.exists():
            lines.append(f"Execute summary: {docs_rel}/execute-summary.md")
        if review_hint:
            lines.append(review_hint.rstrip())
        if task.verify:
            lines.append(f"Verify: {task.verify}")
        if task.done:
            lines.append(f"Done when: {task.done}")
        lines.extend(["", _DONE_INSTRUCTION])
        return "\n".join(lines)

    def _has_planning_stage(self) -> bool:
        return TaskStatus.PLANNING in self._configured_stages()

    def _configured_stages(self) -> set[TaskStatus]:
        return {s.stage for s in self.config.pipeline}

    def _is_active(self, status: TaskStatus) -> bool:
        """True if this stage is in the pipeline (or is BACKLOG/DONE)."""
        if status in (TaskStatus.BACKLOG, TaskStatus.DONE):
            return True
        return status in self._configured_stages()

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
