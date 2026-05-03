"""Tests for brainstorm pipeline logic + session resume."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_cc.models import (
    AgentConfig,
    GlobalConfig,
    MergedConfig,
    PipelineStage,
    ProjectConfig,
    Task,
    TaskStatus,
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


def _make_same_agent_config():
    """Config where same agent handles all stages — enables session resume."""
    agents = {
        "claude": AgentConfig(name="claude", command="claude"),
    }
    pipeline = [
        PipelineStage(stage=TaskStatus.PLANNING, agent="claude"),
        PipelineStage(stage=TaskStatus.EXECUTE, agent="claude"),
        PipelineStage(stage=TaskStatus.REVIEW, agent="claude"),
    ]
    return MergedConfig(
        project=ProjectConfig(),
        global_cfg=GlobalConfig(),
        agents=agents,
        pipeline=pipeline,
    )


def _make_diff_agent_config():
    """Config where different agents handle planning vs execute."""
    agents = {
        "claude_opus": AgentConfig(name="claude_opus", command="claude"),
        "claude_sonnet": AgentConfig(name="claude_sonnet", command="claude"),
    }
    pipeline = [
        PipelineStage(stage=TaskStatus.PLANNING, agent="claude_opus"),
        PipelineStage(stage=TaskStatus.EXECUTE, agent="claude_sonnet"),
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
    registry.stop_session = AsyncMock()
    registry.session_store = None

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


# --- Session Resume Tests ---


def test_can_resume_same_agent(tmp_project):
    """Same agent across stages + alive session → can resume."""
    config = _make_same_agent_config()
    engine, backend = _make_engine(config, tmp_project)

    task = Task(title="test", status=TaskStatus.PLANNING, session_id="pty_test_planning")
    assert engine._can_resume(task, TaskStatus.EXECUTE)


def test_cannot_resume_different_agent(tmp_project):
    """Different agents across stages → cannot resume."""
    config = _make_diff_agent_config()
    engine, backend = _make_engine(config, tmp_project)

    task = Task(title="test", status=TaskStatus.PLANNING, session_id="pty_test_planning")
    assert not engine._can_resume(task, TaskStatus.EXECUTE)


def test_cannot_resume_no_session(tmp_project):
    """No session_id → cannot resume."""
    config = _make_same_agent_config()
    engine, _ = _make_engine(config, tmp_project)

    task = Task(title="test", status=TaskStatus.PLANNING)
    assert not engine._can_resume(task, TaskStatus.EXECUTE)


def test_cannot_resume_dead_process(tmp_project):
    """Dead process → cannot resume."""
    config = _make_same_agent_config()
    engine, backend = _make_engine(config, tmp_project)
    backend.is_alive = MagicMock(return_value=False)

    task = Task(title="test", status=TaskStatus.PLANNING, session_id="pty_test_planning")
    assert not engine._can_resume(task, TaskStatus.EXECUTE)


def test_cannot_resume_to_done(tmp_project):
    """Transition to DONE → cannot resume (agent must stop)."""
    config = _make_same_agent_config()
    engine, backend = _make_engine(config, tmp_project)

    task = Task(title="test", status=TaskStatus.REVIEW, session_id="pty_test_review")
    assert not engine._can_resume(task, TaskStatus.DONE)


def test_cannot_resume_brainstorm(tmp_project):
    """Brainstorm stage → cannot use session resume (uses its own persistence)."""
    config = _make_config()
    engine, backend = _make_engine(config, tmp_project)

    task = Task(title="test", status=TaskStatus.PLANNING, session_id="pty_test")
    # PLANNING is brainstorm in this config
    assert not engine._can_resume(task, TaskStatus.EXECUTE)


def test_cannot_resume_different_flags(tmp_project):
    """Different cli_flags between stages → cannot resume."""
    agents = {
        "claude": AgentConfig(name="claude", command="claude"),
    }
    pipeline = [
        PipelineStage(stage=TaskStatus.PLANNING, agent="claude", cli_flags="--safe"),
        PipelineStage(stage=TaskStatus.EXECUTE, agent="claude", cli_flags="--debug"),
    ]
    config = MergedConfig(
        project=ProjectConfig(),
        global_cfg=GlobalConfig(),
        agents=agents,
        pipeline=pipeline,
    )
    engine, backend = _make_engine(config, tmp_project)
    task = Task(title="test", status=TaskStatus.PLANNING, session_id="pty_test")
    assert not engine._can_resume(task, TaskStatus.EXECUTE)


def test_resume_prompt_execute(tmp_project):
    """Resume prompt for EXECUTE includes task title and completion marker."""
    config = _make_same_agent_config()
    engine, _ = _make_engine(config, tmp_project)

    task = Task(title="My Feature", status=TaskStatus.PLANNING, verify="tests pass", done="login works")
    docs = tmp_project / ".llm-cc" / "tasks" / task.id
    docs.mkdir(parents=True, exist_ok=True)

    prompt = engine._build_resume_prompt(task, TaskStatus.EXECUTE, docs)
    assert "EXECUTE: My Feature" in prompt
    assert "Continue in the same session" in prompt
    assert "EXECUTE COMPLETE" in prompt
    assert "Verify: tests pass" in prompt
    assert "Done when: login works" in prompt


def test_resume_prompt_review(tmp_project):
    """Resume prompt for REVIEW references the work from previous stage."""
    config = _make_same_agent_config()
    engine, _ = _make_engine(config, tmp_project)

    task = Task(title="My Feature", status=TaskStatus.EXECUTE)
    docs = tmp_project / ".llm-cc" / "tasks" / task.id
    docs.mkdir(parents=True, exist_ok=True)

    prompt = engine._build_resume_prompt(task, TaskStatus.REVIEW, docs)
    assert "REVIEW: My Feature" in prompt
    assert "REVIEW COMPLETE" in prompt


async def test_advance_resumes_same_agent(tmp_project):
    """When same agent handles both stages, advance resumes instead of kill+start."""
    config = _make_same_agent_config()
    engine, backend = _make_engine(config, tmp_project)

    task = Task(title="test", status=TaskStatus.PLANNING, session_id="pty_test_planning")
    docs = tmp_project / ".llm-cc" / "tasks" / task.id
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "task.md").write_text("# test\n")

    await engine.advance(task)

    assert task.status == TaskStatus.EXECUTE
    assert task.session_id == "pty_test_planning"  # same session, not replaced
    backend.resume.assert_called_once()
    backend.start.assert_not_called()
    backend.stop.assert_not_called()


async def test_advance_starts_fresh_different_agent(tmp_project):
    """Different agents between stages → normal stop + start."""
    config = _make_diff_agent_config()
    engine, backend = _make_engine(config, tmp_project)

    task = Task(title="test", status=TaskStatus.PLANNING, session_id="pty_test_planning")
    docs = tmp_project / ".llm-cc" / "tasks" / task.id
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "task.md").write_text("# test\n")

    await engine.advance(task)

    assert task.status == TaskStatus.EXECUTE
    backend.stop.assert_called_once()  # old session stopped
    backend.start.assert_called_once()  # new session started
    backend.resume.assert_not_called()


# --- Brainstorm Session Persistence Tests ---


async def test_brainstorm_parks_session_on_advance(tmp_project):
    """advance_sub_agent parks the current agent instead of killing it."""
    config = _make_config()
    engine, backend = _make_engine(config, tmp_project)

    task = Task(title="test", status=TaskStatus.PLANNING, sub_agent_idx=0, loop_count=0)
    task.session_id = "pty_strategist_c0"
    docs = tmp_project / ".llm-cc" / "tasks" / task.id
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "task.md").write_text("# test\n")

    done = await engine.advance_sub_agent(task)

    assert not done
    # Session was parked, not stopped
    backend.stop.assert_not_called()
    assert 0 in engine._brainstorm_sessions.get(task.id, {})
    assert engine._brainstorm_sessions[task.id][0] == "pty_strategist_c0"


async def test_brainstorm_resumes_parked_session(tmp_project):
    """When a parked session exists and is alive, resume it instead of starting fresh."""
    config = _make_config(max_loops=2)
    engine, backend = _make_engine(config, tmp_project)

    task = Task(title="test", status=TaskStatus.PLANNING, sub_agent_idx=0, loop_count=1)
    docs = tmp_project / ".llm-cc" / "tasks" / task.id
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "task.md").write_text("# test\n")

    # Pre-park a strategist session (from cycle 1)
    engine._brainstorm_sessions[task.id] = {0: "pty_strategist_parked"}

    stage = config.stage_config(TaskStatus.PLANNING)
    await engine._spawn_brainstorm_agent(task, stage, docs)

    # Should resume, not start fresh
    backend.resume.assert_called_once()
    assert task.session_id == "pty_strategist_parked"
    backend.start.assert_not_called()
    # Parked entry should be removed
    assert 0 not in engine._brainstorm_sessions.get(task.id, {})


async def test_brainstorm_starts_fresh_if_parked_dead(tmp_project):
    """If parked session died, start a fresh one."""
    config = _make_config(max_loops=2)
    engine, backend = _make_engine(config, tmp_project)
    backend.is_alive = MagicMock(return_value=False)

    task = Task(title="test", status=TaskStatus.PLANNING, sub_agent_idx=0, loop_count=1)
    docs = tmp_project / ".llm-cc" / "tasks" / task.id
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "task.md").write_text("# test\n")

    engine._brainstorm_sessions[task.id] = {0: "pty_dead_session"}

    stage = config.stage_config(TaskStatus.PLANNING)
    await engine._spawn_brainstorm_agent(task, stage, docs)

    # Dead session → start fresh
    backend.start.assert_called_once()
    backend.resume.assert_not_called()
    # Dead parked reference cleaned up
    assert 0 not in engine._brainstorm_sessions.get(task.id, {})


async def test_brainstorm_cleanup_on_complete(tmp_project):
    """All parked sessions are stopped when brainstorm completes."""
    config = _make_config(max_loops=1)
    engine, backend = _make_engine(config, tmp_project)

    task = Task(title="test", status=TaskStatus.PLANNING, sub_agent_idx=1, loop_count=0)
    task.session_id = "pty_critic_c0"
    docs = tmp_project / ".llm-cc" / "tasks" / task.id
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "task.md").write_text("# test\n")

    # Simulate strategist was parked in cycle 0
    engine._brainstorm_sessions[task.id] = {0: "pty_strategist_parked"}

    done = await engine.advance_sub_agent(task)

    assert done
    # Parked sessions cleaned up
    assert task.id not in engine._brainstorm_sessions
    stopped_ids = {c.args[0] for c in engine.agents.stop_session.call_args_list}
    assert "pty_strategist_parked" in stopped_ids
    assert "pty_critic_c0" in stopped_ids


async def test_brainstorm_cleanup_on_revert(tmp_project):
    """Parked sessions are stopped when task reverts."""
    config = _make_config()
    engine, backend = _make_engine(config, tmp_project)

    task = Task(
        title="test", status=TaskStatus.PLANNING,
        sub_agent_idx=1, loop_count=1, session_id="pty_active",
    )
    engine._brainstorm_sessions[task.id] = {0: "pty_parked_strategist"}

    await engine.revert(task)

    assert task.status == TaskStatus.BACKLOG
    assert task.id not in engine._brainstorm_sessions
    engine.agents.stop_session.assert_called_once_with("pty_parked_strategist")


async def test_cleanup_task_stops_all(tmp_project):
    """cleanup_task stops both active and parked sessions."""
    config = _make_config()
    engine, backend = _make_engine(config, tmp_project)

    task = Task(title="test", status=TaskStatus.PLANNING, session_id="pty_active")
    engine._brainstorm_sessions[task.id] = {0: "pty_parked"}

    await engine.cleanup_task(task)

    assert task.session_id is None
    assert task.id not in engine._brainstorm_sessions
    engine.agents.stop_session.assert_called_once_with("pty_parked")


def test_brainstorm_resume_prompt_references_latest(tmp_project):
    """Resume prompt points to the most recent output from the other agent."""
    config = _make_config(max_loops=2)
    engine, _ = _make_engine(config, tmp_project)

    # Strategist resuming for cycle 2 after critic wrote cycle 1
    task = Task(title="My Task", status=TaskStatus.PLANNING, sub_agent_idx=0, loop_count=1)
    docs = tmp_project / ".llm-cc" / "tasks" / task.id
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "task.md").write_text("# My Task\n")

    stage = config.stage_config(TaskStatus.PLANNING)
    prompt = engine._build_brainstorm_resume_prompt(task, "strategist", stage, docs)

    assert "Continue BRAINSTORM: My Task" in prompt
    assert "Your role: strategist" in prompt
    assert "Cycle: 2/2" in prompt
    assert "critic-cycle1.md" in prompt
    assert "strategist-cycle2.md" in prompt
    assert "FINAL CYCLE" in prompt


def test_brainstorm_resume_prompt_non_wrapped(tmp_project):
    """Critic resuming (sub_agent_idx=1) references strategist's output."""
    config = _make_config(max_loops=2)
    engine, _ = _make_engine(config, tmp_project)

    # Critic resuming for cycle 2 after strategist wrote cycle 2
    task = Task(title="test", status=TaskStatus.PLANNING, sub_agent_idx=1, loop_count=1)
    docs = tmp_project / ".llm-cc" / "tasks" / task.id
    docs.mkdir(parents=True, exist_ok=True)

    stage = config.stage_config(TaskStatus.PLANNING)
    prompt = engine._build_brainstorm_resume_prompt(task, "critic", stage, docs)

    assert "strategist-cycle2.md" in prompt
    assert "critic-cycle2.md" in prompt


