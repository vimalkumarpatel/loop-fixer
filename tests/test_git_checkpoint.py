from __future__ import annotations

import subprocess

import pytest

from loop_fixer.errors import GitCheckpointError
from loop_fixer.git_checkpoint import commit_attempt, current_branch, preflight, rollback


def _init_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)


def test_preflight_rejects_dirty_tree(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 2\n")  # dirty, uncommitted
    with pytest.raises(GitCheckpointError, match="not clean"):
        preflight(tmp_path, "tests/test_x.py::test_y")


def test_preflight_rejects_non_git_dir(tmp_path):
    with pytest.raises(GitCheckpointError, match="not inside a git repository"):
        preflight(tmp_path, "tests/test_x.py::test_y")


def test_preflight_creates_dedicated_branch(tmp_path):
    _init_repo(tmp_path)
    original_branch = current_branch(tmp_path)
    branch, baseline = preflight(tmp_path, "tests/test_x.py::test_y")
    assert branch.startswith("loop-fixer/")
    assert current_branch(tmp_path) == branch
    assert current_branch(tmp_path) != original_branch


def test_commit_and_rollback(tmp_path):
    _init_repo(tmp_path)
    branch, baseline = preflight(tmp_path, "tests/test_x.py::test_y")

    (tmp_path / "a.py").write_text("x = 999\n")
    sha = commit_attempt(tmp_path, "attempt 1: failed")
    assert sha != baseline
    assert (tmp_path / "a.py").read_text() == "x = 999\n"

    rollback(tmp_path, baseline)
    assert (tmp_path / "a.py").read_text() == "x = 1\n"

    status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True)
    assert status.stdout.strip() == ""
