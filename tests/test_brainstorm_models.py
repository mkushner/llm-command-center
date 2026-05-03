"""Tests for brainstorm model extensions."""

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
