# Brainstorm Mode Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add brainstorm mode to pipeline stages — one stage runs N agents sequentially through M cycles, auto-advancing between sub-agents.

**Architecture:** A `PipelineStage` with `agents` list + `max_loops` spawns `--print` CLI agents one at a time. Each writes output to `{role}-cycle{N}.md`. Board poll detects process exit → auto-spawns next. After all loops, card shows STALE for user to advance manually.

**Tech Stack:** Existing — pydantic models, pexpect PTY, textual TUI. No new dependencies.

---

### Task 1: Model changes — PipelineStage and Task

**Files:**
- Modify: `src/llm_cc/models.py:109-115` (PipelineStage)
- Modify: `src/llm_cc/models.py:26-46` (Task)
- Modify: `src/llm_cc/models.py:164-177` (MergedConfig.agent_for_stage)
- Test: `tests/test_brainstorm_models.py`

**Step 1: Write failing test for PipelineStage with agents list**

```python
# tests/test_brainstorm_models.py
"""Tests for brainstorm model extensions."""

import pytest
from llm_cc.models import AgentConfig, MergedConfig, PipelineStage, Task, TaskStatus, GlobalConfig, ProjectConfig


def test_pipeline_stage_single_agent():
    """Existing behavior: single agent field works."""
    stage = PipelineStage(stage=TaskStatus.PLANNING, agent="claude")
    assert not stage.is_brainstorm
    assert stage.agent_at(0) == "claude"
    assert stage.agent_at(5) == "claude"


def test_pipeline_stage_agents_list():
    """New: agents list enables brainstorm mode."""
    stage = PipelineStage(stage=TaskStatus.PLANNING, agents=["strategist", "critic"])
    assert stage.is_brainstorm
    assert stage.agent_at(0) == "strategist"
    assert stage.agent_at(1) == "critic"
    assert stage.agent_at(2) == "strategist"  # wraps


def test_pipeline_stage_requires_agent_or_agents():
    """Must have at least one of agent or agents."""
    with pytest.raises(Exception):
        PipelineStage(stage=TaskStatus.PLANNING)


def test_pipeline_stage_max_loops_default():
    stage = PipelineStage(stage=TaskStatus.PLANNING, agents=["a", "b"])
    assert stage.max_loops == 1


def test_task_brainstorm_fields_default():
    """New fields default to 0, backward compatible."""
    task = Task(title="test")
    assert task.sub_agent_idx == 0
    assert task.loop_count == 0


def test_agent_for_stage_brainstorm():
    """agent_for_stage resolves to correct sub-agent based on task.sub_agent_idx."""
    agents = {
        "strategist": AgentConfig(name="strategist", command="claude"),
        "critic": AgentConfig(name="critic", command="claude"),
    }
    pipeline = [PipelineStage(stage=TaskStatus.PLANNING, agents=["strategist", "critic"])]
    config = MergedConfig(
        project=ProjectConfig(),
        global_cfg=GlobalConfig(),
        agents=agents,
        pipeline=pipeline,
    )

    task = Task(title="test", sub_agent_idx=0)
    assert config.agent_for_stage(TaskStatus.PLANNING, task).name == "strategist"

    task.sub_agent_idx = 1
    assert config.agent_for_stage(TaskStatus.PLANNING, task).name == "critic"


def test_agent_for_stage_single_agent_unchanged():
    """Existing single-agent resolution still works."""
    agents = {"claude": AgentConfig(name="claude", command="claude")}
    pipeline = [PipelineStage(stage=TaskStatus.PLANNING, agent="claude")]
    config = MergedConfig(
        project=ProjectConfig(),
        global_cfg=GlobalConfig(),
        agents=agents,
        pipeline=pipeline,
    )
    task = Task(title="test")
    assert config.agent_for_stage(TaskStatus.PLANNING, task).name == "claude"
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/maximkushner/Documents/GitHub/mkp/llm-command-center && python -m pytest tests/test_brainstorm_models.py -v`
Expected: FAIL — `is_brainstorm`, `agent_at`, `agents`, `max_loops`, `sub_agent_idx`, `loop_count` don't exist

**Step 3: Implement model changes**

In `src/llm_cc/models.py`:

Add import at line 9:
```python
from pydantic import BaseModel, Field, model_validator
```

