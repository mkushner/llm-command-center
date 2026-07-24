"""Claude Code statusline hook: writes per-session telemetry for llm-cc.

Shared by both frontends (TUI `app.py` and web `server.py`) so the browser gets
the same context %/token data the TUI does. The hook script writes each session's
statusline JSON to `.llm-cc/status/<task_id>.json`, which the backends read.
"""

from __future__ import annotations

import json
from pathlib import Path

STATUSLINE_SCRIPT = '''\
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


def setup_statusline(project_path: Path) -> None:
    """Write the statusline hook and register it in the global Claude settings.

    Idempotent: only claims `statusLine` if it's unset or already points at our
    script (never clobbers a user's own statusline).
    """
    script_path = project_path / ".llm-cc" / "bin" / "statusline.py"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(STATUSLINE_SCRIPT)
    script_path.chmod(0o755)

    settings_path = Path.home() / ".claude" / "settings.local.json"
    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except Exception:
            settings = {}

    expected_cmd = f"python3 {script_path}"
    current = settings.get("statusLine", {})
    if not current or ".llm-cc/bin/statusline.py" in current.get("command", ""):
        settings["statusLine"] = {"type": "command", "command": expected_cmd}
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings, indent=2))
