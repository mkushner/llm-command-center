"""Tests for OutputBuffer.appears_waiting — the input-detection heuristic."""

from llm_cc.agents import OutputBuffer


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