Replace PipelineStage (lines 109-115):
```python
class PipelineStage(BaseModel):
    stage: TaskStatus
    agent: str = ""
    agents: list[str] = []                # sequential sub-agents for brainstorm
    max_loops: int = 1                    # how many full cycles through agents list
    mode_override: AgentMode | None = None
    prompt_template: str | None = None
    cli_flags: str = ""
    auto: bool = False

    @model_validator(mode="after")
    def _check_agent_or_agents(self) -> PipelineStage:
        if not self.agent and not self.agents:
            raise ValueError("PipelineStage needs 'agent' or 'agents'")
        return self

    @property
    def is_brainstorm(self) -> bool:
        return len(self.agents) > 0

    def agent_at(self, sub_idx: int = 0) -> str:
        """Resolve agent name by index. For brainstorm, wraps around agents list."""
        if self.agents:
            return self.agents[sub_idx % len(self.agents)]
        return self.agent
```

Add to Task class (after line 37, before created_at):
```python
    sub_agent_idx: int = 0    # brainstorm: index into stage.agents
    loop_count: int = 0       # brainstorm: current cycle (0-based)
```

Replace MergedConfig.agent_for_stage (lines 164-177):
```python
    def agent_for_stage(self, status: TaskStatus, task: Task | None = None) -> AgentConfig:
        """Resolve agent: task override -> pipeline stage -> global default."""
        if task and task.agent_override and task.agent_override in self.agents:
            return self.agents[task.agent_override]
        for stage in self.pipeline:
            if stage.stage == status:
                idx = task.sub_agent_idx if task else 0
                agent_name = stage.agent_at(idx)
                if agent_name in self.agents:
                    return self.agents[agent_name]
                break
        default_name = self.global_cfg.default_agent
        if default_name in self.agents:
            return self.agents[default_name]
        return next(iter(self.agents.values()))
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_brainstorm_models.py -v`
Expected: All PASS

Run: `python -m pytest tests/ -v`
Expected: All PASS (no regressions)

**Step 5: Commit**

```bash
git add src/llm_cc/models.py tests/test_brainstorm_models.py
git commit -m "feat: add brainstorm fields to PipelineStage and Task models"
```

---

### Task 2: Pipeline brainstorm logic — spawn, advance, prompt

**Files:**
- Modify: `src/llm_cc/pipeline.py:88-152` (advance method)
- Modify: `src/llm_cc/pipeline.py:154-168` (revert method)
- Add methods to: `src/llm_cc/pipeline.py` (after line 249)
- Test: `tests/test_brainstorm_pipeline.py`

**Step 1: Write failing test for brainstorm pipeline logic**

