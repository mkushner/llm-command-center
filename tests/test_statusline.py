"""Tests for statusline integration: status file parsing, health, env var injection, script setup."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from llm_cc.health import AgentHealth, ContextMonitor, HealthScorer


# --- ContextMonitor.update_from_status ---


def test_update_from_status_sets_context():
    """update_from_status sets context_percent from statusline JSON."""
    mon = ContextMonitor()
    mon.update_from_status({"context_window": {"used_percentage": 45}})
    assert mon.context_percent == 45
    assert mon.remaining == 55


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


# --- PtyBackend env var injection ---


def test_env_var_injected(tmp_path):
    """spawn_env includes LLM_CC_TASK_ID after start()."""
    from llm_cc.agents import PtyBackend
    from llm_cc.models import AgentConfig, Task

    backend = PtyBackend()
    task = Task(title="Test task")
    config = AgentConfig(name="test", command="echo", args_template="{prompt}")

    with patch("pexpect.spawn") as mock_spawn:
        mock_child = MagicMock()
        mock_child.isalive.return_value = True
        mock_spawn.return_value = mock_child

        import asyncio
        session_id = asyncio.get_event_loop().run_until_complete(
            backend.start(config, task, "test prompt", tmp_path)
        )

        call_kwargs = mock_spawn.call_args
        env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env", {})
        assert env.get("LLM_CC_TASK_ID") == task.id

        mock_child.isalive.return_value = False
        asyncio.get_event_loop().run_until_complete(backend.stop(session_id))


# --- Status file tracking ---


def test_status_file_tracked(tmp_path):
    """Status file path is stored on start and removed on stop."""
    from llm_cc.agents import PtyBackend
    from llm_cc.models import AgentConfig, Task

    backend = PtyBackend()
    task = Task(title="Test task")
    config = AgentConfig(name="test", command="echo", args_template="{prompt}")

    with patch("pexpect.spawn") as mock_spawn:
        mock_child = MagicMock()
        mock_child.isalive.return_value = True
        mock_spawn.return_value = mock_child

        import asyncio
        session_id = asyncio.get_event_loop().run_until_complete(
            backend.start(config, task, "test", tmp_path)
        )

        expected_path = tmp_path / ".llm-cc" / "status" / f"{task.id}.json"
        assert backend._status_files[session_id] == expected_path

        expected_path.parent.mkdir(parents=True, exist_ok=True)
        expected_path.write_text('{"test": true}')
        assert expected_path.exists()

        mock_child.isalive.return_value = False
        asyncio.get_event_loop().run_until_complete(backend.stop(session_id))

        assert session_id not in backend._status_files
        assert not expected_path.exists()


# --- Status file reading ---


def test_read_status_file(tmp_path):
    """_read_status_file returns parsed JSON from status file."""
    from llm_cc.agents import PtyBackend
    from llm_cc.models import AgentConfig, Task

    backend = PtyBackend()
    task = Task(title="Test task")
    config = AgentConfig(name="test", command="echo", args_template="{prompt}")

    with patch("pexpect.spawn") as mock_spawn:
        mock_child = MagicMock()
        mock_child.isalive.return_value = True
        mock_spawn.return_value = mock_child

        import asyncio
        session_id = asyncio.get_event_loop().run_until_complete(
            backend.start(config, task, "test", tmp_path)
        )

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

        mock_child.isalive.return_value = False
        asyncio.get_event_loop().run_until_complete(backend.stop(session_id))


def test_status_data_public_method(tmp_path):
    """status_data() provides public access to statusline JSON."""
    from llm_cc.agents import PtyBackend
    from llm_cc.models import AgentConfig, Task

    backend = PtyBackend()
    task = Task(title="Test task")
    config = AgentConfig(name="test", command="echo", args_template="{prompt}")

    with patch("pexpect.spawn") as mock_spawn:
        mock_child = MagicMock()
        mock_child.isalive.return_value = True
        mock_spawn.return_value = mock_child

        import asyncio
        session_id = asyncio.get_event_loop().run_until_complete(
            backend.start(config, task, "test", tmp_path)
        )

        # No file yet
        assert backend.status_data(session_id) is None

        # Write status file
        status_file = backend._status_files[session_id]
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text('{"context_window": {"used_percentage": 30}}')

        result = backend.status_data(session_id)
        assert result["context_window"]["used_percentage"] == 30

        mock_child.isalive.return_value = False
        asyncio.get_event_loop().run_until_complete(backend.stop(session_id))


# --- Statusline script setup ---


def test_statusline_script_setup(tmp_path):
    """Statusline script is written and global settings.local.json configured."""
    from llm_cc.app import CommandCenterApp, _STATUSLINE_SCRIPT

    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch.object(CommandCenterApp, "__init__", lambda self, *a, **kw: None), \
         patch("pathlib.Path.home", return_value=fake_home):
        app = CommandCenterApp.__new__(CommandCenterApp)
        app.project_path = tmp_path

        app._setup_statusline()

        # Script written to project
        script_path = tmp_path / ".llm-cc" / "bin" / "statusline.py"
        assert script_path.exists()
        assert script_path.read_text() == _STATUSLINE_SCRIPT
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
    from llm_cc.app import CommandCenterApp

    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch.object(CommandCenterApp, "__init__", lambda self, *a, **kw: None), \
         patch("pathlib.Path.home", return_value=fake_home):
        app = CommandCenterApp.__new__(CommandCenterApp)
        app.project_path = tmp_path

        # Pre-existing global settings with a custom (non-llm-cc) statusLine
        settings_path = fake_home / ".claude" / "settings.local.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {"statusLine": {"type": "command", "command": "my-custom-script"}}
        settings_path.write_text(json.dumps(existing))

        app._setup_statusline()

        # Custom command should NOT be overwritten
        settings = json.loads(settings_path.read_text())
        assert settings["statusLine"]["command"] == "my-custom-script"


def test_statusline_settings_updates_own_path(tmp_path):
    """Our own statusLine command is updated when script path changes."""
    from llm_cc.app import CommandCenterApp

    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch.object(CommandCenterApp, "__init__", lambda self, *a, **kw: None), \
         patch("pathlib.Path.home", return_value=fake_home):
        app = CommandCenterApp.__new__(CommandCenterApp)
        app.project_path = tmp_path

        # Existing settings pointing to an old llm-cc script path
        settings_path = fake_home / ".claude" / "settings.local.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        old = {"statusLine": {"type": "command", "command": "python3 /old/path/.llm-cc/bin/statusline.py"}}
        settings_path.write_text(json.dumps(old))

        app._setup_statusline()

        # Should be updated to current path
        settings = json.loads(settings_path.read_text())
        assert str(tmp_path) in settings["statusLine"]["command"]
