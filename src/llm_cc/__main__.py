"""CLI entry point for llm-command-center."""

from __future__ import annotations

import shutil
import sys
import tomllib
from pathlib import Path


def _require_tmux() -> None:
    """Verify tmux is on PATH. llm-cc requires it as the agent backend."""
    if shutil.which("tmux") is None:
        print(
            "Error: tmux is required by llm-cc but was not found on PATH.\n"
            "Install with: brew install tmux",
            file=sys.stderr,
        )
        sys.exit(1)


def _import_tasks(storage: object, tasks_path: Path) -> int:
    """Import tasks from a TOML file into BACKLOG. Returns count of imported tasks."""
    from llm_cc.models import Task

    with open(tasks_path, "rb") as f:
        data = tomllib.load(f)

    task_list = data.get("task", [])
    if not isinstance(task_list, list):
        task_list = [task_list]

    # Read existing titles for dedup (snapshot is fine — import runs at startup)
    store = storage.load_tasks()
    existing_titles = {t.title for t in store.tasks}
    count = 0
    for entry in task_list:
        title = entry.get("title", "").strip()
        if not title:
            continue
        if title in existing_titles:
            continue  # skip duplicates by title
        task = Task(
            title=title,
            description=entry.get("description"),
            verify=entry.get("verify"),
            done=entry.get("done"),
            checkout_branch=entry.get("checkout_branch"),
        )
        # Atomic per-task upsert (read-modify-write under one lock)
        storage.save_task(task)
        existing_titles.add(title)
        count += 1
    return count


def main() -> None:
    args = sys.argv[1:]
    tasks_file: str | None = None
    clean_exit = False

    # Extract --tasks flag
    if "--tasks" in args:
        idx = args.index("--tasks")
        if idx + 1 < len(args):
            tasks_file = args[idx + 1]
            args = args[:idx] + args[idx + 2:]
        else:
            print("Error: --tasks requires a file path", file=sys.stderr)
            sys.exit(1)

    # --clean-exit: kill tmux sessions on shutdown (default: leave them running)
    if "--clean-exit" in args:
        clean_exit = True
        args = [a for a in args if a != "--clean-exit"]

    # --web: run the browser frontend instead of the TUI (--host/--port optional)
    web = False
    host = "127.0.0.1"
    port = 7420
    if "--web" in args:
        web = True
        args = [a for a in args if a != "--web"]
    for flag, setter in (("--host", "host"), ("--port", "port")):
        if flag in args:
            idx = args.index(flag)
            if idx + 1 >= len(args):
                print(f"Error: {flag} requires a value", file=sys.stderr)
                sys.exit(1)
            value = args[idx + 1]
            if setter == "port":
                try:
                    port = int(value)
                except ValueError:
                    print("Error: --port must be a number", file=sys.stderr)
                    sys.exit(1)
            else:
                host = value
            args = args[:idx] + args[idx + 2:]

    # Determine project path: first positional arg or cwd
    if args and args[0] not in ("--help", "-h", "--version"):
        project_path = Path(args[0]).resolve()
    else:
        project_path = Path.cwd()

    if "--version" in args:
        from llm_cc import __version__

        print(f"llm-cc {__version__}")
        return

    if "--help" in args or "-h" in args:
        print("Usage: llm-cc [project-path] [--tasks tasks.toml] [--clean-exit] [--web]")
        print("  Launch the LLM Command Center for the given project.")
        print("  Defaults to current directory if no path given.")
        print("")
        print("Options:")
        print("  --tasks FILE  Import tasks from a TOML file into BACKLOG")
        print("  --clean-exit  Kill tmux agent sessions on shutdown")
        print("                (default: sessions persist for reattach)")
        print("  --web         Run the browser UI instead of the terminal TUI")
        print("  --host ADDR   Web bind address (default: 127.0.0.1)")
        print("  --port N      Web port (default: 7420)")
        return

    # Ensure project path exists
    if not project_path.is_dir():
        print(f"Error: {project_path} is not a directory", file=sys.stderr)
        sys.exit(1)

    _require_tmux()

    # Initialize storage and ensure .llm-cc dir exists
    from llm_cc.storage import Storage

    storage = Storage(project_path)
    storage.ensure_dirs()
    storage.ensure_gitignore()
    storage.update_recent_projects(str(project_path))

    # Auto-permissions setup (idempotent, silent after first run)
    from llm_cc.permissions import ensure_global_claude_default_mode, ensure_global_gitignore

    ensure_global_gitignore()
    ensure_global_claude_default_mode()

    # Import tasks if --tasks specified or .llm-cc/tasks.toml exists
    if tasks_file:
        tasks_path = Path(tasks_file).resolve()
        if not tasks_path.is_file():
            print(f"Error: tasks file not found: {tasks_path}", file=sys.stderr)
            sys.exit(1)
        count = _import_tasks(storage, tasks_path)
        if count:
            print(f"Imported {count} task{'s' if count != 1 else ''} from {tasks_path.name}")
    else:
        # Auto-detect .llm-cc/tasks.toml
        auto_tasks = project_path / ".llm-cc" / "tasks.toml"
        if auto_tasks.is_file():
            count = _import_tasks(storage, auto_tasks)
            if count:
                print(f"Imported {count} task{'s' if count != 1 else ''} from .llm-cc/tasks.toml")

    from llm_cc.agents import set_clean_exit_mode

    set_clean_exit_mode(clean_exit)

    if web:
        from llm_cc.web.server import run_web

        run_web(project_path, host=host, port=port)
        return

    from llm_cc.app import CommandCenterApp

    app = CommandCenterApp(project_path=project_path)
    app.run()


if __name__ == "__main__":
    main()