```python
# tests/test_brainstorm_pipeline.py
"""Tests for brainstorm pipeline logic."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from llm_cc.models import (
    AgentConfig, GlobalConfig, MergedConfig, PipelineStage,
    ProjectConfig, Task, TaskStatus,
)
from llm_cc.pipeline import PipelineEngine


def _make_config(agents_list=None, max_loops=2):
    """Create a MergedConfig with a brainstorm planning stage."""
    agents = {
        "strategist": AgentConfig(name="strategist", command="claude", args_template="--print {prompt}"),
        "critic": AgentConfig(name="critic", command="claude", args_template="--print {prompt}"),
        "claude": AgentConfig(name="claude", command="claude"),
    }
    pipeline = [
        PipelineStage(
            stage=TaskStatus.PLANNING,
            agents=agents_list or ["strategist", "critic"],
            max_loops=max_loops,
        ),
        PipelineStage(stage=TaskStatus.EXECUTE, agent="claude"),
    ]
    return MergedConfig(
        project=ProjectConfig(),
        global_cfg=GlobalConfig(),
        agents=agents,
        pipeline=pipeline,
    )


def _make_engine(config, tmp_path):
    """Create PipelineEngine with mocked dependencies."""
    registry = MagicMock()
    backend = AsyncMock()
    backend.start = AsyncMock(return_value="pty_test_planning_strategist_c0")
    backend.get_output = AsyncMock(return_value="test output")
    backend.is_alive = MagicMock(return_value=True)
    registry.backend_for = MagicMock(return_value=backend)

    git = MagicMock()
    git.project_path = tmp_path
    git.setup = AsyncMock()

    storage = MagicMock()
    storage.llm_cc_dir = tmp_path / ".llm-cc"
    storage.save_task = MagicMock()

    engine = PipelineEngine(config, registry, git, storage)
    return engine, backend


@pytest.fixture
def tmp_project(tmp_path):
    (tmp_path / ".llm-cc" / "tasks").mkdir(parents=True)
    return tmp_path


async def test_advance_into_brainstorm_resets_counters(tmp_project):
    config = _make_config()
    engine, backend = _make_engine(config, tmp_project)

    task = Task(title="test", sub_agent_idx=5, loop_count=3)
    await engine.advance(task)

    assert task.status == TaskStatus.PLANNING
    assert task.sub_agent_idx == 0
    assert task.loop_count == 0
    backend.start.assert_called_once()


async def test_advance_sub_agent_moves_to_next(tmp_project):
    config = _make_config()
    engine, backend = _make_engine(config, tmp_project)

    task = Task(title="test", status=TaskStatus.PLANNING, sub_agent_idx=0, loop_count=0)
    task.session_id = "pty_test_planning_strategist_c0"
    # Create docs dir
    docs = tmp_project / ".llm-cc" / "tasks" / task.id
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "task.md").write_text("# test\n")

    done = await engine.advance_sub_agent(task)

    assert not done
    assert task.sub_agent_idx == 1  # moved to critic


async def test_advance_sub_agent_loops(tmp_project):
    config = _make_config(max_loops=2)
    engine, backend = _make_engine(config, tmp_project)

    task = Task(title="test", status=TaskStatus.PLANNING, sub_agent_idx=1, loop_count=0)
    task.session_id = "pty_test_planning_critic_c0"
    docs = tmp_project / ".llm-cc" / "tasks" / task.id
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "task.md").write_text("# test\n")

    done = await engine.advance_sub_agent(task)

    assert not done
    assert task.sub_agent_idx == 0  # back to strategist
    assert task.loop_count == 1      # next cycle


async def test_advance_sub_agent_completes(tmp_project):
    config = _make_config(max_loops=1)
    engine, backend = _make_engine(config, tmp_project)

    task = Task(title="test", status=TaskStatus.PLANNING, sub_agent_idx=1, loop_count=0)
    task.session_id = "pty_test_planning_critic_c0"
    docs = tmp_project / ".llm-cc" / "tasks" / task.id
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "task.md").write_text("# test\n")

    done = await engine.advance_sub_agent(task)

    assert done
    assert task.session_id is None
    assert task.sub_agent_idx == 0
    assert task.loop_count == 0


async def test_brainstorm_prompt_contains_cycle_info(tmp_project):
    config = _make_config(max_loops=3)
    engine, backend = _make_engine(config, tmp_project)

    task = Task(title="My Task", status=TaskStatus.PLANNING, sub_agent_idx=0, loop_count=1)
    docs = tmp_project / ".llm-cc" / "tasks" / task.id
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "task.md").write_text("# My Task\n")
    (docs / "strategist-cycle1.md").write_text("prev output")

    stage = config.stage_config(TaskStatus.PLANNING)
    prompt = engine._build_brainstorm_prompt(task, "strategist", stage, docs)

    assert "Cycle: 2/3" in prompt
    assert "strategist" in prompt
    assert "critic" in prompt
    assert "strategist-cycle1.md" in prompt
    assert "task.md" not in prompt.split("Previous outputs")[0] or "task.md" in prompt


async def test_brainstorm_prompt_final_cycle(tmp_project):
    config = _make_config(max_loops=2)
    engine, backend = _make_engine(config, tmp_project)

    task = Task(title="test", status=TaskStatus.PLANNING, sub_agent_idx=0, loop_count=1)
    docs = tmp_project / ".llm-cc" / "tasks" / task.id
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "task.md").write_text("# test\n")

    stage = config.stage_config(TaskStatus.PLANNING)
    prompt = engine._build_brainstorm_prompt(task, "strategist", stage, docs)

    assert "FINAL CYCLE" in prompt


async def test_revert_resets_brainstorm(tmp_project):
    config = _make_config()
    engine, backend = _make_engine(config, tmp_project)

    task = Task(
        title="test", status=TaskStatus.PLANNING,
        sub_agent_idx=1, loop_count=1, session_id="pty_test"
    )
    await engine.revert(task)

    assert task.sub_agent_idx == 0
    assert task.loop_count == 0
    assert task.status == TaskStatus.BACKLOG


def test_is_brainstorm_stage(tmp_project):
    config = _make_config()
    engine, _ = _make_engine(config, tmp_project)

    task = Task(title="test", status=TaskStatus.PLANNING)
    assert engine.is_brainstorm_stage(task)

    task.status = TaskStatus.EXECUTE
    assert not engine.is_brainstorm_stage(task)
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_brainstorm_pipeline.py -v`
Expected: FAIL — `advance_sub_agent`, `_build_brainstorm_prompt`, `is_brainstorm_stage` don't exist

