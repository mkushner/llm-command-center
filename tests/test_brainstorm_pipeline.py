"""Tests for brainstorm pipeline logic."""

import pytest
from unittest.mock import AsyncMock, MagicMock

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
