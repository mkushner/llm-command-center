"""Web frontend for llm-cc (`llm-cc --web`).

An alternate frontend to the Textual TUI. Serves a browser UI over the same
Storage / AgentRegistry / PipelineEngine objects the TUI uses, plus a PTY
bridge (`tmux attach`) so each agent's live terminal is interactive in xterm.js.
"""

from __future__ import annotations