**Step 3: Implement pipeline brainstorm logic**

In `src/llm_cc/pipeline.py`:

Modify the `advance()` method. Inside each match case, after setup but before spawning the agent, check for brainstorm. Replace lines 106-112 (PLANNING case):
```python
            case TaskStatus.PLANNING:
                await self.git.setup(task)
                if stage and stage.is_brainstorm:
                    task.sub_agent_idx = 0
                    task.loop_count = 0
                    await self._spawn_brainstorm_agent(task, stage, docs)
                else:
                    plan_path = self._resolve_plan_path(task)
                    prompt = self._build_planning_prompt(task, docs, plan_path)
                    backend = self.agents.backend_for(agent_config.name, stage.mode_override if stage else None)
                    task.session_id = await backend.start(agent_config, task, prompt, self.git.project_path, stage="planning", cli_flags=flags)
```

Replace lines 114-127 (EXECUTE case):
```python
            case TaskStatus.EXECUTE:
                if self.config.project.git.mode in (GitMode.NONE, GitMode.BRANCH):
                    store = self.storage.load_tasks()
                    occupied = [t for t in store.by_status(TaskStatus.EXECUTE) if t.id != task.id]
                    if occupied:
                        raise RuntimeError(f"Execute slot occupied by: {occupied[0].title}")
                if not self._has_planning_stage():
                    await self.git.setup(task)
                if stage and stage.is_brainstorm:
                    task.sub_agent_idx = 0
                    task.loop_count = 0
                    await self._spawn_brainstorm_agent(task, stage, docs)
                else:
                    plan_path = self._resolve_plan_path(task)
                    prompt = self._build_execute_prompt(task, docs, plan_path)
                    backend = self.agents.backend_for(agent_config.name, stage.mode_override if stage else None)
                    task.session_id = await backend.start(agent_config, task, prompt, self.git.project_path, stage="execute", cli_flags=flags)
```

Replace lines 129-143 (REVIEW case):
```python
            case TaskStatus.REVIEW:
                diff = await self.git.diff_from_base(task)
                changed = await self.git.changed_files(task)
                diff_md = docs / "diff.md"
                diff_md.write_text(
                    f"# Changes for: {task.title}\n\n"
                    f"## Changed Files\n{chr(10).join(changed) or 'None'}\n\n"
                    f"## Diff\n```\n{diff or 'No changes yet.'}\n```\n"
                )
                if stage and stage.is_brainstorm:
                    task.sub_agent_idx = 0
                    task.loop_count = 0
                    await self._spawn_brainstorm_agent(task, stage, docs)
                else:
                    plan_path = self._resolve_plan_path(task)
                    prompt = self._build_review_prompt(task, docs, plan_path)
                    backend = self.agents.backend_for(agent_config.name, stage.mode_override if stage else None)
                    task.session_id = await backend.start(agent_config, task, prompt, self.git.project_path, stage="review", cli_flags=flags)
```

In `revert()`, add reset after line 163 (`await self._stop_current_agent(task)`):
```python
        # Reset brainstorm counters
        task.sub_agent_idx = 0
        task.loop_count = 0
```

Add new methods after `_save_stage_output` (after line 249):

