"""Auto-permission setup for worktree-isolated agents.

Worktrees created by llm-cc get a `.claude/settings.local.json` with
`defaultMode: acceptEdits` so file edits inside the disposable worktree
auto-approve, while shell/network/dangerous prompts still surface. The
file is globally gitignored so it doesn't pollute the project's tracked
files.

`acceptEdits` only covers the workspace, so anything the agent has to touch
outside its worktree — the main checkout (task docs and plan files live
there) or a sibling repo from `git.extra_dirs` — is listed in
`permissions.additionalDirectories`. Without that, every such edit prompts
individually.

Codex is handled separately in `agents.py` via `--full-auto` flag injection.
No filesystem mutation for Codex.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

CLAUDE_IGNORE_LINE = ".claude/settings.local.json"
DEFAULT_EXCLUDES_FILE = Path.home() / ".config" / "git" / "ignore"
GLOBAL_CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON via tempfile in the same dir, then os.replace."""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_claude_settings(worktree: Path, extra_dirs: Iterable[Path | str] = ()) -> None:
    """Write or refresh `.claude/settings.local.json` in the worktree.

    Merges rather than clobbers: existing keys are kept, `defaultMode` is only
    filled in when absent, and `extra_dirs` are added to
    `permissions.additionalDirectories` if not already there. Nothing is
    written when the result is unchanged.
    """
    try:
        d = worktree / ".claude"
        d.mkdir(exist_ok=True)
        path = d / "settings.local.json"

        data: dict[str, Any] = {}
        if path.exists():
            raw = path.read_text()
            try:
                data = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError as e:
                print(f"llm-cc: {path} is not valid JSON, skipping: {e}", file=sys.stderr)
                return
        if not isinstance(data, dict):
            return
        perms = data.setdefault("permissions", {})
        if not isinstance(perms, dict):
            return

        before = json.dumps(data, sort_keys=True)
        perms.setdefault("defaultMode", "acceptEdits")

        current = perms.get("additionalDirectories")
        dirs = [str(x) for x in current] if isinstance(current, list) else []
        merged = list(dict.fromkeys(dirs + [str(p) for p in extra_dirs]))
        if merged:
            perms["additionalDirectories"] = merged

        if json.dumps(data, sort_keys=True) != before:
            _atomic_write_json(path, data)
    except Exception as e:
        print(f"llm-cc: could not write Claude settings to {worktree}: {e}", file=sys.stderr)


def _get_global_excludes_file() -> Path:
    """Resolve `core.excludesFile` from global git config.

    If unset, sets it to `~/.config/git/ignore` and returns that path.
    """
    res = subprocess.run(
        ["git", "config", "--global", "--get", "core.excludesFile"],
        capture_output=True,
        text=True,
        check=False,
    )
    path_str = res.stdout.strip() if res.returncode == 0 else ""
    if path_str:
        return Path(os.path.expanduser(path_str))

    # Not set — configure git to use the default XDG location
    subprocess.run(
        ["git", "config", "--global", "core.excludesFile", str(DEFAULT_EXCLUDES_FILE)],
        check=False,
        capture_output=True,
    )
    return DEFAULT_EXCLUDES_FILE


def ensure_global_claude_default_mode() -> None:
    """Set `permissions.defaultMode = "acceptEdits"` in `~/.claude/settings.json`.

    Preserves all other keys. Idempotent — only writes when the value actually
    changes. Will not overwrite an explicit `"bypassPermissions"` or `"plan"`
    setting (respect user's stronger choice); only upgrades `"default"` (or
    missing) to `"acceptEdits"`.
    """
    try:
        path = GLOBAL_CLAUDE_SETTINGS
        if path.exists():
            raw = path.read_text()
            try:
                data = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError as e:
                print(
                    f"llm-cc: ~/.claude/settings.json is not valid JSON, skipping: {e}",
                    file=sys.stderr,
                )
                return
        else:
            data = {}

        if not isinstance(data, dict):
            print("llm-cc: ~/.claude/settings.json is not an object, skipping", file=sys.stderr)
            return

        perms = data.setdefault("permissions", {})
        if not isinstance(perms, dict):
            print(
                "llm-cc: ~/.claude/settings.json 'permissions' is not an object, skipping",
                file=sys.stderr,
            )
            return

        current = perms.get("defaultMode")
        # Respect stronger user choices; only upgrade from default/unset.
        if current not in (None, "default"):
            return
        if current == "acceptEdits":
            return

        perms["defaultMode"] = "acceptEdits"

        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        print(
            f"llm-cc: set permissions.defaultMode = \"acceptEdits\" in {path}",
            file=sys.stderr,
        )
    except Exception as e:
        print(f"llm-cc: could not update global Claude settings: {e}", file=sys.stderr)


def ensure_global_gitignore() -> None:
    """Append `.claude/settings.local.json` to the user's global gitignore.

    Idempotent. Logs to stderr on first addition. Silent thereafter.
    Any failure is logged but does not raise.
    """
    try:
        path = _get_global_excludes_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text() if path.exists() else ""
        if CLAUDE_IGNORE_LINE in existing.splitlines():
            return
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with path.open("a") as f:
            f.write(f"{prefix}{CLAUDE_IGNORE_LINE}\n")
        print(f"llm-cc: added {CLAUDE_IGNORE_LINE} to {path}", file=sys.stderr)
    except Exception as e:
        print(f"llm-cc: could not update global gitignore: {e}", file=sys.stderr)
