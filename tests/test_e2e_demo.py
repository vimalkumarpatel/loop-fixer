from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from loop_fixer.adapters.python_pytest import PythonPytestAdapter
from loop_fixer.fsm import LoopState, run_loop
from loop_fixer.llm_client import FakeLLMClient

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "broken_repo"

FIXING_DIFF = """\
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""

USELESS_DIFF = """\
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a - b  # comment
"""


def _seed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE_DIR, repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def test_loop_converges_to_passing_test(tmp_path):
    repo = _seed_repo(tmp_path)
    from loop_fixer.git_checkpoint import preflight

    branch, baseline = preflight(repo, "test_calc.py::test_add")

    fake_llm = FakeLLMClient(responses=[FIXING_DIFF])
    events = []
    state = LoopState(
        repo_root=repo,
        target_test="test_calc.py::test_add",
        llm_client=fake_llm,
        adapter=PythonPytestAdapter(),
        max_iterations=5,
        no_progress_window=3,
        baseline_commit=baseline,
        last_known_good_commit=baseline,
        on_event=events.append,
    )

    result = run_loop(state)

    assert result.status == "success"
    assert "return a + b" in (repo / "calc.py").read_text()
    # The unmodified test file's assertion is what actually passed.
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "test_calc.py::test_add", "-q"], cwd=repo, capture_output=True, text=True
    )
    assert proc.returncode == 0
    assert any("success" in e for e in events)


def test_loop_stops_on_no_progress_when_unfixable(tmp_path):
    repo = _seed_repo(tmp_path)
    from loop_fixer.git_checkpoint import preflight

    branch, baseline = preflight(repo, "test_calc.py::test_add")

    fake_llm = FakeLLMClient(responses=[USELESS_DIFF] * 5)
    state = LoopState(
        repo_root=repo,
        target_test="test_calc.py::test_add",
        llm_client=fake_llm,
        adapter=PythonPytestAdapter(),
        max_iterations=10,
        no_progress_window=3,
        baseline_commit=baseline,
        last_known_good_commit=baseline,
    )

    result = run_loop(state)

    assert result.status == "failed_no_progress"
    # Working tree must be rolled back to baseline (still broken, but clean).
    assert (repo / "calc.py").read_text() == "def add(a, b):\n    return a - b\n"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True)
    assert status.stdout.strip() == ""


def test_loop_rejects_llm_attempt_to_edit_test_file(tmp_path):
    """The unfakeable-signal guard: an LLM diff targeting the test file is rejected,
    counted as a failed attempt, and never reaches disk."""
    repo = _seed_repo(tmp_path)
    from loop_fixer.git_checkpoint import preflight

    branch, baseline = preflight(repo, "test_calc.py::test_add")

    cheat_diff = """\
--- a/test_calc.py
+++ b/test_calc.py
@@ -1,4 +1,4 @@
 from calc import add

 def test_add():
-    assert add(2, 3) == 5
+    assert True
"""
    fake_llm = FakeLLMClient(responses=[cheat_diff] * 5)
    state = LoopState(
        repo_root=repo,
        target_test="test_calc.py::test_add",
        llm_client=fake_llm,
        adapter=PythonPytestAdapter(),
        max_iterations=10,
        no_progress_window=3,
        baseline_commit=baseline,
        last_known_good_commit=baseline,
    )

    result = run_loop(state)

    assert result.status == "failed_no_progress"
    original_test_source = (FIXTURE_DIR / "test_calc.py").read_text()
    assert (repo / "test_calc.py").read_text() == original_test_source
    assert all(
        a.failure_signature and a.failure_signature.startswith("PATCH_APPLY_ERROR")
        for a in result.attempts
    )