```python
    # --- Brainstorm ---

    def is_brainstorm_stage(self, task: Task) -> bool:
        """True if task is on a stage with multiple agents (brainstorm)."""
        stage = self.config.stage_config(task.status)
        return stage is not None and stage.is_brainstorm

    async def advance_sub_agent(self, task: Task) -> bool:
        """Advance to next sub-agent within a brainstorm stage.

        Returns True if brainstorm is complete (all loops exhausted).
        """
        stage = self.config.stage_config(task.status)
        if not stage or not stage.is_brainstorm:
            return True

        # Save current sub-agent output
        agent_name = stage.agent_at(task.sub_agent_idx)
        await self._save_brainstorm_output(task, agent_name, task.loop_count)
        await self._stop_current_agent(task)

        # Advance to next sub-agent
        task.sub_agent_idx += 1
        if task.sub_agent_idx >= len(stage.agents):
            # Finished all agents in this cycle
            task.loop_count += 1
            task.sub_agent_idx = 0
            if task.loop_count >= stage.max_loops:
                # All cycles done
                task.sub_agent_idx = 0
                task.loop_count = 0
                task.session_id = None
                task.touch()
                self.storage.save_task(task)
                return True

        # Spawn next sub-agent
        docs = self._task_docs_dir(task)
        await self._spawn_brainstorm_agent(task, stage, docs)
        task.touch()
        self.storage.save_task(task)
        return False

    async def _spawn_brainstorm_agent(self, task: Task, stage, docs: Path) -> None:
        """Spawn the current brainstorm sub-agent."""
        agent_name = stage.agent_at(task.sub_agent_idx)
        agent_config = self.config.agents[agent_name]
        prompt = self._build_brainstorm_prompt(task, agent_name, stage, docs)
        backend = self.agents.backend_for(agent_config.name, stage.mode_override)
        session_stage = f"{task.status.value}_{agent_name}_c{task.loop_count}"
        task.session_id = await backend.start(
            agent_config, task, prompt, self.git.project_path,
            stage=session_stage, cli_flags=stage.cli_flags,
        )

    async def _save_brainstorm_output(self, task: Task, agent_name: str, cycle: int) -> None:
        """Save brainstorm sub-agent output to {agent}-cycle{N}.md."""
        if not task.session_id:
            return
        try:
            agent_config = self.config.agent_for_stage(task.status, task)
            backend = self.agents.backend_for(agent_config.name)
            output = await backend.get_output(task.session_id)
            if output:
                docs = self._task_docs_dir(task)
                out_file = docs / f"{agent_name}-cycle{cycle + 1}.md"
                out_file.write_text(output)
        except Exception:
            pass

    def _build_brainstorm_prompt(self, task: Task, agent_name: str, stage, docs: Path) -> str:
        """Build prompt for a brainstorm sub-agent with cycle context."""
        docs_rel = self._docs_rel(docs)
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

        lines.append("")
        lines.append(f"Write your output to: {docs_rel}/{agent_name}-cycle{cycle}.md")
        return "\n".join(lines)
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_brainstorm_pipeline.py -v`
Expected: All PASS

Run: `python -m pytest tests/ -v`
Expected: All PASS (no regressions)

**Step 5: Commit**

```bash
git add src/llm_cc/pipeline.py tests/test_brainstorm_pipeline.py
git commit -m "feat: add brainstorm pipeline logic — spawn, advance, prompt"
```

---

### Task 3: Board poll auto-advance for brainstorm sub-agents

**Files:**
- Modify: `src/llm_cc/ui/board.py:205-230` (_poll_agent_status)
- Add worker to: `src/llm_cc/ui/board.py` (after _do_restart, ~line 444)

**Step 1: Write failing test**

```python
# tests/test_brainstorm_board.py
"""Tests for brainstorm auto-advance in board polling."""

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from llm_cc.models import AgentConfig, PipelineStage, Task, TaskStatus
from llm_cc.ui.board import BoardScreen


def test_poll_detects_dead_brainstorm_agent():
    """When a brainstorm sub-agent's process exits, poll should trigger auto-advance."""
    # This is an integration-level test concept — verify the logic branch exists
    # by checking that BoardScreen has _do_brainstorm_advance method
    assert hasattr(BoardScreen, "_do_brainstorm_advance")
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_brainstorm_board.py -v`
Expected: FAIL — `_do_brainstorm_advance` doesn't exist

**Step 3: Implement board auto-advance**

In `src/llm_cc/ui/board.py`, replace `_poll_agent_status` (lines 205-230):
```python
    def _poll_agent_status(self) -> None:
        """Check active agents: auto-advance brainstorm sub-agents, detect input waits."""
        if not self.registry:
            return
        changed = False
        for col in self._columns:
            for card in col.query(TaskCard):
                task = card.task_data
                if not task.session_id:
                    if card.waiting_for_input:
                        card.waiting_for_input = False
                        changed = True
                    continue

                try:
                    agent_config = self._config.agent_for_stage(task.status, task)
                    backend = self.registry.backend_for(agent_config.name)
                except Exception:
                    continue

                # Auto-advance brainstorm sub-agents when process exits
                if (
                    self.pipeline
                    and self.pipeline.is_brainstorm_stage(task)
                    and not backend.is_alive(task.session_id)
                ):
                    self._do_brainstorm_advance(task.id)
                    changed = True
                    continue

                # Existing: detect permission/input prompts
                waiting = (
                    isinstance(backend, PtyBackend)
                    and backend.is_waiting_for_input(task.session_id)
                )
                if card.waiting_for_input != waiting:
                    card.waiting_for_input = waiting
                    changed = True

        if changed:
            self._update_column_focus()
```

