"""Agent health monitoring: error detection, context tracking, health scoring, session persistence."""

from __future__ import annotations

import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path


# --- Error Detection ---


class Severity(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


@dataclass
class DetectedError:
    pattern_name: str
    severity: Severity
    timestamp: float  # time.monotonic()
    matched_text: str


# (pattern_name, severity, trigger_substrings)
_ERROR_PATTERNS: list[tuple[str, Severity, list[str]]] = [
    ("auth_failure", Severity.HIGH, [
        "permission denied", "authentication failed", "unauthorized", "api key invalid",
    ]),
    ("rate_limit", Severity.MEDIUM, [
        "rate limit", "too many requests", "429", "quota exceeded",
    ]),
    ("token_overflow", Severity.HIGH, [
        "context too long", "token limit", "max tokens", "maximum context length",
    ]),
    ("plan_stuck", Severity.LOW, [
        "what should i do", "i'm not sure what", "please clarify",
    ]),
    ("build_failure", Severity.MEDIUM, [
        "build failed", "compilation error", "npm err",
    ]),
    ("test_failure", Severity.MEDIUM, [
        "tests failed", "assertion error",
    ]),
    ("git_conflict", Severity.HIGH, [
        "merge conflict", "conflict in",
    ]),
    ("agent_crash", Severity.CRITICAL, [
        "segfault", "panic:", "fatal error", "unhandled exception",
    ]),
    ("permission_wait", Severity.LOW, [
        "approve this action", "requires approval",
    ]),
]


class ErrorDetector:
    """Scans screen text for known error patterns with deduplication."""

    def __init__(self) -> None:
        self._last_text: str = ""
        self._errors: list[DetectedError] = []
        self._last_seen: dict[str, float] = {}  # pattern_name -> last trigger time

    def scan(self, screen_text: str) -> list[DetectedError]:
        """Scan text for errors. Short-circuits if text unchanged. Deduplicates within 30s."""
        if screen_text == self._last_text:
            return []
        self._last_text = screen_text

        now = time.monotonic()
        new_errors: list[DetectedError] = []
        lower = screen_text.lower()

        for name, severity, triggers in _ERROR_PATTERNS:
            for trigger in triggers:
                if trigger in lower:
                    # Dedup: skip if same pattern fired within 30s
                    last = self._last_seen.get(name, 0.0)
                    if now - last < 30.0:
                        break
                    err = DetectedError(name, severity, now, trigger)
                    new_errors.append(err)
                    self._errors.append(err)
                    self._last_seen[name] = now
                    break  # one match per pattern

        return new_errors

    @property
    def recent_errors(self) -> list[DetectedError]:
        """All errors detected in the last 60 seconds."""
        cutoff = time.monotonic() - 60.0
        return [e for e in self._errors if e.timestamp > cutoff]

    @property
    def active_error_count(self) -> int:
        return len(self.recent_errors)

    @property
    def worst_severity(self) -> Severity | None:
        recent = self.recent_errors
        if not recent:
            return None
        return max(e.severity for e in recent)


# --- Context Monitoring ---


_CONTEXT_PATTERN = re.compile(r"(\d{1,3})%\s*(?:of\s+)?context|context[^\n]*?(\d{1,3})%")


class ContextMonitor:
    """Parses agent output for context/token usage indicators."""

    def __init__(self) -> None:
        self.context_percent: int | None = None  # used percent, None if not detected

    def scan(self, screen_text: str) -> None:
        """Extract context usage percentage from screen text."""
        for m in _CONTEXT_PATTERN.finditer(screen_text.lower()):
            pct_str = m.group(1) or m.group(2)
            if pct_str:
                pct = int(pct_str)
                if 0 <= pct <= 100:
                    self.context_percent = pct

    @property
    def remaining(self) -> int | None:
        """Percent of context remaining, None if unknown."""
        if self.context_percent is None:
            return None
        return 100 - self.context_percent

    @property
    def warning_level(self) -> str | None:
        """None if OK or unknown, 'warning' if 10-30% remaining, 'critical' if <10%."""
        r = self.remaining
        if r is None:
            return None
        if r < 10:
            return "critical"
        if r <= 30:
            return "warning"
        return None


# --- Health Scoring ---


@dataclass
class AgentHealth:
    score: int  # 0-100 composite
    liveness: int  # 0-25
    activity: int  # 0-25
    stability: int  # 0-25
    responsiveness: int  # 0-25
    context_remaining: int | None = None  # percent remaining, None if unknown
    errors: list[DetectedError] = field(default_factory=list)

    @property
    def color(self) -> str:
        if self.score >= 75:
            return "green"
        if self.score >= 50:
            return "yellow"
        if self.score >= 25:
            return "dark_orange"
        return "red"

    @property
    def label(self) -> str:
        if self.score >= 75:
            return "healthy"
        if self.score >= 50:
            return "degraded"
        if self.score >= 25:
            return "unhealthy"
        return "critical"

    @property
    def context_color(self) -> str | None:
        if self.context_remaining is None:
            return None
        if self.context_remaining > 30:
            return "green"
        if self.context_remaining >= 10:
            return "yellow"
        return "red"


class HealthScorer:
    """Computes composite health score for an agent session."""

    def __init__(self) -> None:
        self.error_detector = ErrorDetector()
        self.context_monitor = ContextMonitor()
        self._total_bytes: int = 0
        self._last_output_time: float = 0.0

    def record_output(self, data_len: int) -> None:
        """Called when PTY data arrives."""
        self._total_bytes += data_len
        self._last_output_time = time.monotonic()

    def compute(self, alive: bool, stable_ticks: int, screen_text: str) -> AgentHealth:
        """Compute health score from current state."""
        # Run detectors
        self.error_detector.scan(screen_text)
        self.context_monitor.scan(screen_text)

        now = time.monotonic()

        # Liveness (0-25): alive = 25, dead = 0
        liveness = 25 if alive else 0

        # Activity (0-25): 25 if output <5s ago, scaling to 5 if >60s
        if self._last_output_time == 0.0:
            activity = 15  # no output yet, neutral
        else:
            age = now - self._last_output_time
            if age < 5:
                activity = 25
            elif age > 60:
                activity = 5
            else:
                # Linear scale: 25 at 5s -> 5 at 60s
                activity = int(25 - (age - 5) * 20 / 55)

        # Stability (0-25): 25 minus severity-weighted penalties
        stability = 25
        for err in self.error_detector.recent_errors:
            stability -= int(err.severity) * 3
        stability = max(0, stability)

        # Responsiveness (0-25): 25 if stable_ticks <3, scaling to 5 if >100
        if stable_ticks < 3:
            responsiveness = 25
        elif stable_ticks > 100:
            responsiveness = 5
        else:
            responsiveness = int(25 - (stable_ticks - 3) * 20 / 97)

        score = liveness + activity + stability + responsiveness

        return AgentHealth(
            score=score,
            liveness=liveness,
            activity=activity,
            stability=stability,
            responsiveness=responsiveness,
            context_remaining=self.context_monitor.remaining,
            errors=self.error_detector.recent_errors,
        )


# --- Session Ring Buffer ---


@dataclass
class SessionEvent:
    type: str  # "output", "error", "health", "stage"
    timestamp: float
    data: dict


@dataclass
class SessionContext:
    session_id: str
    task_id: str
    stage: str
    agent_name: str
    start_time: float
    events: deque = field(default_factory=lambda: deque(maxlen=50))
    _dirty: bool = field(default=False, repr=False)
    _last_write: float = field(default=0.0, repr=False)

    def add_event(self, event_type: str, data: dict) -> None:
        self.events.append(SessionEvent(event_type, time.monotonic(), data))
        self._dirty = True

    def needs_write(self, interval: float = 3.0) -> bool:
        """True if dirty AND enough time since last write."""
        return self._dirty and (time.monotonic() - self._last_write) >= interval

    def mark_written(self) -> None:
        self._dirty = False
        self._last_write = time.monotonic()

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "stage": self.stage,
            "agent_name": self.agent_name,
            "start_time": self.start_time,
            "events": [
                {"type": e.type, "timestamp": e.timestamp, "data": e.data}
                for e in self.events
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> SessionContext:
        ctx = cls(
            session_id=d["session_id"],
            task_id=d["task_id"],
            stage=d["stage"],
            agent_name=d["agent_name"],
            start_time=d["start_time"],
        )
        for ev in d.get("events", []):
            ctx.events.append(SessionEvent(ev["type"], ev["timestamp"], ev["data"]))
        return ctx

    def generate_handoff(self, task_title: str, verify: str | None = None, done: str | None = None) -> str:
        """Produce human-readable markdown handoff file."""
        lines = [
            f"# Handoff: {task_title}",
            "",
            "## Position",
            f"- Stage: {self.stage}",
            f"- Agent: {self.agent_name}",
            f"- Started: {self.start_time:.0f}",
            "",
            "## Verify",
            verify or "Not specified",
            "",
            "## Done When",
            done or "Not specified",
            "",
            "## Recent Activity",
        ]

        for ev in self.events:
            ts = f"{ev.timestamp:.1f}"
            ev_type = ev.type.upper()
            summary = ""
            if ev.type == "output":
                text = ev.data.get("text", "")
                summary = text[-80:] if len(text) > 80 else text
            elif ev.type == "error":
                summary = ev.data.get("pattern", "unknown")
            elif ev.type == "health":
                summary = f"{ev.data.get('score', '?')}/100"
            elif ev.type == "stage":
                summary = ev.data.get("action", "")
            lines.append(f"- [{ts}] {ev_type}: {summary}")

        # Context info
        ctx_remaining = None
        ctx_warning = None
        for ev in reversed(list(self.events)):
            if ev.type == "health":
                ctx_remaining = ev.data.get("context_remaining")
                ctx_warning = ev.data.get("context_warning")
                break

        lines.extend(["", "## Context"])
        if ctx_remaining is not None:
            lines.append(f"{ctx_remaining}% context remaining — {ctx_warning or 'OK'}")
        else:
            lines.append("Context usage unknown")

        # Errors
        error_events = [ev for ev in self.events if ev.type == "error"]
        if error_events:
            lines.extend(["", "## Errors"])
            for ev in error_events:
                sev = ev.data.get("severity", "?")
                pat = ev.data.get("pattern", "unknown")
                lines.append(f"- {pat} ({sev}) at {ev.timestamp:.1f}")
        return "\n".join(lines)


# --- Session Store ---


class SessionStore:
    """Manages session contexts with debounced disk persistence."""

    def __init__(self, sessions_dir: Path) -> None:
        self._dir = sessions_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._contexts: dict[str, SessionContext] = {}

    def get_or_create(
        self, session_id: str, task_id: str, stage: str, agent_name: str,
    ) -> SessionContext:
        """Get existing context or create new. Loads from disk if file exists."""
        if session_id in self._contexts:
            return self._contexts[session_id]

        # Try loading from disk
        path = self._dir / f"{session_id}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text())
                ctx = SessionContext.from_dict(data)
                self._contexts[session_id] = ctx
                return ctx
            except Exception:
                pass

        ctx = SessionContext(
            session_id=session_id,
            task_id=task_id,
            stage=stage,
            agent_name=agent_name,
            start_time=time.monotonic(),
        )
        self._contexts[session_id] = ctx
        return ctx

    def get(self, session_id: str) -> SessionContext | None:
        return self._contexts.get(session_id)

    def flush(self, session_id: str) -> None:
        """Write session to disk if dirty and debounce interval passed."""
        ctx = self._contexts.get(session_id)
        if not ctx or not ctx.needs_write():
            return
        self._write(ctx)

    def flush_force(self, session_id: str) -> None:
        """Write session to disk unconditionally."""
        ctx = self._contexts.get(session_id)
        if not ctx:
            return
        self._write(ctx)

    def flush_all(self) -> None:
        """Write all dirty contexts. Called on shutdown."""
        for ctx in self._contexts.values():
            if ctx._dirty:
                self._write(ctx)

    def remove(self, session_id: str) -> SessionContext | None:
        """Remove context from memory (after flushing)."""
        return self._contexts.pop(session_id, None)

    def _write(self, ctx: SessionContext) -> None:
        path = self._dir / f"{ctx.session_id}.json"
        try:
            path.write_text(json.dumps(ctx.to_dict(), indent=2))
            ctx.mark_written()
        except Exception:
            pass
