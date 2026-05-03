"""Tests for worktree path resolution in pipeline.

Verifies that when a task has worktree_path set:
- Agent is spawned with worktree as cwd
- Task docs (.llm-cc/tasks/) use absolute paths in prompts
- Plan files resolve inside the worktree
- Branch mode falls through to project_path correctly
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_cc.models import (
    AgentConfig,
    GitMode,
    GlobalConfig,
    MergedConfig,
    PipelineStage,
    ProjectConfig,
    Task,
    TaskStatus,
)
from llm_cc.pipeline import PipelineEngine


def _make_config(git_mode=GitMode.WORKTREE):
    agents = {
        "claude": AgentConfig(name="claude", command="claude"),
    }
    pipeline = [
        PipelineStage(stage=TaskStatus.PLANNING, agent="claude"),
        PipelineStage(stage=TaskStatus.EXECUTE, agent="claude"),
        PipelineStage(stage=TaskStatus.REVIEW, agent="claude"),
    ]
    return MergedConfig(
        project=ProjectConfig(git={"mode": git_mode.value}),
        global_cfg=GlobalConfig(),
        agents=agents,
        pipeline=pipeline,
    )


def _make_engine(config, tmp_path):
    registry = MagicMock()
    backend = AsyncMock()
    backend.start = AsyncMock(return_value="pty_test_planning")
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
def project_with_worktree(tmp_path):
    """Create a project structure with a worktree subdirectory."""
    # Main tree
    (tmp_path / ".llm-cc" / "tasks").mkdir(parents=True)

    # Worktree (simulated — just a directory)
    wt = tmp_path / ".llm-cc" / "worktrees" / "test-slug"
    wt.mkdir(parents=True)

    return tmp_path, wt


# --- _task_cwd ---


def test_task_cwd_returns_worktree_when_set(project_with_worktree):
    tmp_path, wt = project_with_worktree
    config = _make_config()
    engine, _ = _make_engine(config, tmp_path)

    task = Task(title="test", worktree_path=str(wt))
    assert engine._task_cwd(task) == wt


def test_task_cwd_returns_project_when_no_worktree(project_with_worktree):
    tmp_path, _ = project_with_worktree
    config = _make_config()
    engine, _ = _make_engine(config, tmp_path)

    task = Task(title="test")
    assert engine._task_cwd(task) == tmp_path


def test_task_cwd_falls_back_when_worktree_deleted(project_with_worktree):
    tmp_path, _ = project_with_worktree
    config = _make_config()
    engine, _ = _make_engine(config, tmp_path)

    task = Task(title="test", worktree_path="/nonexistent/path")
    assert engine._task_cwd(task) == tmp_path


# --- _in_worktree ---


def test_in_worktree_true(project_with_worktree):
    tmp_path, wt = project_with_worktree
    config = _make_config()
    engine, _ = _make_engine(config, tmp_path)

    task = Task(title="test", worktree_path=str(wt))
    assert engine._in_worktree(task) is True


def test_in_worktree_false_no_path(project_with_worktree):
    tmp_path, _ = project_with_worktree
    config = _make_config()
    engine, _ = _make_engine(config, tmp_path)

    task = Task(title="test")
    assert engine._in_worktree(task) is False


# --- Prompt path resolution ---


def test_docs_absolute_in_worktree(project_with_worktree):
    """Task docs should use absolute paths when agent runs in worktree."""
    tmp_path, wt = project_with_worktree
    config = _make_config()
    engine, _ = _make_engine(config, tmp_path)

    task = Task(title="test", worktree_path=str(wt))
    docs = tmp_path / ".llm-cc" / "tasks" / task.id
    docs.mkdir(parents=True, exist_ok=True)

    result = engine._docs_path_for_prompt(docs, task)
    # Must be absolute — docs are in main tree, not the worktree
    assert result.startswith("/")
    assert ".llm-cc/tasks/" in result


def test_docs_relative_without_worktree(project_with_worktree):
    """Task docs should use relative paths when no worktree."""
    tmp_path, _ = project_with_worktree
    config = _make_config()
    engine, _ = _make_engine(config, tmp_path)

    task = Task(title="test")
    docs = tmp_path / ".llm-cc" / "tasks" / task.id
    docs.mkdir(parents=True, exist_ok=True)

    result = engine._docs_path_for_prompt(docs, task)
    assert not result.startswith("/")
    assert result.startswith(".llm-cc/tasks/")


def test_plan_absolute_in_worktree(project_with_worktree):
    """Plan lives in main tree (.llm-cc/tasks/), so worktree agents get absolute path."""
    tmp_path, wt = project_with_worktree
    config = _make_config()
    engine, _ = _make_engine(config, tmp_path)

    task = Task(title="test", worktree_path=str(wt), branch_name="task/test-slug")

    plan_path = engine._resolve_plan_path(task)
    # Plan should be in main tree (default plan_dir is .llm-cc/tasks/{id})
    assert str(plan_path).startswith(str(tmp_path))
    assert str(wt) not in str(plan_path) or str(plan_path).startswith(str(tmp_path))

    result = engine._plan_path_for_prompt(plan_path, task)
    # Must be absolute — plan is in main tree, agent cwd is worktree
    assert result.startswith("/")
    assert "plan.md" in result


def test_plan_relative_without_worktree(project_with_worktree):
    """Plan path should be relative to project root without worktree."""
    tmp_path, _ = project_with_worktree
    config = _make_config()
    engine, _ = _make_engine(config, tmp_path)

    task = Task(title="test", branch_name="task/test-slug")

    plan_path = engine._resolve_plan_path(task)
    assert str(plan_path).startswith(str(tmp_path))

    result = engine._plan_path_for_prompt(plan_path, task)
    assert not result.startswith("/")


# --- Agent cwd in backend.start ---


async def test_agent_spawned_in_worktree(project_with_worktree):
    """backend.start() must receive worktree path as cwd."""
    tmp_path, wt = project_with_worktree
    config = _make_config()
    engine, backend = _make_engine(config, tmp_path)

    task = Task(title="test", worktree_path=str(wt))
    await engine.advance(task)

    assert task.status == TaskStatus.PLANNING
    backend.start.assert_called_once()
    call_kwargs = backend.start.call_args
    # 4th positional arg is cwd
    cwd_arg = call_kwargs[0][3]
    assert cwd_arg == wt, f"Agent cwd should be worktree, got {cwd_arg}"


async def test_agent_spawned_in_project_without_worktree(project_with_worktree):
    """backend.start() must receive project path as cwd when no worktree."""
    tmp_path, _ = project_with_worktree
    config = _make_config()
    engine, backend = _make_engine(config, tmp_path)

    task = Task(title="test")
    await engine.advance(task)

    assert task.status == TaskStatus.PLANNING
    backend.start.assert_called_once()
    call_kwargs = backend.start.call_args
    cwd_arg = call_kwargs[0][3]
    assert cwd_arg == tmp_path


# --- Prompt content verification ---


async def test_worktree_prompt_has_absolute_docs_path(project_with_worktree):
    """When in worktree, prompt must contain absolute path to task docs."""
    tmp_path, wt = project_with_worktree
    config = _make_config()
    engine, backend = _make_engine(config, tmp_path)

    task = Task(title="test", worktree_path=str(wt))
    await engine.advance(task)

    # Extract the prompt from backend.start call
    prompt = backend.start.call_args[0][2]
    # Docs path should be absolute (pointing to main tree's .llm-cc/)
    assert str(tmp_path / ".llm-cc" / "tasks") in prompt


async def test_no_worktree_prompt_has_relative_docs_path(project_with_worktree):
    """When not in worktree, prompt must contain relative path to task docs."""
    tmp_path, _ = project_with_worktree
    config = _make_config()
    engine, backend = _make_engine(config, tmp_path)

    task = Task(title="test")
    await engine.advance(task)

    prompt = backend.start.call_args[0][2]
    # Should be relative, not absolute
    assert ".llm-cc/tasks/" in prompt
    assert str(tmp_path) not in prompt
