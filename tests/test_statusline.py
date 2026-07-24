"""Tests for statusline integration: status file parsing, health, env var injection, script setup."""

import json
import os
from unittest.mock import patch

import pytest

from llm_cc.health import AgentHealth, ContextMonitor, HealthScorer

# --- ContextMonitor.update_from_status ---


def test_update_from_status_sets_context():
    """update_from_status sets context_percent from statusline JSON."""
    mon = ContextMonitor()
    mon.update_from_status({"context_window": {"used_percentage": 45}})
    assert mon.context_percent == 45  # fallback to used_percentage when no raw tokens
    assert mon.remaining == 55


def test_update_from_status_raw_tokens():
    """Computes used% from raw tokens with 64k output reservation."""
    mon = ContextMonitor()
    mon.update_from_status({
        "context_window": {
            "context_window_size": 200_000,
            "used_percentage": 8,  # inaccurate official value — should be ignored
            "current_usage": {
                "input_tokens": 8500,
                "cache_creation_input_tokens": 5000,
                "cache_read_input_tokens": 2000,
            },
        },
    })
    # (8500 + 5000 + 2000) / 200000 = 15500 / 200000 ≈ 7%
    assert mon.context_percent == 7
    assert mon.remaining == 93


def test_update_from_status_ignores_invalid():
    """Out-of-range or missing values are ignored."""
    mon = ContextMonitor()
    mon.update_from_status({"context_window": {"used_percentage": 150}})
    assert mon.context_percent is None

    mon.update_from_status({"context_window": {}})
    assert mon.context_percent is None

    mon.update_from_status({})
    assert mon.context_percent is None


# --- HealthScorer.compute with status_data ---


def test_health_scorer_with_status_data():
    """compute() with status_data extracts current_usage tokens."""
    scorer = HealthScorer()
    scorer.record_output(100)

    status_data = {
        "context_window": {
            "used_percentage": 60,
            "current_usage": {
                "input_tokens": 8500,
                "output_tokens": 3000,
                "cache_creation_input_tokens": 2000,
                "cache_read_input_tokens": 5000,
            },
        },
    }

    h = scorer.compute(alive=True, stable_ticks=0, screen_text="all good", status_data=status_data)

    assert h.input_tokens == 8500
    assert h.output_tokens == 3000
    assert h.cache_creation_tokens == 2000
    assert h.cache_read_tokens == 5000
    assert h.context_remaining == 40  # 100 - 60
    assert h.score >= 75


def test_health_scorer_fallback_no_status():
    """Without status_data, falls back to screen scraping for context."""
    scorer = HealthScorer()
    scorer.record_output(100)

    h = scorer.compute(alive=True, stable_ticks=0, screen_text="context usage: 70%")

    assert h.input_tokens is None
    assert h.output_tokens is None
    assert h.cache_creation_tokens is None
    assert h.cache_read_tokens is None
    assert h.context_remaining == 30  # 100 - 70


def test_health_scorer_status_overrides_screen():
    """status_data context takes priority over screen text context."""
    scorer = HealthScorer()
    scorer.record_output(100)

    status_data = {
        "context_window": {"used_percentage": 50},
    }
    # Screen says 90% but statusline says 50%
    h = scorer.compute(alive=True, stable_ticks=0, screen_text="context: 90%", status_data=status_data)
    assert h.context_remaining == 50  # from statusline, not screen


def test_health_scorer_no_current_usage():
    """current_usage can be null (before first API call)."""
    scorer = HealthScorer()
    scorer.record_output(100)

    status_data = {
        "context_window": {
            "used_percentage": 30,
            "current_usage": None,
        },
    }

    h = scorer.compute(alive=True, stable_ticks=0, screen_text="", status_data=status_data)
    assert h.input_tokens is None
    assert h.output_tokens is None
    assert h.cache_creation_tokens is None
    assert h.cache_read_tokens is None


# --- AgentHealth fields ---


def test_agent_health_token_defaults():
    """Token fields default to None."""
    h = AgentHealth(score=75, liveness=25, activity=25, stability=25, responsiveness=0)
    assert h.input_tokens is None
    assert h.output_tokens is None
    assert h.cache_creation_tokens is None
    assert h.cache_read_tokens is None


def test_agent_health_token_fields_set():
    """Token fields can be set."""
    h = AgentHealth(
        score=75, liveness=25, activity=25, stability=25, responsiveness=0,
        input_tokens=8500, output_tokens=2000,
        cache_creation_tokens=1000, cache_read_tokens=3000,
    )
    assert h.input_tokens == 8500
    assert h.output_tokens == 2000
    assert h.cache_creation_tokens == 1000
    assert h.cache_read_tokens == 3000


# --- TmuxBackend env var injection ---


@pytest.fixture
def patched_tmux():
    """Patch tmux invocations so tests don't actually spawn sessions."""
    async def fake_tmux(*args, **kwargs):
        return 0, b"", b""

    def fake_tmux_sync(*args, **kwargs):
        return 0, b""

    with patch("llm_cc.agents._tmux", side_effect=fake_tmux) as a_mock, \
         patch("llm_cc.agents._tmux_sync", side_effect=fake_tmux_sync):
        yield a_mock


