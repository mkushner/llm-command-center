"""Git operations: worktree/branch management, diff, PR creation."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from .models import GitConfig, GitMode, Task
from .utils import async_run


class GitWorkspace:
    """Manages git worktrees or branches for task isolation.

    If git mode is NONE or project is not a git repo, all operations
    are no-ops and the agent runs in the project directory directly.
    """

    def __init__(self, project_path: Path, config: GitConfig) -> None:
        self.project_path = project_path
        self.config = config
        self._branch_lock = asyncio.Lock()
        # Auto-detect base branch if left as default
        if self.is_git_repo() and config.base_branch == "main":
            self._auto_detect_base_branch()

    def is_git_repo(self) -> bool:
        return (self.project_path / ".git").exists()

    def _auto_detect_base_branch(self) -> None:
        """Detect the default branch (main, master, etc.) from the repo."""
        import subprocess

        # Try 1: origin/HEAD (set by git clone or git remote set-head)
        try:
            result = subprocess.run(
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
                cwd=self.project_path,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                ref = result.stdout.strip()
                branch = ref.split("/")[-1]
                if branch:
                    self.config.base_branch = branch
                    return
        except Exception:
            pass

        # Try 2: check if common branch names exist locally
        for candidate in ("main", "master", "develop"):
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "--verify", f"refs/heads/{candidate}"],
                    cwd=self.project_path,
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    self.config.base_branch = candidate
                    return
            except Exception:
                pass

    @property
    def _git_enabled(self) -> bool:
        """Git operations only run if repo exists AND mode isn't NONE."""
        return self.config.mode != GitMode.NONE and self.is_git_repo()

    async def setup(self, task: Task) -> Path:
        """Create workspace for a task. Returns working directory path."""
        if not self._git_enabled:
            # No git ops, but detect current branch for template resolution
            if self.is_git_repo() and not task.branch_name:
                result = await async_run(
                    ["git", "branch", "--show-current"],
                    cwd=self.project_path,
                    capture=True,
                    check=False,
                )
                branch = result.stdout.strip()
                if branch:
                    task.branch_name = branch
            return self.project_path

        if self.config.mode == GitMode.WORKTREE:
            return await self._setup_worktree(task)
        return await self._setup_branch(task)

    async def _setup_worktree(self, task: Task) -> Path:
        slug = task.slug()
        wt_path = self.project_path / ".llm-cc" / "worktrees" / slug
        branch = f"{self.config.branch_prefix}{slug}"

        # Remove stale worktree if exists (git command first, then force-delete dir)
        if wt_path.exists():
            await async_run(
                ["git", "worktree", "remove", "--force", str(wt_path)],
                cwd=self.project_path,
                check=False,
            )
        if wt_path.exists():
            shutil.rmtree(str(wt_path), ignore_errors=True)

        # Prune stale worktree refs (so branch delete works)
        await async_run(
            ["git", "worktree", "prune"],
            cwd=self.project_path,
            check=False,
        )

        # Clean stale branch if exists
        await async_run(
            ["git", "branch", "-D", branch],
            cwd=self.project_path,
            check=False,
        )

        # Create worktree
        result = await async_run(
            ["git", "worktree", "add", str(wt_path), "-b", branch, self.config.base_branch],
            cwd=self.project_path,
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            # If branch exists but worktree doesn't, reuse the branch
            if "already exists" in result.stderr:
                await async_run(
                    ["git", "worktree", "add", str(wt_path), branch],
                    cwd=self.project_path,
                    capture=True,
                )
            else:
                raise RuntimeError(f"git worktree add failed: {result.stderr.strip()}")

        await self._init_workspace(wt_path)
        task.worktree_path = str(wt_path)
        task.branch_name = branch
        return wt_path

    async def _setup_branch(self, task: Task) -> Path:
        """Branch-only mode. Lock prevents concurrent checkouts."""
        async with self._branch_lock:
            slug = task.slug()
            branch = f"{self.config.branch_prefix}{slug}"
            await async_run(
                ["git", "checkout", "-b", branch, self.config.base_branch],
                cwd=self.project_path,
            )
            task.branch_name = branch
            return self.project_path

    async def _init_workspace(self, wt_path: Path) -> None:
        """Copy files and run init script in new workspace."""
        for f in self.config.copy_files:
            src = self.project_path / f
            dst = wt_path / f
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dst))

        if self.config.init_script:
            await async_run(
                self.config.init_script,
                cwd=wt_path,
                shell=True,
                check=False,
                timeout=120.0,
            )

    async def diff_from_base(self, task: Task) -> str:
        """Get diff between base branch and task workspace."""
        if not self._git_enabled:
            return ""
        cwd = Path(task.worktree_path) if task.worktree_path else self.project_path
        result = await async_run(
            ["git", "diff", self.config.base_branch, "--", "."],
            cwd=cwd,
            capture=True,
            check=False,
        )
        return result.stdout

    async def changed_files(self, task: Task) -> list[str]:
        """List files changed from base branch."""
        if not self._git_enabled:
            return []
        cwd = Path(task.worktree_path) if task.worktree_path else self.project_path
        result = await async_run(
            ["git", "diff", self.config.base_branch, "--name-only"],
            cwd=cwd,
            capture=True,
            check=False,
        )
        return [f for f in result.stdout.strip().splitlines() if f]

    async def cleanup(self, task: Task) -> None:
        """Remove worktree and prune. Keeps branch for history."""
        if not self._git_enabled:
            return
        if task.worktree_path:
            await async_run(
                ["git", "worktree", "remove", "--force", task.worktree_path],
                cwd=self.project_path,
                check=False,
            )
            await async_run(
                ["git", "worktree", "prune"],
                cwd=self.project_path,
                check=False,
            )
            task.worktree_path = None


class PRManager:
    """Create pull requests via GitHub CLI (gh)."""

    def __init__(self, project_path: Path) -> None:
        self.project_path = project_path

    async def create(
        self,
        task: Task,
        title: str,
        body: str,
    ) -> tuple[int, str]:
        """Stage, commit, push, create PR. Returns (pr_number, pr_url)."""
        cwd = Path(task.worktree_path) if task.worktree_path else self.project_path

        await async_run(["git", "add", "-A"], cwd=cwd)

        commit_msg = f"{title}\n\n{body}"
        await async_run(
            ["git", "commit", "-m", commit_msg],
            cwd=cwd,
            check=False,
        )

        if not task.branch_name:
            raise ValueError("Task has no branch — cannot push")
        await async_run(
            ["git", "push", "-u", "origin", task.branch_name],
            cwd=cwd,
        )

        result = await async_run(
            [
                "gh", "pr", "create",
                "--title", title,
                "--body", body,
                "--head", task.branch_name,
            ],
            cwd=cwd,
            capture=True,
        )

        pr_url = result.stdout.strip()
        pr_number = int(pr_url.rstrip("/").split("/")[-1])
        task.pr_url = pr_url
        task.pr_number = pr_number
        return pr_number, pr_url

    @staticmethod
    def is_available() -> bool:
        return shutil.which("gh") is not None
