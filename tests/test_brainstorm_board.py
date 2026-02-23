"""Tests for brainstorm auto-advance in board polling."""

from llm_cc.ui.board import BoardScreen


def test_poll_detects_dead_brainstorm_agent():
    """When a brainstorm sub-agent's process exits, poll should trigger auto-advance."""
    assert hasattr(BoardScreen, "_do_brainstorm_advance")
