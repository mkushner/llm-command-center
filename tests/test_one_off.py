"""One-off tasks: skip git isolation and run in the project root.

A one-off opts out of the worktree whatever the configured GitMode is, so it
lands in the project root on the branch that is already checked out. Because it
shares that checkout, it also has to contend for the single EXECUTE slot with
the other root-sharing tasks — but not with worktree tasks, which are isolated.

It also skips BACKLOG and PLANNING: there is no spec to plan from, so the agent
starts in EXECUTE with an empty prompt and the user drives it from the terminal.
"""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_cc.agents import render_args
from llm_cc.git import GitWorkspace
from llm_cc.models import (
    AgentConfig,
    GitConfig,
    GitMode,
    GlobalConfig,
    MergedConfig,
    PipelineStage,
    ProjectConfig,
    Task,
    TaskStatus,
    TaskStore,
)
from llm_cc.pipeline import PipelineEngine


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """Git repo with one commit on main, checked out on feature/x."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "README.md").write_text("hi\n")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "init")
    _git(tmp_path, "checkout", "-b", "feature/x")
    return tmp_path


def _workspace(repo: Path) -> GitWorkspace:
    return GitWorkspace(repo, GitConfig(mode=GitMode.WORKTREE, base_branch="main"))


# --- GitWorkspace.setup ---


async def test_one_off_stays_in_project_root(repo):
    task = Task(title="ad hoc", one_off=True)

    cwd = await _workspace(repo).setup(task)

    assert cwd == repo
    assert task.worktree_path is None
    assert task.branch_name == "feature/x"  # current branch, not a cut one
    assert not (repo / ".llm-cc" / "worktrees").exists()


async def test_normal_task_still_gets_a_worktree(repo):
    task = Task(title="isolated")

    cwd = await _workspace(repo).setup(task)

    assert cwd != repo
    assert task.worktree_path is not None
    assert Path(task.worktree_path).exists()


async def test_one_off_cleanup_leaves_the_checkout_alone(repo):
    ws = _workspace(repo)
    task = Task(title="ad hoc", one_off=True)
    await ws.setup(task)

    await ws.cleanup(task)

    assert (repo / "README.md").exists()
    assert task.worktree_path is None


# --- EXECUTE slot contention ---


def _engine(tmp_path: Path, tasks: list[Task], git_mode=GitMode.WORKTREE) -> PipelineEngine:
    config = MergedConfig(
        project=ProjectConfig(git={"mode": git_mode.value}),
        global_cfg=GlobalConfig(),
        agents={"claude": AgentConfig(name="claude", command="claude")},
        pipeline=[PipelineStage(stage=TaskStatus.EXECUTE, agent="claude")],
    )
    storage = MagicMock()
    storage.llm_cc_dir = tmp_path / ".llm-cc"
    storage.load_tasks = MagicMock(return_value=TaskStore(tasks=tasks))
    git = MagicMock()
    git.project_path = tmp_path
    git.setup = AsyncMock(return_value=tmp_path)
    registry = MagicMock()
    backend = AsyncMock()
    backend.start = AsyncMock(return_value="llmcc_x_execute")
    registry.backend_for = MagicMock(return_value=backend)
    return PipelineEngine(config, registry, git, storage)


def _backend(engine: PipelineEngine) -> AsyncMock:
    return engine.agents.backend_for.return_value  # type: ignore[attr-defined,no-any-return]


def test_one_off_blocked_by_another_one_off(tmp_path):
    running = Task(title="other one-off", status=TaskStatus.EXECUTE, one_off=True)
    engine = _engine(tmp_path, [running])

    with pytest.raises(RuntimeError, match="Execute slot occupied"):
        engine._require_execute_slot(Task(title="mine", one_off=True))


def test_one_off_not_blocked_by_worktree_task(tmp_path):
    running = Task(title="isolated", status=TaskStatus.EXECUTE, worktree_path="/tmp/wt")
    engine = _engine(tmp_path, [running])

    engine._require_execute_slot(Task(title="mine", one_off=True))


def test_worktree_task_never_contends(tmp_path):
    running = Task(title="other one-off", status=TaskStatus.EXECUTE, one_off=True)
    engine = _engine(tmp_path, [running])

    engine._require_execute_slot(Task(title="mine"))


def test_none_mode_slot_guard_unchanged(tmp_path):
    running = Task(title="occupier", status=TaskStatus.EXECUTE)
    engine = _engine(tmp_path, [running], git_mode=GitMode.NONE)

    with pytest.raises(RuntimeError, match="Execute slot occupied"):
        engine._require_execute_slot(Task(title="mine"))


# --- start_one_off ---


async def test_start_one_off_lands_in_execute_with_empty_prompt(tmp_path):
    engine = _engine(tmp_path, [])
    task = Task(title="one-off 14:32", one_off=True)

    await engine.start_one_off(task)

    assert task.status == TaskStatus.EXECUTE  # skipped backlog and planning
    assert task.session_id == "llmcc_x_execute"
    args = _backend(engine).start.call_args[0]
    assert args[2] == ""       # no stage prompt
    assert args[3] == tmp_path  # project root, not a worktree
    engine.storage.save_task.assert_called_once_with(task)


async def test_start_one_off_respects_the_execute_slot(tmp_path):
    engine = _engine(tmp_path, [Task(title="busy", status=TaskStatus.EXECUTE, one_off=True)])
    task = Task(title="mine", one_off=True)

    with pytest.raises(RuntimeError, match="Execute slot occupied"):
        await engine.start_one_off(task)

    _backend(engine).start.assert_not_called()
    engine.storage.save_task.assert_not_called()


# --- empty prompt means "just open the CLI" ---


def test_render_args_empty_prompt_passes_no_argument():
    config = AgentConfig(name="claude", command="claude", args_template="{prompt}")
    assert render_args(config, "", "abc123") == ""


def test_render_args_quotes_a_real_prompt():
    config = AgentConfig(name="claude", command="claude", args_template="{prompt}")
    assert render_args(config, "do a thing", "abc123") == "'do a thing'"