async def test_env_var_injected(tmp_path, patched_tmux):
    """tmux new-session args include LLM_CC_TASK_ID."""
    from llm_cc.agents import TmuxBackend
    from llm_cc.models import AgentConfig, Task

    backend = TmuxBackend()
    task = Task(title="Test task")
    config = AgentConfig(name="test", command="echo", args_template="{prompt}")

    session_id = await backend.start(config, task, "test prompt", tmp_path)

    new_session_calls = [c for c in patched_tmux.call_args_list if c.args[0] == "new-session"]
    assert new_session_calls, "expected a new-session invocation"
    args = new_session_calls[0].args
    assert f"LLM_CC_TASK_ID={task.id}" in args

    await backend.stop(session_id)


async def test_status_file_tracked(tmp_path, patched_tmux):
    """Status file path is stored on start and removed on stop."""
    from llm_cc.agents import TmuxBackend
    from llm_cc.models import AgentConfig, Task

    backend = TmuxBackend()
    task = Task(title="Test task")
    config = AgentConfig(name="test", command="echo", args_template="{prompt}")

    session_id = await backend.start(config, task, "test", tmp_path)

    expected_path = tmp_path / ".llm-cc" / "status" / f"{task.id}.json"
    assert backend._status_files[session_id] == expected_path

    expected_path.parent.mkdir(parents=True, exist_ok=True)
    expected_path.write_text('{"test": true}')
    assert expected_path.exists()

    await backend.stop(session_id)

    assert session_id not in backend._status_files
    assert not expected_path.exists()


async def test_read_status_file(tmp_path, patched_tmux):
    """_read_status_file returns parsed JSON from status file."""
    from llm_cc.agents import TmuxBackend
    from llm_cc.models import AgentConfig, Task

    backend = TmuxBackend()
    task = Task(title="Test task")
    config = AgentConfig(name="test", command="echo", args_template="{prompt}")

    session_id = await backend.start(config, task, "test", tmp_path)

    assert backend._read_status_file(session_id) is None

    status_file = backend._status_files[session_id]
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_data = {
        "context_window": {
            "used_percentage": 45,
            "total_output_tokens": 1000,
            "current_usage": {
                "input_tokens": 5000,
                "cache_creation_input_tokens": 500,
                "cache_read_input_tokens": 2000,
            },
        },
    }
    status_file.write_text(json.dumps(status_data))

    result = backend._read_status_file(session_id)
    assert result == status_data

    await backend.stop(session_id)


async def test_status_data_public_method(tmp_path, patched_tmux):
    """status_data() provides public access to statusline JSON."""
    from llm_cc.agents import TmuxBackend
    from llm_cc.models import AgentConfig, Task

    backend = TmuxBackend()
    task = Task(title="Test task")
    config = AgentConfig(name="test", command="echo", args_template="{prompt}")

    session_id = await backend.start(config, task, "test", tmp_path)

    assert backend.status_data(session_id) is None

    status_file = backend._status_files[session_id]
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text('{"context_window": {"used_percentage": 30}}')

    result = backend.status_data(session_id)
    assert result["context_window"]["used_percentage"] == 30

    await backend.stop(session_id)


# --- Statusline script setup ---


def test_statusline_script_setup(tmp_path):
    """Statusline script is written and global settings.local.json configured."""
    from llm_cc.statusline import STATUSLINE_SCRIPT, setup_statusline

    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch("pathlib.Path.home", return_value=fake_home):
        setup_statusline(tmp_path)

        # Script written to project
        script_path = tmp_path / ".llm-cc" / "bin" / "statusline.py"
        assert script_path.exists()
        assert script_path.read_text() == STATUSLINE_SCRIPT
        assert os.access(script_path, os.X_OK)

        # Settings written to global ~/.claude/
        settings_path = fake_home / ".claude" / "settings.local.json"
        assert settings_path.exists()
        settings = json.loads(settings_path.read_text())
        assert "statusLine" in settings
        assert settings["statusLine"]["type"] == "command"
        assert "statusline.py" in settings["statusLine"]["command"]


def test_statusline_settings_not_overwritten(tmp_path):
    """Non-llm-cc statusLine config is preserved."""
    from llm_cc.statusline import setup_statusline

    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch("pathlib.Path.home", return_value=fake_home):
        # Pre-existing global settings with a custom (non-llm-cc) statusLine
        settings_path = fake_home / ".claude" / "settings.local.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {"statusLine": {"type": "command", "command": "my-custom-script"}}
        settings_path.write_text(json.dumps(existing))

        setup_statusline(tmp_path)

        # Custom command should NOT be overwritten
        settings = json.loads(settings_path.read_text())
        assert settings["statusLine"]["command"] == "my-custom-script"


def test_statusline_settings_updates_own_path(tmp_path):
    """Our own statusLine command is updated when script path changes."""
    from llm_cc.statusline import setup_statusline

    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch("pathlib.Path.home", return_value=fake_home):
        # Existing settings pointing to an old llm-cc script path
        settings_path = fake_home / ".claude" / "settings.local.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        old = {"statusLine": {"type": "command", "command": "python3 /old/path/.llm-cc/bin/statusline.py"}}
        settings_path.write_text(json.dumps(old))

        setup_statusline(tmp_path)

        # Should be updated to current path
        settings = json.loads(settings_path.read_text())
        assert str(tmp_path) in settings["statusLine"]["command"]
