"""Tests for agent health monitoring: error detection, context monitoring, health scoring, sessions."""

import json
import time
from pathlib import Path

import pytest

from llm_cc.health import (
    AgentHealth,
    ContextMonitor,
    DetectedError,
    ErrorDetector,
    HealthScorer,
    SessionContext,
    SessionStore,
    Severity,
)
from llm_cc.models import Task


# --- Error Detection ---


def test_error_detector_scan():
    """Known error patterns are detected."""
    det = ErrorDetector()
    errors = det.scan("Error: permission denied for /etc/shadow")
    assert len(errors) == 1
    assert errors[0].pattern_name == "auth_failure"
    assert errors[0].severity == Severity.HIGH


def test_error_detector_multiple_patterns():
    """Multiple distinct patterns in one scan."""
    det = ErrorDetector()
    text = "permission denied\nbuild failed\nsegfault"
    errors = det.scan(text)
    names = {e.pattern_name for e in errors}
    assert "auth_failure" in names
    assert "build_failure" in names
    assert "agent_crash" in names


def test_error_detector_dedup():
    """Same pattern suppressed within 30s."""
    det = ErrorDetector()
    errors1 = det.scan("permission denied")
    assert len(errors1) == 1
    # Same text again — should be suppressed
    det._last_text = ""  # force rescan
    errors2 = det.scan("permission denied again")
    assert len(errors2) == 0


def test_error_detector_clean():
    """No false positives on clean output."""
    det = ErrorDetector()
    errors = det.scan("All tests passed. Build successful. Deploying to staging...")
    assert len(errors) == 0


def test_error_detector_case_insensitive():
    """Pattern matching is case-insensitive."""
    det = ErrorDetector()
    errors = det.scan("PERMISSION DENIED")
    assert len(errors) == 1
    assert errors[0].pattern_name == "auth_failure"


def test_error_detector_recent_errors():
    """recent_errors filters by 60s window."""
    det = ErrorDetector()
    det.scan("permission denied")
    assert det.active_error_count == 1
    assert det.worst_severity == Severity.HIGH


# --- Context Monitoring ---


def test_context_monitor_parse_percent_of_context():
    """Extracts percentage from 'XX% of context' pattern."""
    mon = ContextMonitor()
    mon.scan("Using 73% of context window")
    assert mon.context_percent == 73
    assert mon.remaining == 27


def test_context_monitor_parse_context_percent():
    """Extracts percentage from 'context...XX%' pattern."""
    mon = ContextMonitor()
    mon.scan("context usage: 85%")
    assert mon.context_percent == 85
    assert mon.remaining == 15


def test_context_monitor_warning_levels():
    """Warning levels: None >30%, warning 10-30%, critical <10%."""
    mon = ContextMonitor()

    # >30% remaining = no warning
    mon.context_percent = 60  # 40% remaining
    assert mon.warning_level is None

    # 10-30% remaining = warning
    mon.context_percent = 80  # 20% remaining
    assert mon.warning_level == "warning"

    # <10% remaining = critical
    mon.context_percent = 95  # 5% remaining
    assert mon.warning_level == "critical"


def test_context_monitor_unknown():
    """No context info = None."""
    mon = ContextMonitor()
    assert mon.remaining is None
    assert mon.warning_level is None


# --- Health Scoring ---


def test_health_scorer_alive():
    """Alive agent with recent output scores high."""
    scorer = HealthScorer()
    scorer.record_output(100)
    h = scorer.compute(alive=True, stable_ticks=0, screen_text="all good")
    assert h.score >= 75
    assert h.label == "healthy"
    assert h.color == "green"


def test_health_scorer_dead():
    """Dead agent loses liveness component."""
    scorer = HealthScorer()
    h = scorer.compute(alive=False, stable_ticks=50, screen_text="")
    assert h.liveness == 0
    # Dead but no errors + neutral activity/responsiveness = degraded range
    assert h.score < 75


def test_health_scorer_with_errors():
    """Errors reduce stability component."""
    scorer = HealthScorer()
    scorer.record_output(100)
    h = scorer.compute(alive=True, stable_ticks=0, screen_text="permission denied for /etc/shadow")
    assert h.stability < 25  # penalized
    assert h.score < 100


def test_health_scorer_idle():
    """High stable_ticks reduce responsiveness."""
    scorer = HealthScorer()
    scorer.record_output(100)
    h = scorer.compute(alive=True, stable_ticks=200, screen_text="idle screen")
    assert h.responsiveness <= 5


def test_health_color_thresholds():
    """Color and label properties at boundary values."""
    assert AgentHealth(score=100, liveness=25, activity=25, stability=25, responsiveness=25).color == "green"
    assert AgentHealth(score=75, liveness=25, activity=25, stability=25, responsiveness=0).color == "green"
    assert AgentHealth(score=50, liveness=25, activity=25, stability=0, responsiveness=0).color == "yellow"
    assert AgentHealth(score=25, liveness=25, activity=0, stability=0, responsiveness=0).color == "dark_orange"
    assert AgentHealth(score=10, liveness=0, activity=0, stability=10, responsiveness=0).color == "red"

    assert AgentHealth(score=80, liveness=25, activity=25, stability=25, responsiveness=5).label == "healthy"
    assert AgentHealth(score=50, liveness=25, activity=25, stability=0, responsiveness=0).label == "degraded"
    assert AgentHealth(score=30, liveness=25, activity=5, stability=0, responsiveness=0).label == "unhealthy"
    assert AgentHealth(score=10, liveness=0, activity=0, stability=10, responsiveness=0).label == "critical"


