"""Debug log for the paths that must not raise.

Agent teardown, log compression, status-file reads and session persistence all
run on best-effort paths where raising would break the pipeline — but swallowing
silently makes them undiagnosable. They log here instead.

A TUI can't print, so this writes to `.llm-cc/logs/llm-cc.log` only. Nothing is
emitted to stdout/stderr, and `setup()` is a no-op until a project path is known
(tests import the modules without ever calling it).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("llm_cc")


def setup(project_path: Path, level: int = logging.DEBUG) -> None:
    """Attach a file handler under the project's `.llm-cc/logs/`. Idempotent."""
    if logger.handlers:
        return
    log_dir = project_path / ".llm-cc" / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_dir / "llm-cc.log")
    except OSError:
        return
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
