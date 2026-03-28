"""JSON task store + TOML config loading. File locking for safety."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import tomllib
from pathlib import Path

from .models import (
    AgentConfig,
    AgentMode,
    GlobalConfig,
    MergedConfig,
    PipelineStage,
    ProjectConfig,
    Task,
    TaskStatus,
    TaskStore,
)

LLM_CC_DIR = ".llm-cc"
TASKS_FILE = "tasks.json"
LOCK_FILE = "tasks.lock"
CONFIG_FILE = "config.toml"
GLOBAL_CONFIG_DIR = Path.home() / ".config" / "llm-cc"


class Storage:
    def __init__(self, project_path: Path) -> None:
        self.project_path = project_path
        self.llm_cc_dir = project_path / LLM_CC_DIR
        self.tasks_file = self.llm_cc_dir / TASKS_FILE
        self._lock_file = self.llm_cc_dir / LOCK_FILE
        self.config_file = self.llm_cc_dir / CONFIG_FILE
        self.global_config_path = GLOBAL_CONFIG_DIR / CONFIG_FILE

    def ensure_dirs(self) -> None:
        self.llm_cc_dir.mkdir(parents=True, exist_ok=True)
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # --- Tasks ---

    def _acquire_lock(self) -> int:
        """Acquire exclusive file lock. Returns lock fd."""
        self.ensure_dirs()
        fd = os.open(str(self._lock_file), os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except Exception:
            os.close(fd)
            raise
        return fd

    def _release_lock(self, fd: int) -> None:
        """Release file lock."""
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    def _read_store(self) -> TaskStore:
        """Read tasks from file. Caller must hold lock."""
        if not self.tasks_file.exists():
            return TaskStore()
        try:
            with open(self.tasks_file) as f:
                data = json.load(f)
            return TaskStore.model_validate(data)
        except Exception:
            return TaskStore()

    def _write_store(self, store: TaskStore) -> None:
        """Atomic write: write to temp file, then rename. Caller must hold lock."""
        self.ensure_dirs()
        fd, tmp_path = tempfile.mkstemp(dir=str(self.llm_cc_dir), suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(store.model_dump(mode="json"), f, indent=2, default=str)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(self.tasks_file))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def load_tasks(self) -> TaskStore:
        fd = self._acquire_lock()
        try:
            return self._read_store()
        finally:
            self._release_lock(fd)

    def save_tasks(self, store: TaskStore) -> None:
        fd = self._acquire_lock()
        try:
            self._write_store(store)
        finally:
            self._release_lock(fd)

    def save_task(self, task: Task) -> None:
        """Upsert a single task. Atomic read-modify-write under one lock."""
        fd = self._acquire_lock()
        try:
            store = self._read_store()
            store.upsert(task)
            self._write_store(store)
        finally:
            self._release_lock(fd)

    def delete_task(self, task_id: str) -> Task | None:
        fd = self._acquire_lock()
        try:
            store = self._read_store()
            removed = store.remove(task_id)
            if removed:
                self._write_store(store)
            return removed
        finally:
            self._release_lock(fd)

    # --- Config ---

    def load_config(self) -> MergedConfig:
        global_cfg = self._load_global_config()
        project_cfg = self._load_project_config()
        agents = {**_default_agents(), **project_cfg.agents}
        pipeline = project_cfg.pipeline or _default_pipeline()
        return MergedConfig(
            project=project_cfg,
            global_cfg=global_cfg,
            agents=agents,
            pipeline=pipeline,
        )

    def _load_project_config(self) -> ProjectConfig:
        if not self.config_file.exists():
            return ProjectConfig(name=self.project_path.name)
        with open(self.config_file, "rb") as f:
            data = tomllib.load(f)
        # Flatten nested [project] section if present
        if "project" in data:
            proj = data.pop("project")
            data = {**proj, **data}
        # Parse agents from [agents.*] sections
        if "agents" in data and isinstance(data["agents"], dict):
            for name, cfg in data["agents"].items():
                if isinstance(cfg, dict) and "name" not in cfg:
                    cfg["name"] = name
        return ProjectConfig.model_validate(data)

    def _load_global_config(self) -> GlobalConfig:
        if not self.global_config_path.exists():
            return GlobalConfig()
        with open(self.global_config_path, "rb") as f:
            return GlobalConfig.model_validate(tomllib.load(f))

    # --- Recent Projects ---

    def update_recent_projects(self, path: str) -> None:
        cfg = self._load_global_config()
        cfg.recent_projects = [path] + [p for p in cfg.recent_projects if p != path]
        cfg.recent_projects = cfg.recent_projects[:20]
        self._save_global_config(cfg)

    def _save_global_config(self, cfg: GlobalConfig) -> None:
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        lines = [
            f'default_agent = "{cfg.default_agent}"',
            f'theme = "{cfg.theme}"',
            "",
            "recent_projects = [",
        ]
        for p in cfg.recent_projects:
            lines.append(f'  "{p}",')
        lines.append("]")
        lines.append("")
        self.global_config_path.write_text("\n".join(lines))

    # --- .gitignore ---

    def ensure_gitignore(self) -> None:
        gitignore = self.llm_cc_dir / ".gitignore"
        if not gitignore.exists():
            self.ensure_dirs()
            gitignore.write_text("worktrees/\nlogs/\ntasks/\ntasks.json\n")


# --- Default configs ---

_DEFAULT_CLAUDE_ALLOWED_TOOLS: list[str] = [
    "Read", "Glob", "Grep", "LS", "WebFetch", "WebSearch",
    "Bash(git status:*)", "Bash(git log:*)", "Bash(git diff:*)",
    "Bash(git show:*)", "Bash(git branch:*)",
    "Bash(gh issue:*)", "Bash(gh pr list:*)", "Bash(gh pr view:*)",
    "Bash(gh pr checks:*)", "Bash(gh pr diff:*)",
    "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)",
    "Bash(find:*)", "Bash(grep:*)", "Bash(rg:*)",
    "Bash(wc:*)", "Bash(file:*)", "Bash(which:*)",
    "Bash(echo:*)", "Bash(pwd:*)",
]


def _default_agents() -> dict[str, AgentConfig]:
    return {
        "claude": AgentConfig(
            name="claude",
            command="claude",
            args_template="{prompt}",
            co_author="Claude <noreply@anthropic.com>",
            allowed_tools=_DEFAULT_CLAUDE_ALLOWED_TOOLS,
        ),
        "codex": AgentConfig(
            name="codex",
            command="codex",
            args_template='"{prompt}"',
            co_author="Codex <noreply@openai.com>",
        ),
    }


def _default_pipeline() -> list[PipelineStage]:
    return [
        PipelineStage(stage=TaskStatus.PLANNING, agent="claude"),
        PipelineStage(stage=TaskStatus.EXECUTE, agent="claude"),
        PipelineStage(stage=TaskStatus.REVIEW, agent="claude"),
    ]
