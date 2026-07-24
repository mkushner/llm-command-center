"""Tests for OutputBuffer.appears_waiting — the input-detection heuristic."""

from llm_cc.agents import STAGE_COMPLETE_MARKER, OutputBuffer
from llm_cc.pipeline import _DONE_INSTRUCTION


def _tick(buf: OutputBuffer, n: int = 1) -> None:
    """Simulate n poll ticks (~0.1s each)."""
    for _ in range(n):
        buf.mark_idle()


def test_not_waiting_initially():
    buf = OutputBuffer()
    _tick(buf, 5)
    assert not buf.appears_waiting


def test_detects_permission_prompt():
    """Screen shows a pattern and is stable → waiting."""
    buf = OutputBuffer()
    buf.append("Allow this action? (y)es/(n)o\r\n")
    _tick(buf, 5)
    assert buf.appears_waiting


def test_clears_after_user_input_and_agent_resumes():
    """THE BUG: after user answers a prompt and agent starts producing
    output, 'waiting' must clear even if the old prompt text is still
    visible on the 40-row screen.
    """
    buf = OutputBuffer()

    # Agent shows permission prompt
    buf.append("Allow this action? (y)es/(n)o\r\n")
    _tick(buf, 5)
    assert buf.appears_waiting, "should detect waiting"

    # User sends 'y', agent starts producing output lines
    buf.append("y\r\n")
    buf.append("Working on task...\r\n")
    # Screen changed → stable_ticks resets to 0
    _tick(buf)
    assert not buf.appears_waiting, "should clear once agent is producing output"


def test_clears_when_pattern_scrolls_off():
    """Pattern scrolls off the 40-row screen → not waiting."""
    buf = OutputBuffer()
    buf.append("Allow? (y)es/(n)o\r\n")
    _tick(buf, 5)
    assert buf.appears_waiting

    # Push the prompt off screen with 50 lines of output
    for i in range(50):
        buf.append(f"output line {i}\r\n")
    _tick(buf, 5)
    assert not buf.appears_waiting


def test_stays_waiting_while_stable_with_pattern():
    """Screen stable + pattern present → stays waiting (agent is idle)."""
    buf = OutputBuffer()
    buf.append("What would you like to do?\r\n")
    _tick(buf, 20)
    assert buf.appears_waiting


def test_not_waiting_during_active_output():
    """Screen constantly changing → not waiting, even if pattern text appears."""
    buf = OutputBuffer()
    for i in range(10):
        buf.append(f"Generating y/n decision tree line {i}\r\n")
        _tick(buf)
    # Pattern "y/n" is on screen but content is changing
    assert not buf.appears_waiting


def test_re_enters_waiting_on_next_prompt():
    """After clearing, a new stable prompt triggers waiting again."""
    buf = OutputBuffer()

    # First prompt
    buf.append("Allow? (y)es/(n)o\r\n")
    _tick(buf, 5)
    assert buf.appears_waiting

    # Agent resumes
    buf.append("Done with that.\r\n")
    _tick(buf)
    assert not buf.appears_waiting

    # New prompt appears and screen stabilizes
    buf.append("Press enter to confirm\r\n")
    _tick(buf, 5)
    assert buf.appears_waiting


# --- Stage completion vs waiting ---


def test_stage_complete_detected():
    """EXECUTE COMPLETE triggers stage_complete, not waiting."""
    buf = OutputBuffer()
    buf.append("All done.\r\nEXECUTE COMPLETE\r\n")
    _tick(buf, 5)
    assert buf.appears_stage_complete
    assert not buf.appears_waiting


def test_planning_complete_detected():
    buf = OutputBuffer()
    buf.append("PLANNING COMPLETE\r\n")
    _tick(buf, 5)
    assert buf.appears_stage_complete
    assert not buf.appears_waiting


def test_review_complete_detected():
    buf = OutputBuffer()
    buf.append("REVIEW COMPLETE\r\n")
    _tick(buf, 5)
    assert buf.appears_stage_complete
    assert not buf.appears_waiting


def test_stage_complete_not_triggered_during_active_output():
    """Screen changing → no stage complete even if marker text appears."""
    buf = OutputBuffer()
    for i in range(10):
        buf.append(f"line {i} execute complete check\r\n")
        _tick(buf)
    assert not buf.appears_stage_complete


def test_stage_complete_clears_when_new_output():
    """After stage complete, new output clears the flag."""
    buf = OutputBuffer()
    buf.append("EXECUTE COMPLETE\r\n")
    _tick(buf, 5)
    assert buf.appears_stage_complete

    # Agent resumes (user typed something)
    buf.append("Starting new work...\r\n")
    _tick(buf)
    assert not buf.appears_stage_complete


# --- The prompt echo must not read as a completion ---


def test_sentinel_marker_detected():
    """The agent emitting the marker alone on a line is a completion."""
    buf = OutputBuffer()
    buf.append(f"Done with the work.\r\n{STAGE_COMPLETE_MARKER}\r\n")
    _tick(buf, 5)
    assert buf.appears_stage_complete


def test_done_instruction_is_not_a_completion():
    """REGRESSION: our own prompt echo must not trip completion detection.

    Prompts are rendered into the agent's transcript, so the instruction asking
    for the marker is on screen from the moment the stage starts. A substring
    match fired here — auto-advancing the stage before any work happened.
    """
    buf = OutputBuffer()
    buf.append(f"EXECUTE: Some task\r\n\r\n{_DONE_INSTRUCTION}\r\n")
    _tick(buf, 20)
    assert not buf.appears_stage_complete


def test_legacy_prompt_echo_is_not_a_completion():
    """Same guarantee for the pre-marker phrasing, taken from a real capture.

    Verbatim from .llm-cc/logs/llmcc_c6c21bf9_planning.log, where this exact
    line produced five spurious completion signals.
    """
    buf = OutputBuffer()
    buf.append(
        "Continue in the same session. You already have the plan in context.\r\n"
        "Plan: .llm-cc/tasks/c6c21bf9/plan.md\r\n"
        "\r\n"
        "When finished, say: EXECUTE COMPLETE\r\n"
    )
    _tick(buf, 20)
    assert not buf.appears_stage_complete


def test_legacy_bare_marker_still_detected():
    """A pre-marker session that declares completion on its own line still works."""
    buf = OutputBuffer()
    buf.append("When finished, say: EXECUTE COMPLETE\r\n")
    _tick(buf, 5)
    assert not buf.appears_stage_complete

    # ...agent does the work, then declares it
    buf.append("Did the thing.\r\nEXECUTE COMPLETE\r\n")
    _tick(buf, 5)
    assert buf.appears_stage_complete


def test_marker_detected_through_cli_gutter():
    """CLIs prefix output with bullets/quote bars; the marker still counts."""
    buf = OutputBuffer()
    buf.append(f"  ⏺ {STAGE_COMPLETE_MARKER}\r\n")
    _tick(buf, 5)
    assert buf.appears_stage_complete


def test_marker_inside_prose_is_not_a_completion():
    """The marker mentioned mid-sentence is discussion, not a declaration."""
    buf = OutputBuffer()
    buf.append(f"I will print {STAGE_COMPLETE_MARKER} once the tests pass.\r\n")
    _tick(buf, 20)
    assert not buf.appears_stage_complete
