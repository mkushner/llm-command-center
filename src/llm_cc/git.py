"""Git operations: worktree/branch management, diff, PR creation."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from .models import GitConfig, GitMode, Task
from .permissions import write_claude_settings
from .utils import RunResult, async_run


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

        # Explicit checkout_branch bypasses slug-based branch naming and
        # attaches the worktree to an existing branch rather than cutting a new one.
        if task.checkout_branch:
            branch = task.checkout_branch
            use_existing_branch = True
        else:
            branch = f"{self.config.branch_prefix}{slug}"
            use_existing_branch = False

        # If worktree already exists and is valid, reuse it (preserves uncommitted work)
        if wt_path.exists() and (wt_path / ".git").exists():
            task.worktree_path = str(wt_path)
            task.branch_name = branch
            return wt_path
        # Clean up invalid/empty worktree directory
        if wt_path.exists() and not (wt_path / ".git").exists():
            shutil.rmtree(str(wt_path), ignore_errors=True)

        # Prune stale worktree refs (so branch operations work)
        await async_run(
            ["git", "worktree", "prune"],
            cwd=self.project_path,
            check=False,
        )

        if use_existing_branch:
            result = await self._worktree_add_existing(wt_path, branch)
        else:
            result = await async_run(
                ["git", "worktree", "add", str(wt_path), "-b", branch, self.config.base_branch],
                cwd=self.project_path,
                check=False,
                capture=True,
            )
            if result.returncode != 0 and "already exists" in result.stderr:
                # Slug-branch exists but no worktree — attach to existing branch
                result = await async_run(
                    ["git", "worktree", "add", str(wt_path), branch],
                    cwd=self.project_path,
                    check=False,
                    capture=True,
                )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "already used by worktree" in stderr or "is already checked out" in stderr:
                raise RuntimeError(
                    f"branch '{branch}' is already checked out elsewhere "
                    f"(main checkout or another worktree). Switch that checkout off "
                    f"the branch or pick a different branch."
                )
            raise RuntimeError(f"git worktree add failed: {stderr}")

        await self._init_workspace(wt_path)
        write_claude_settings(wt_path)
        task.worktree_path = str(wt_path)
        task.branch_name = branch
        return wt_path

    async def _worktree_add_existing(self, wt_path: Path, branch: str) -> RunResult:
        """Attach a worktree to an existing branch (local or remote-tracking)."""
        local = await async_run(
            ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
            cwd=self.project_path,
            check=False,
            capture=True,
        )
        if local.returncode == 0:
            return await async_run(
                ["git", "worktree", "add", str(wt_path), branch],
                cwd=self.project_path,
                check=False,
                capture=True,
            )
        # No local branch — try remote and create a local tracking branch.
        remote = await async_run(
            ["git", "rev-parse", "--verify", f"refs/remotes/origin/{branch}"],
            cwd=self.project_path,
            check=False,
            capture=True,
        )
        if remote.returncode != 0:
            raise RuntimeError(
                f"checkout_branch '{branch}' not found locally or on origin. "
                f"Fetch it first (git fetch origin {branch}) or create it."
            )
        return await async_run(
            ["git", "worktree", "add", str(wt_path), "-b", branch, f"origin/{branch}"],
            cwd=self.project_path,
            check=False,
            capture=True,
        )

    async def _current_branch(self) -> str:
        """Return the current branch name, or empty string if detached."""
        result = await async_run(
            ["git", "branch", "--show-current"],
            cwd=self.project_path,
            capture=True,
            check=False,
        )
        return result.stdout.strip()

    async def _setup_branch(self, task: Task) -> Path:
        """Branch-only mode. Lock prevents concurrent checkouts.

        If already on a non-base branch, stay on it instead of creating a new one.
        This supports the workflow where the user checks out a feature branch
        before launching llm-cc.
        """
        async with self._branch_lock:
            current = await self._current_branch()
            if current and current != self.config.base_branch:
                # Already on a feature branch — use it as-is
                task.branch_name = current
                return self.project_path

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
        """Remove worktree and prune. Keeps branch for history.

        Uses non-force remove first to protect uncommitted changes.
        Falls back to --force only if the worktree directory is already gone.
        """
        if not self._git_enabled:
            return
        if task.worktree_path:
            removed = False
            # Try clean removal (fails if uncommitted changes exist — that's intentional)
            result = await async_run(
                ["git", "worktree", "remove", task.worktree_path],
                cwd=self.project_path,
                check=False,
                capture=True,
            )
            if result.returncode == 0:
                removed = True
            else:
                wt = Path(task.worktree_path)
                if not wt.exists():
                    # Directory already gone — force-remove the worktree ref
                    await async_run(
                        ["git", "worktree", "remove", "--force", task.worktree_path],
                        cwd=self.project_path,
                        check=False,
                    )
                    removed = True
                # else: uncommitted changes — leave the worktree intact
            await async_run(
                ["git", "worktree", "prune"],
                cwd=self.project_path,
                check=False,
            )
            if removed:
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
        commit_result = await async_run(
            ["git", "commit", "-m", commit_msg],
            cwd=cwd,
            check=False,
            capture=True,
        )
        if commit_result.returncode != 0:
            stderr = commit_result.stderr.strip()
            stdout = commit_result.stdout.strip()
            # "nothing to commit" is acceptable — push whatever is on the branch
            if "nothing to commit" not in stdout and "nothing to commit" not in stderr:
                raise RuntimeError(f"git commit failed: {stderr or stdout}")

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