async def test_full_brainstorm_cycle_with_persistence(tmp_project):
    """Walk through a full 2-agent, 2-cycle brainstorm verifying parking and resuming."""
    config = _make_config(max_loops=2)
    engine, backend = _make_engine(config, tmp_project)

    # Enter planning (brainstorm)
    task = Task(title="test")
    await engine.advance(task)
    assert task.status == TaskStatus.PLANNING
    assert task.sub_agent_idx == 0  # strategist
    strategist_session = task.session_id

    # Strategist done → advance to critic (cycle 1)
    done = await engine.advance_sub_agent(task)
    assert not done
    assert task.sub_agent_idx == 1  # critic
    critic_session = task.session_id
    # Strategist was parked
    assert engine._brainstorm_sessions[task.id][0] == strategist_session

    # Critic done → advance to strategist (cycle 2, wrap)
    done = await engine.advance_sub_agent(task)
    assert not done
    assert task.sub_agent_idx == 0  # back to strategist
    assert task.loop_count == 1
    # Critic was parked
    assert engine._brainstorm_sessions[task.id][1] == critic_session
    # Strategist was resumed (not started fresh)
    backend.resume.assert_called()

    # Strategist cycle 2 done → advance to critic (cycle 2)
    done = await engine.advance_sub_agent(task)
    assert not done
    assert task.sub_agent_idx == 1  # critic

    # Critic cycle 2 done → brainstorm complete (max_loops reached)
    done = await engine.advance_sub_agent(task)
    assert done
    assert task.id not in engine._brainstorm_sessions  # all cleaned up