def test_health_context_color():
    """Context color property."""
    h = AgentHealth(score=75, liveness=25, activity=25, stability=25, responsiveness=0)
    assert h.context_color is None  # unknown

    h.context_remaining = 50
    assert h.context_color == "green"

    h.context_remaining = 20
    assert h.context_color == "yellow"

    h.context_remaining = 5
    assert h.context_color == "red"


# --- Session Ring Buffer ---


def test_session_ring_buffer():
    """Events evicted at maxlen=50."""
    ctx = SessionContext(
        session_id="s1", task_id="t1", stage="execute",
        agent_name="claude", start_time=1000.0,
    )
    for i in range(60):
        ctx.add_event("output", {"text": f"line {i}"})
    assert len(ctx.events) == 50
    # Oldest events (0-9) should be evicted
    assert ctx.events[0].data["text"] == "line 10"


def test_session_serialization():
    """to_dict/from_dict roundtrip."""
    ctx = SessionContext(
        session_id="s1", task_id="t1", stage="execute",
        agent_name="claude", start_time=1000.0,
    )
    ctx.add_event("output", {"text": "hello"})
    ctx.add_event("error", {"pattern": "auth_failure", "severity": "HIGH"})

    d = ctx.to_dict()
    ctx2 = SessionContext.from_dict(d)

    assert ctx2.session_id == "s1"
    assert ctx2.task_id == "t1"
    assert ctx2.stage == "execute"
    assert ctx2.agent_name == "claude"
    assert len(ctx2.events) == 2
    assert ctx2.events[0].type == "output"
    assert ctx2.events[1].data["pattern"] == "auth_failure"


def test_session_store_persistence(tmp_path):
    """Write + read from disk."""
    store = SessionStore(tmp_path / "sessions")
    ctx = store.get_or_create("s1", "t1", "execute", "claude")
    ctx.add_event("output", {"text": "test"})

    # Force write
    store.flush_force("s1")

    # Verify file exists
    session_file = tmp_path / "sessions" / "s1.json"
    assert session_file.exists()

    # Create new store, load from disk
    store2 = SessionStore(tmp_path / "sessions")
    ctx2 = store2.get_or_create("s1", "t1", "execute", "claude")
    assert len(ctx2.events) == 1
    assert ctx2.events[0].data["text"] == "test"


def test_session_needs_write():
    """Debounce: needs_write respects interval."""
    ctx = SessionContext(
        session_id="s1", task_id="t1", stage="execute",
        agent_name="claude", start_time=1000.0,
    )
    # Not dirty initially
    assert not ctx.needs_write()

    # After adding event, dirty
    ctx.add_event("output", {"text": "x"})
    assert ctx.needs_write(interval=0.0)  # 0s interval = always writable

    # After marking written, not dirty
    ctx.mark_written()
    assert not ctx.needs_write()


# --- Handoff Generation ---


def test_handoff_generation():
    """Produces readable markdown with all sections."""
    ctx = SessionContext(
        session_id="s1", task_id="t1", stage="execute",
        agent_name="claude", start_time=1000.0,
    )
    ctx.add_event("output", {"text": "implementing login feature"})
    ctx.add_event("error", {"pattern": "rate_limit", "severity": "MEDIUM"})
    ctx.add_event("health", {"score": 65, "context_remaining": 30, "context_warning": "warning"})

    handoff = ctx.generate_handoff(
        "Add user login",
        verify="curl -X POST /login returns 200",
        done="login works with valid and invalid creds",
    )

    assert "# Handoff: Add user login" in handoff
    assert "Stage: execute" in handoff
    assert "Agent: claude" in handoff
    assert "curl -X POST /login returns 200" in handoff
    assert "login works with valid and invalid creds" in handoff
    assert "rate_limit" in handoff
    assert "30% context remaining" in handoff
    assert "## Recent Activity" in handoff
    assert "## Errors" in handoff


def test_handoff_no_verify_done():
    """Handoff handles None verify/done gracefully."""
    ctx = SessionContext(
        session_id="s1", task_id="t1", stage="planning",
        agent_name="codex", start_time=0.0,
    )
    handoff = ctx.generate_handoff("Some task")
    assert "Not specified" in handoff


# --- Structured Task Fields ---


def test_structured_task_fields():
    """Task model accepts verify/done fields."""
    task = Task(
        title="Add login",
        verify="curl returns 200",
        done="valid/invalid creds work",
    )
    assert task.verify == "curl returns 200"
    assert task.done == "valid/invalid creds work"


def test_structured_task_fields_optional():
    """verify/done default to None (backward compatible)."""
    task = Task(title="Fix bug")
    assert task.verify is None
    assert task.done is None


def test_structured_task_json_roundtrip():
    """verify/done survive JSON serialization."""
    task = Task(title="X", verify="test passes", done="no errors")
    data = json.loads(task.model_dump_json())
    task2 = Task(**data)
    assert task2.verify == "test passes"
    assert task2.done == "no errors"
