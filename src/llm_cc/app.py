"""Main Textual application: screen routing, lifecycle."""

from __future__ import annotations

import json
import time
from pathlib import Path

from textual.app import App

from llm_cc.agents import AgentRegistry, is_clean_exit_mode
from llm_cc.git import GitWorkspace
from llm_cc.pipeline import PipelineEngine
from llm_cc.storage import Storage
from llm_cc.ui.board import BoardScreen

_STATUSLINE_SCRIPT = '''\
#!/usr/bin/env python3
"""Claude Code statusline hook — writes session status for llm-command-center."""
import json, os, sys

data = json.load(sys.stdin)
task_id = os.environ.get("LLM_CC_TASK_ID")
if task_id:
    status_dir = os.path.join(".llm-cc", "status")
    os.makedirs(status_dir, exist_ok=True)
    with open(os.path.join(status_dir, f"{task_id}.json"), "w") as f:
        json.dump(data, f)

# Output statusline for Claude Code's own display
ctx = data.get("context_window", {})
usage = ctx.get("current_usage") or {}
used = ctx.get("used_percentage")
parts = []
if used is not None:
    parts.append(f"{100 - used}% ctx")
inp = usage.get("input_tokens", 0)
if inp:
    parts.append(f"{inp / 1000:.1f}k in")
out = usage.get("output_tokens", 0)
if out:
    parts.append(f"{out / 1000:.1f}k out")
cache_cr = usage.get("cache_creation_input_tokens", 0)
if cache_cr:
    parts.append(f"{cache_cr / 1000:.1f}k cache wr")
cache_rd = usage.get("cache_read_input_tokens", 0)
if cache_rd:
    parts.append(f"{cache_rd / 1000:.1f}k cache rd")
if parts:
    print(" | ".join(parts))
'''


class CommandCenterApp(App):
    """LLM Command Center — Multi-agent pipeline orchestration TUI."""

    CSS_PATH = "ui/styles.tcss"
    TITLE = "LLM Command Center"

    def __init__(self, project_path: Path) -> None:
        super().__init__()
        self.project_path = project_path
        self.storage = Storage(project_path)
        self._config = self.storage.load_config()
        self.registry = AgentRegistry(
            self._config.agents,
            sessions_dir=project_path / ".llm-cc" / "sessions",
        )

        self.git = GitWorkspace(project_path, self._config.project.git)
        self.pipeline = PipelineEngine(
            self._config, self.registry, self.git, self.storage
        )
        self._setup_statusline()
        # Toast dedupe: drop identical messages emitted within 1.5s
        self._recent_notifies: dict[str, float] = {}

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: str = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        key = f"{severity}:{message}"
        now = time.monotonic()
        last = self._recent_notifies.get(key)
        if last is not None and now - last < 1.5:
            return
        self._recent_notifies[key] = now
        if len(self._recent_notifies) > 64:
            cutoff = now - 5.0
            self._recent_notifies = {
                k: t for k, t in self._recent_notifies.items() if t >= cutoff
            }
        super().notify(
            message,
            title=title,
            severity=severity,  # type: ignore[arg-type]
            timeout=timeout,
            markup=markup,
        )

    def _setup_statusline(self) -> None:
        """Write statusline hook script and configure Claude Code globally."""
        script_path = self.project_path / ".llm-cc" / "bin" / "statusline.py"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(_STATUSLINE_SCRIPT)
        script_path.chmod(0o755)

        # Write to global ~/.claude/settings.local.json so it applies
        # regardless of which project directory the spawned agent runs in.
        settings_path = Path.home() / ".claude" / "settings.local.json"
        settings: dict = {}
        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text())
            except Exception:
                settings = {}

        expected_cmd = f"python3 {script_path}"
        current = settings.get("statusLine", {})
        # Update if missing or if we own it (points to our script path)
        if not current or ".llm-cc/bin/statusline.py" in current.get("command", ""):
            settings["statusLine"] = {
                "type": "command",
                "command": expected_cmd,
            }
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(json.dumps(settings, indent=2))

    async def on_mount(self) -> None:
        reattached, orphans = await self.registry.reattach_existing(self.project_path)
        if reattached:
            self.notify(f"Reattached to {reattached} agent session(s)", timeout=3)
        if orphans:
            self.log(f"orphan tmux sessions (no matching task): {orphans}")
        self.push_screen(BoardScreen(self.storage, self.pipeline, self.registry))

    async def on_unmount(self) -> None:
        if self.registry.session_store:
            self.registry.session_store.flush_all()
        if is_clean_exit_mode():
            await self.registry.cleanup_all()
        else:
            await self.registry.detach_all()
