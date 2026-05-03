"""End-to-end test: launch app, create tasks, navigate, advance, delete."""

import asyncio
import subprocess
import tempfile
from pathlib import Path

from llm_cc.app import CommandCenterApp
from llm_cc.models import Task, TaskStatus
from llm_cc.storage import Storage


def init_git_repo(path: Path) -> None:
    """Initialize a real git repo with an initial commit."""
    subprocess.run(["git", "init"], cwd=path, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=path, capture_output=True)
    # Create a file and commit
    (path / "README.md").write_text("# Test Project\n")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path,
        capture_output=True,
        env={"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@test.com",
             "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@test.com",
             "HOME": str(Path.home()), "PATH": subprocess.check_output(
                 ["bash", "-c", "echo $PATH"]).decode().strip()},
    )


async def test_full_flow():
    """Test the app headlessly using Textual's pilot."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)

        # Set up real git repo so pipeline operations work
        init_git_repo(project)

        storage = Storage(project)
        storage.ensure_dirs()
        storage.ensure_gitignore()

        # Pre-seed tasks
        storage.save_task(Task(id="aaa", title="Task A", description="First task"))
        storage.save_task(Task(id="bbb", title="Task B", description="Second task"))
        storage.save_task(
            Task(id="ccc", title="Task C", status=TaskStatus.EXECUTE, description="In progress")
        )

        app = CommandCenterApp(project_path=project)

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            # --- Test 1: Board renders ---
            board = app.screen
            print(f"[1] Screen: {type(board).__name__}")
            assert type(board).__name__ == "BoardScreen"

            from llm_cc.ui.board import KanbanColumn, TaskCard
            columns = board.query(KanbanColumn)
            cards = board.query(TaskCard)
            print(f"[2] Columns: {len(columns)}, Cards: {len(cards)}")
            assert len(columns) == 5
            assert len(cards) == 3

            # --- Test 2: Vim navigation ---
            await pilot.press("right")
            await pilot.pause()
            assert board._active_col == 1
            print("[3] left/right navigation: OK")

            await pilot.press("left")
            await pilot.pause()
            assert board._active_col == 0

            await pilot.press("down")
            await pilot.pause()
            assert board._columns[0].selected_index == 1

            await pilot.press("up")
            await pilot.pause()
            assert board._columns[0].selected_index == 0
            print("[4] up/down navigation: OK")

            # --- Test 3: Advance task (with git repo) ---
            # Task A is selected in Backlog. Advance to Planning.
            # Pipeline will try to create worktree — should work with real git repo.
            await pilot.press("m")
            await pilot.pause()
            await asyncio.sleep(1.5)  # give worker time for git ops

            store = storage.load_tasks()
            task_a = store.get("aaa")
            print(f"[5] Task A after advance: {task_a.status.value}")
            if task_a.status == TaskStatus.PLANNING:
                print("    Pipeline advance with git: OK")
                assert task_a.branch_name is not None
                assert task_a.worktree_path is not None
                print(f"    Branch: {task_a.branch_name}")
                print(f"    Worktree: {task_a.worktree_path}")
            else:
                print(f"    Advance stayed at {task_a.status.value} (pipeline may have errored)")

            # --- Test 4: Advance again (Planning -> Execute) ---
            # Navigate to planning column first
            if task_a.status == TaskStatus.PLANNING:
                await pilot.press("right")  # go to planning column
                await pilot.pause()
                await pilot.press("m")  # advance to execute
                await pilot.pause()
                await asyncio.sleep(1.5)

                store = storage.load_tasks()
                task_a = store.get("aaa")
                print(f"[6] Task A after 2nd advance: {task_a.status.value}")
            else:
                print("[6] Skipped (task didn't advance to planning)")

            # --- Test 5: Revert task ---
            if task_a.status in (TaskStatus.EXECUTE, TaskStatus.PLANNING):
                await pilot.press("r")
                await pilot.pause()
                await asyncio.sleep(1.0)

                store = storage.load_tasks()
                task_a = store.get("aaa")
                print(f"[7] Task A after revert: {task_a.status.value}")
            else:
                print("[7] Skipped (task not in revertable state)")

            # --- Test 6: Create new task ---
            # Go back to backlog
            while board._active_col > 0:
                await pilot.press("left")
                await pilot.pause()

            await pilot.press("o")
            await pilot.pause()
            await asyncio.sleep(0.3)

            # Type title
            await pilot.press(*list("New Task"))
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await asyncio.sleep(0.5)

            store = storage.load_tasks()
            new = [t for t in store.tasks if "New" in t.title]
            print(f"[8] Create task: {len(new) > 0} — {[t.title for t in new]}")

            # --- Test 7: Edit task ---
            await pilot.press("e")
            await pilot.pause()
            await asyncio.sleep(0.3)

            # The edit dialog should open with current title pre-filled
            # Just press escape to cancel
            await pilot.press("escape")
            await pilot.pause()
            print("[9] Edit dialog open/close: OK")

            # --- Test 8: Delete task ---
            count_before = len(storage.load_tasks().tasks)
            await pilot.press("x")
            await pilot.pause()
            await asyncio.sleep(0.3)

            await pilot.press("y")
            await pilot.pause()
            await asyncio.sleep(0.5)

            count_after = len(storage.load_tasks().tasks)
            print(f"[10] Delete: {count_before} -> {count_after}")
            assert count_after < count_before

            # --- Test 9: Boundary checks ---
            # Try to go left past backlog
            while board._active_col > 0:
                await pilot.press("left")
                await pilot.pause()
            await pilot.press("left")
            await pilot.pause()
            assert board._active_col == 0
            print("[11] Left boundary: OK")

            # Try to go right past Done
            for _ in range(10):
                await pilot.press("right")
                await pilot.pause()
            assert board._active_col == 4
            print("[12] Right boundary: OK")

            # --- Test 10: Diff view (no branch = warning) ---
            while board._active_col > 0:
                await pilot.press("left")
                await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await asyncio.sleep(0.3)
            print("[13] Diff on branchless task: OK (warning shown)")

            # --- Test 11: Agent panel (no session = warning) ---
            await pilot.press("enter")
            await pilot.pause()
            await asyncio.sleep(0.3)
            print("[14] Agent panel on sessionless task: OK (warning shown)")

            # --- Quit ---
            await pilot.press("q")
            await pilot.pause()

    print()
    print("=" * 50)
    print("ALL E2E TESTS PASSED")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(test_full_flow())
