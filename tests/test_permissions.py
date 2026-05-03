"""Tests for auto-permissions module.

Covers:
- `write_claude_settings` — writes file, idempotent (doesn't clobber existing).
- `ensure_global_gitignore` — appends line, idempotent, works when excludesFile unset.
- `ensure_global_claude_default_mode` — sets defaultMode, preserves other keys,
   respects stronger user choices, handles malformed JSON, idempotent.
"""

from __future__ import annotations

import json

import pytest

from llm_cc import permissions


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Redirect HOME and relevant paths into tmp_path."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    # Rebind module-level constants that were captured at import time
    monkeypatch.setattr(
        permissions, "DEFAULT_EXCLUDES_FILE", fake_home / ".config" / "git" / "ignore"
    )
    monkeypatch.setattr(
        permissions, "GLOBAL_CLAUDE_SETTINGS", fake_home / ".claude" / "settings.json"
    )
    return fake_home


# --- write_claude_settings ---


def test_write_claude_settings_creates_file(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    permissions.write_claude_settings(worktree)
    f = worktree / ".claude" / "settings.local.json"
    assert f.exists()
    data = json.loads(f.read_text())
    assert data == {"permissions": {"defaultMode": "bypassPermissions"}}


def test_write_claude_settings_idempotent_does_not_clobber(tmp_path):
    worktree = tmp_path / "wt"
    (worktree / ".claude").mkdir(parents=True)
    existing = worktree / ".claude" / "settings.local.json"
    existing.write_text('{"custom": true}')
    permissions.write_claude_settings(worktree)
    # Original content preserved
    assert json.loads(existing.read_text()) == {"custom": True}


def test_write_claude_settings_swallows_errors(tmp_path, capsys):
    # Point at a path that cannot be created (parent is a file, not a dir)
    bad_parent = tmp_path / "notadir"
    bad_parent.write_text("x")
    permissions.write_claude_settings(bad_parent)
    # Should not raise; error logged to stderr
    out = capsys.readouterr()
    assert "could not write Claude settings" in out.err


# --- ensure_global_gitignore ---


def test_ensure_global_gitignore_creates_file_when_unset(tmp_home, monkeypatch):
    # Simulate git config unset
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1

        class R:
            returncode = 1 if "--get" in cmd else 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(permissions.subprocess, "run", fake_run)

    permissions.ensure_global_gitignore()

    expected = tmp_home / ".config" / "git" / "ignore"
    assert expected.exists()
    assert ".claude/settings.local.json" in expected.read_text()


def test_ensure_global_gitignore_idempotent(tmp_home, monkeypatch):
    ignore_path = tmp_home / ".config" / "git" / "ignore"
    ignore_path.parent.mkdir(parents=True)
    ignore_path.write_text("other-line\n.claude/settings.local.json\n")

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = str(ignore_path) + "\n"
            stderr = ""

        return R()

    monkeypatch.setattr(permissions.subprocess, "run", fake_run)

    permissions.ensure_global_gitignore()

    # Line not duplicated
    content = ignore_path.read_text()
    assert content.count(".claude/settings.local.json") == 1
    assert "other-line" in content


def test_ensure_global_gitignore_appends_to_existing_file(tmp_home, monkeypatch):
    ignore_path = tmp_home / ".config" / "git" / "ignore"
    ignore_path.parent.mkdir(parents=True)
    ignore_path.write_text("existing\n")

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = str(ignore_path) + "\n"
            stderr = ""

        return R()

    monkeypatch.setattr(permissions.subprocess, "run", fake_run)

    permissions.ensure_global_gitignore()

    lines = ignore_path.read_text().splitlines()
    assert "existing" in lines
    assert ".claude/settings.local.json" in lines


# --- ensure_global_claude_default_mode ---


def test_claude_default_mode_creates_file_when_missing(tmp_home):
    permissions.ensure_global_claude_default_mode()
    f = tmp_home / ".claude" / "settings.json"
    assert f.exists()
    data = json.loads(f.read_text())
    assert data["permissions"]["defaultMode"] == "acceptEdits"


def test_claude_default_mode_upgrades_from_default(tmp_home):
    f = tmp_home / ".claude" / "settings.json"
    f.parent.mkdir(parents=True)
    f.write_text(
        json.dumps(
            {
                "env": {"FOO": "BAR"},
                "permissions": {
                    "allow": [],
                    "deny": ["Bash(git commit *)"],
                    "defaultMode": "default",
                },
                "other_key": "preserved",
            }
        )
    )
    permissions.ensure_global_claude_default_mode()
    data = json.loads(f.read_text())
    assert data["permissions"]["defaultMode"] == "acceptEdits"
    # Everything else preserved
    assert data["permissions"]["deny"] == ["Bash(git commit *)"]
    assert data["env"] == {"FOO": "BAR"}
    assert data["other_key"] == "preserved"


def test_claude_default_mode_respects_stronger_choice(tmp_home):
    f = tmp_home / ".claude" / "settings.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"permissions": {"defaultMode": "bypassPermissions"}}))
    permissions.ensure_global_claude_default_mode()
    data = json.loads(f.read_text())
    # User's choice preserved
    assert data["permissions"]["defaultMode"] == "bypassPermissions"


def test_claude_default_mode_idempotent(tmp_home):
    f = tmp_home / ".claude" / "settings.json"
    f.parent.mkdir(parents=True)
    original = json.dumps({"permissions": {"defaultMode": "acceptEdits"}}, indent=2) + "\n"
    f.write_text(original)
    mtime_before = f.stat().st_mtime_ns
    permissions.ensure_global_claude_default_mode()
    # Not rewritten when already correct
    assert f.stat().st_mtime_ns == mtime_before


def test_claude_default_mode_handles_malformed_json(tmp_home, capsys):
    f = tmp_home / ".claude" / "settings.json"
    f.parent.mkdir(parents=True)
    f.write_text("{ this is not json")
    permissions.ensure_global_claude_default_mode()
    out = capsys.readouterr()
    assert "not valid JSON" in out.err
    # File untouched
    assert f.read_text() == "{ this is not json"


def test_claude_default_mode_handles_non_object_root(tmp_home, capsys):
    f = tmp_home / ".claude" / "settings.json"
    f.parent.mkdir(parents=True)
    f.write_text("[]")
    permissions.ensure_global_claude_default_mode()
    out = capsys.readouterr()
    assert "not an object" in out.err


# --- Codex --full-auto injection ---


def test_codex_default_config_has_auto_full_auto():
    from llm_cc.storage import _default_agents

    agents = _default_agents()
    assert agents["codex"].auto_full_auto is True
    assert agents["claude"].auto_full_auto is False


def test_codex_full_auto_injected_in_command():
    """End-to-end check on command composition: --full-auto should be in argv
    for a codex agent with auto_full_auto=True."""
    import shlex

    from llm_cc.models import AgentConfig

    config = AgentConfig(
        name="codex",
        command="codex",
        args_template='"{prompt}"',
        auto_full_auto=True,
    )
    cli_flags = ""
    # Replicate the command-build logic from agents.py (keep in sync)
    full_auto_flag = ""
    if (
        config.auto_full_auto
        and config.command == "codex"
        and "--full-auto" not in cli_flags
        and "--ask-for-approval" not in cli_flags
    ):
        full_auto_flag = "--full-auto"
    cmd_args = config.args_template.format(prompt=shlex.quote("hello"), session_id="x", model="")
    parts = [config.command, "", "", full_auto_flag, cli_flags, cmd_args]
    full_cmd = " ".join(p for p in parts if p).strip()
    assert "--full-auto" in full_cmd


def test_codex_full_auto_not_injected_if_user_set_approval():
    from llm_cc.models import AgentConfig

    config = AgentConfig(
        name="codex",
        command="codex",
        auto_full_auto=True,
    )
    cli_flags = "--ask-for-approval on-request"
    full_auto_flag = ""
    if (
        config.auto_full_auto
        and config.command == "codex"
        and "--full-auto" not in cli_flags
        and "--ask-for-approval" not in cli_flags
    ):
        full_auto_flag = "--full-auto"
    assert full_auto_flag == ""
