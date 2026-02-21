"""CLI entry point for llm-command-center."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    # Determine project path: argument or current directory
    if len(sys.argv) > 1 and sys.argv[1] not in ("--help", "-h", "--version"):
        project_path = Path(sys.argv[1]).resolve()
    else:
        project_path = Path.cwd()

    if "--version" in sys.argv:
        from llm_cc import __version__

        print(f"llm-cc {__version__}")
        return

    if "--help" in sys.argv or "-h" in sys.argv:
        print("Usage: llm-cc [project-path]")
        print("  Launch the LLM Command Center TUI for the given project.")
        print("  Defaults to current directory if no path given.")
        return

    # Ensure project path exists
    if not project_path.is_dir():
        print(f"Error: {project_path} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Initialize storage and ensure .llm-cc dir exists
    from llm_cc.storage import Storage

    storage = Storage(project_path)
    storage.ensure_dirs()
    storage.ensure_gitignore()
    storage.update_recent_projects(str(project_path))

    from llm_cc.app import CommandCenterApp

    app = CommandCenterApp(project_path=project_path)
    app.run()


if __name__ == "__main__":
    main()
