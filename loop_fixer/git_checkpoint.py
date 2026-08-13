from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .errors import GitCheckpointError


def _run_git(repo_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo_root), capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise GitCheckpointError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def preflight(repo_root: Path, target_test: str) -> tuple[str, str]:
    """Refuse to start unless inside a git repo with a clean working tree.

    Creates and checks out a dedicated disposable branch. Returns
    (branch_name, baseline_commit_sha). Never touches the user's original branch.
    """
    inside = _run_git(repo_root, "rev-parse", "--is-inside-work-tree", check=False)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise GitCheckpointError(f"'{repo_root}' is not inside a git repository")

    status = _run_git(repo_root, "status", "--porcelain")
    if status.stdout.strip():
        raise GitCheckpointError(
            "working tree is not clean — commit or stash your changes before running loop_fixer"
        )

    baseline = _run_git(repo_root, "rev-parse", "HEAD").stdout.strip()

    slug = _slugify(target_test)
    branch = f"loop-fixer/{slug}/{int(time.time())}"
    _run_git(repo_root, "checkout", "-b", branch)

    return branch, baseline


def _slugify(target_test: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in target_test).strip("-")[:60]


def commit_attempt(repo_root: Path, message: str) -> str:
    """Commit the current working-tree state (pass or fail) and return the SHA."""
    _run_git(repo_root, "add", "-A")
    # Nothing to commit is possible only if the patch produced no net diff; still
    # record a checkpoint by allowing an empty commit so attempt history stays complete.
    _run_git(repo_root, "commit", "--allow-empty", "-m", message)
    return _run_git(repo_root, "rev-parse", "HEAD").stdout.strip()


def rollback(repo_root: Path, commit_sha: str) -> None:
    """Hard-reset the working tree back to a known-good commit, leaving it clean."""
    _run_git(repo_root, "reset", "--hard", commit_sha)


def current_branch(repo_root: Path) -> str:
    return _run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
