"""All data models for llm-command-center. Single file, no scattered imports."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


# --- Task ---


class TaskStatus(str, Enum):
    BACKLOG = "backlog"
    PLANNING = "planning"
    EXECUTE = "execute"
    REVIEW = "review"
    DONE = "done"


STAGE_ORDER = list(TaskStatus)


class Task(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    title: str
    description: str | None = None
    status: TaskStatus = TaskStatus.BACKLOG
    agent_override: str | None = None
    session_id: str | None = None
    worktree_path: str | None = None
    branch_name: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    docs_path: str | None = None  # .llm-cc/tasks/<id>/ — shared docs between stages
    verify: str | None = None   # how to verify task completion (e.g., "curl returns 200")
    done: str | None = None     # definition of done (e.g., "login works with valid/invalid creds")
    sub_agent_idx: int = 0    # brainstorm: index into stage.agents
    loop_count: int = 0       # brainstorm: current cycle (0-based)
    brainstorm_summarizing: bool = False  # brainstorm: in final summary phase
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def slug(self) -> str:
        safe = "".join(c if c.isalnum() else "-" for c in self.title.lower())
        return f"{self.id}-{safe[:30].strip('-')}"

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)


class TaskStore(BaseModel):
    """Root object for .llm-cc/tasks.json."""

    version: int = 1
    tasks: list[Task] = []

    def get(self, task_id: str) -> Task | None:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None

    def upsert(self, task: Task) -> None:
        for i, t in enumerate(self.tasks):
            if t.id == task.id:
                self.tasks[i] = task
                return
        self.tasks.append(task)

    def remove(self, task_id: str) -> Task | None:
        for i, t in enumerate(self.tasks):
            if t.id == task_id:
                return self.tasks.pop(i)
        return None

    def by_status(self, status: TaskStatus) -> list[Task]:
        return [t for t in self.tasks if t.status == status]


# --- Agent Config ---


class AgentMode(str, Enum):
    PTY = "pty"
    API = "api"


class AgentConfig(BaseModel):
    name: str
    command: str | None = None
    args_template: str = "{prompt}"
    model: str | None = None          # model name for display + {model} in args_template
    mode: AgentMode = AgentMode.PTY
    api_provider: str | None = None
    api_model: str | None = None
    resume_template: str | None = None
    co_author: str = ""
    detect_command: str | None = None

    @property
    def display_label(self) -> str:
        """Agent name + model for UI, e.g. 'claude opus-4.6'."""
        if self.model:
            return f"{self.name} {self.model}"
        return self.name


# --- Pipeline ---


class PipelineStage(BaseModel):
    stage: TaskStatus
    agent: str = ""
    agents: list[str] = []                # sequential sub-agents for brainstorm
    max_loops: int = 1                    # how many full cycles through agents list
    summarizer: str = ""                  # agent that writes final summary after all loops
    mode_override: AgentMode | None = None
    prompt_template: str | None = None
    cli_flags: str = ""  # extra CLI flags for this stage (e.g. "--dangerously-skip-permissions")
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


# --- Git Config ---


class GitMode(str, Enum):
    WORKTREE = "worktree"
    BRANCH = "branch"
    NONE = "none"  # no git — agent runs in project directory as-is


class GitConfig(BaseModel):
    mode: GitMode = GitMode.NONE
    base_branch: str = "main"
    branch_prefix: str = "task/"  # prefix for branch names, e.g. "task/" → "task/a1b2-slug"
    copy_files: list[str] = []
    init_script: str | None = None


# --- Project & Global Config ---


class ProjectConfig(BaseModel):
    name: str = ""
    github_url: str | None = None
    git: GitConfig = GitConfig()
    pipeline: list[PipelineStage] = []
    agents: dict[str, AgentConfig] = {}
    stage_labels: dict[str, str] = {}
    plan_dir: str = ".llm-cc/tasks/{id}"  # template: {id}, {slug}, {branch}, {title}
    plan_file: str = "plan.md"  # constant filename within plan_dir
    review_file: str | None = None  # optional: where review agent writes summary (resolved in plan_dir)


class GlobalConfig(BaseModel):
    default_agent: str = "claude"
    recent_projects: list[str] = []
    theme: str = "default"


class MergedConfig(BaseModel):
    """Runtime config: global + project merged."""

    project: ProjectConfig
    global_cfg: GlobalConfig
    agents: dict[str, AgentConfig]
    pipeline: list[PipelineStage]

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
        # Absolute fallback
        return next(iter(self.agents.values()))

    def stage_config(self, status: TaskStatus) -> PipelineStage | None:
        for stage in self.pipeline:
            if stage.stage == status:
                return stage
        return None

    def active_stages(self) -> list[TaskStatus]:
        """Stages visible on the board: BACKLOG + configured stages + DONE."""
        configured = {s.stage for s in self.pipeline}
        return [
            s for s in STAGE_ORDER
            if s in (TaskStatus.BACKLOG, TaskStatus.DONE) or s in configured
        ]

    def label_for(self, status: TaskStatus) -> str:
        """Get display label for a stage (custom or default)."""
        return self.project.stage_labels.get(status.value, status.value.title())


