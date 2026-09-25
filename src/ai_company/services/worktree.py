"""Git worktree isolation for parallel Cursor workers."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ai_company.paths import data_dir, project_root


@dataclass(frozen=True)
class WorktreeInfo:
    path: Path
    branch: str
    base_ref: str
    task_id: str


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=check,
    )


def workspace_is_git(workspace: Path | None = None) -> bool:
    ws = (workspace or project_root()).resolve()
    return (ws / ".git").exists()


def worktree_root() -> Path:
    root = data_dir() / "worktrees"
    root.mkdir(parents=True, exist_ok=True)
    return root


def create_task_worktree(
    task_id: str,
    *,
    workspace: Path | None = None,
    base_ref: str | None = None,
) -> WorktreeInfo | None:
    """
    Create an isolated worktree for a task. Returns None if workspace is not a git repo
    (caller should fall back to shared workspace).
    """
    ws = (workspace or project_root()).resolve()
    if not workspace_is_git(ws):
        return None
    try:
        head = _git(ws, "rev-parse", "HEAD")
        base = base_ref or head.stdout.strip()
    except Exception:
        return None

    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_id)[:48]
    branch = f"ai-company/{safe_id}"
    path = worktree_root() / safe_id
    try:
        if path.exists():
            remove_task_worktree(path, workspace=ws)

        _git(ws, "branch", branch, base, check=False)
        try:
            _git(ws, "worktree", "add", str(path), branch)
        except subprocess.CalledProcessError:
            try:
                _git(ws, "worktree", "add", "-f", str(path), branch)
            except Exception:
                return None
    except Exception:
        return None

    return WorktreeInfo(path=path, branch=branch, base_ref=base, task_id=task_id)


def remove_task_worktree(path: Path, *, workspace: Path | None = None) -> None:
    ws = (workspace or project_root()).resolve()
    if path.exists():
        _git(ws, "worktree", "remove", "--force", str(path), check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


def merge_worktree_branch(
    info: WorktreeInfo,
    *,
    workspace: Path | None = None,
    into: str = "HEAD",
) -> bool:
    """Best-effort merge of worktree branch into main workspace. Returns success."""
    ws = (workspace or project_root()).resolve()
    try:
        # Stage nothing in worktree; merge branch
        _git(ws, "merge", "--no-ff", "-m", f"ai-company merge {info.task_id}", info.branch)
        return True
    except subprocess.CalledProcessError:
        _git(ws, "merge", "--abort", check=False)
        return False