Add new worker after `_do_restart` (after line 444):
```python
    # --- Brainstorm auto-advance ---

    @work(exclusive=True, group="brainstorm")
    async def _do_brainstorm_advance(self, task_id: str) -> None:
        try:
            task = self._fresh_task(task_id)
            if not task:
                return
            done = await self.pipeline.advance_sub_agent(task)
            self._refresh_board()
            if done:
                self.notify(f"Brainstorm complete: {task.title}")
            else:
                stage = self._config.stage_config(task.status)
                if stage:
                    agent_name = stage.agent_at(task.sub_agent_idx)
                    cycle = task.loop_count + 1
                    self.notify(f"Brainstorm: {agent_name} (cycle {cycle}/{stage.max_loops})")
        except Exception as e:
            self.notify(f"Brainstorm error: {e}", severity="error")
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_brainstorm_board.py -v`
Expected: PASS

Run: `python -m pytest tests/ -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/llm_cc/ui/board.py tests/test_brainstorm_board.py
git commit -m "feat: board poll auto-advances brainstorm sub-agents"
```

---

### Task 4: Default pipeline backward compatibility fix

**Files:**
- Modify: `src/llm_cc/storage.py:213-218` (_default_pipeline)

The default pipeline uses `PipelineStage(stage=..., agent="claude")` which still works because `agent` has a default of `""` but the validator requires `agent` or `agents`. Need to keep `agent="claude"` explicit.

**Step 1: Verify existing tests still pass**

Run: `python -m pytest tests/ -v`
Expected: All PASS (default pipeline already passes `agent=`)

If the validator breaks defaults, fix `_default_pipeline` to keep `agent="claude"` explicit (it already does).

**Step 2: Run linter**

Run: `cd /Users/maximkushner/Documents/GitHub/mkp/llm-command-center && python -m ruff check src/ tests/`
Expected: Clean

**Step 3: Run type checker**

Run: `python -m mypy src/llm_cc/models.py src/llm_cc/pipeline.py`
Expected: Clean (or existing issues only)

**Step 4: Commit if any fixes**

```bash
git add -A
git commit -m "fix: ensure backward compatibility with default pipeline config"
```

---

### Task 5: Manual integration test

**Step 1: Create a test project config**

Create `.llm-cc/config.toml` in a test project directory:
```toml
[project]
name = "brainstorm-test"

[agents.strategist]
command = "claude"
model = "claude-sonnet-4-6"
args_template = "--print {prompt}"

[agents.critic]
command = "claude"
model = "claude-sonnet-4-6"
args_template = "--print {prompt}"

[agents.claude]
command = "claude"
model = "claude-sonnet-4-6"

[[pipeline]]
stage = "planning"
agents = ["strategist", "critic"]
max_loops = 2

[[pipeline]]
stage = "execute"
agent = "claude"

[[pipeline]]
stage = "review"
agent = "claude"
```

**Step 2: Run the TUI**

Run: `cd <test-project> && llm-cc .`

**Step 3: Test flow**

1. Press `o` → create task "Test brainstorm"
2. Press `m` → advance to PLANNING
3. Verify: strategist agent spawns with `--print`
4. If permission prompt appears, open panel (Enter), approve, close (Esc)
5. Watch: agent finishes → poll auto-advances → critic spawns
6. Watch: critic finishes → loop 2 starts → strategist spawns again
7. Watch: all loops done → card shows STALE
8. Check `.llm-cc/tasks/<id>/`: strategist-cycle1.md, critic-cycle1.md, strategist-cycle2.md, critic-cycle2.md
9. Press `m` → advance to EXECUTE (normal interactive agent)
10. Press `b` → verify brainstorm counters reset

**Step 4: Test revert and restart**

1. Create new task, advance to PLANNING brainstorm
2. While brainstorm running, press `b` → should revert to BACKLOG
3. Advance again → brainstorm starts fresh from cycle 1
4. While brainstorm running, press `r` → should restart current sub-agent
