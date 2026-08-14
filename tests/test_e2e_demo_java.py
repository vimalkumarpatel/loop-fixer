"""Java analog of test_e2e_demo.py: drives a real `mvn test` against the
seeded Maven fixture with a FakeLLMClient scripted to fix the bug. Skipped
if `mvn` isn't on PATH so the suite stays runnable on machines without Java
tooling."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from loop_fixer.adapters.java_maven import JavaMavenAdapter
from loop_fixer.fsm import build_initial_state, run_loop
from loop_fixer.llm_client import FakeLLMClient

pytestmark = pytest.mark.skipif(shutil.which("mvn") is None, reason="mvn not found on PATH")

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "broken_repo_java"
TARGET = "com.example.CalcTest#testAdd"

FIXING_DIFF = """\
--- a/src/main/java/com/example/Calc.java
+++ b/src/main/java/com/example/Calc.java
@@ -1,7 +1,7 @@
 package com.example;

 public class Calc {
     public static int add(int a, int b) {
-        return a - b;
+        return a + b;
     }
 }
"""

USELESS_DIFF = """\
--- a/src/main/java/com/example/Calc.java
+++ b/src/main/java/com/example/Calc.java
@@ -1,7 +1,7 @@
 package com.example;

 public class Calc {
     public static int add(int a, int b) {
-        return a - b;
+        return a - b; // noop
     }
 }
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

    branch, baseline = preflight(repo, TARGET)

    fake_llm = FakeLLMClient(responses=[FIXING_DIFF])
    events = []
    initial_state = build_initial_state(
        repo_root=str(repo),
        target_test=TARGET,
        language="java",
        max_iterations=5,
        no_progress_window=3,
        test_timeout=120.0,
        baseline_commit=baseline,
        last_known_good_commit=baseline,
    )

    result = run_loop(initial_state, llm_client=fake_llm, adapter=JavaMavenAdapter(), on_event=events.append)

    assert result["status"] == "success"
    assert "return a + b;" in (repo / "src/main/java/com/example/Calc.java").read_text()
    # The unmodified test file's assertion is what actually passed.
    proc = subprocess.run(
        ["mvn", "-q", f"-Dtest={TARGET}", "-Dsurefire.failIfNoSpecifiedTests=false", "test"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert any("success" in e for e in events)


def test_loop_stops_on_no_progress_when_unfixable(tmp_path):
    repo = _seed_repo(tmp_path)
    from loop_fixer.git_checkpoint import preflight

    branch, baseline = preflight(repo, TARGET)

    fake_llm = FakeLLMClient(responses=[USELESS_DIFF] * 5)
    initial_state = build_initial_state(
        repo_root=str(repo),
        target_test=TARGET,
        language="java",
        max_iterations=10,
        no_progress_window=3,
        test_timeout=120.0,
        baseline_commit=baseline,
        last_known_good_commit=baseline,
    )

    result = run_loop(initial_state, llm_client=fake_llm, adapter=JavaMavenAdapter())

    assert result["status"] == "failed_no_progress"
    # Working tree must be rolled back to baseline (still broken, but clean).
    original = (FIXTURE_DIR / "src/main/java/com/example/Calc.java").read_text()
    assert (repo / "src/main/java/com/example/Calc.java").read_text() == original
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True)
    assert status.stdout.strip() == ""


def test_loop_rejects_llm_attempt_to_edit_test_file(tmp_path):
    """The unfakeable-signal guard: an LLM diff targeting the test file is rejected,
    counted as a failed attempt, and never reaches disk."""
    repo = _seed_repo(tmp_path)
    from loop_fixer.git_checkpoint import preflight

    branch, baseline = preflight(repo, TARGET)

    cheat_diff = """\
--- a/src/test/java/com/example/CalcTest.java
+++ b/src/test/java/com/example/CalcTest.java
@@ -1,10 +1,10 @@
 package com.example;

 import static org.junit.Assert.assertEquals;

 import org.junit.Test;

 public class CalcTest {
     @Test
     public void testAdd() {
-        assertEquals(5, Calc.add(2, 3));
+        assertEquals(-1, Calc.add(2, 3));
     }
 }
"""
    fake_llm = FakeLLMClient(responses=[cheat_diff] * 5)
    initial_state = build_initial_state(
        repo_root=str(repo),
        target_test=TARGET,
        language="java",
        max_iterations=10,
        no_progress_window=3,
        test_timeout=120.0,
        baseline_commit=baseline,
        last_known_good_commit=baseline,
    )

    result = run_loop(initial_state, llm_client=fake_llm, adapter=JavaMavenAdapter())

    assert result["status"] == "failed_no_progress"
    original_test_source = (FIXTURE_DIR / "src/test/java/com/example/CalcTest.java").read_text()
    assert (repo / "src/test/java/com/example/CalcTest.java").read_text() == original_test_source
    assert all(
        a["failure_signature"] and a["failure_signature"].startswith("PATCH_APPLY_ERROR")
        for a in result["attempts"]
    )
