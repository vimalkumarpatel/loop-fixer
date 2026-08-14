"""Hermetic unit tests for JavaMavenAdapter's pure logic — no real `mvn`
invocation, so these run everywhere the rest of the suite does."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from loop_fixer.adapters.java_maven import JavaMavenAdapter
from loop_fixer.errors import AdapterError
from loop_fixer.test_runner import TestResult

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "broken_repo_java"


SUREFIRE_FAILURE_OUTPUT = """\
[ERROR] Tests run: 1, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 0.020 s <<< FAILURE! -- in com.example.CalcTest
[ERROR] com.example.CalcTest.testAdd -- Time elapsed: 0.003 s <<< FAILURE!
java.lang.AssertionError: expected:<5> but was:<-1>
\tat org.junit.Assert.fail(Assert.java:89)
\tat org.junit.Assert.failNotEquals(Assert.java:835)
\tat org.junit.Assert.assertEquals(Assert.java:647)
\tat com.example.CalcTest.testAdd(CalcTest.java:10)
[ERROR] Failures:
[ERROR]   CalcTest.testAdd:10 expected:<5> but was:<-1>
[ERROR] Tests run: 1, Failures: 1, Errors: 0, Skipped: 0
"""


def test_resolve_test_file_converts_dots_to_path():
    adapter = JavaMavenAdapter()
    resolved = adapter.resolve_test_file(FIXTURE_DIR, "com.example.CalcTest#testAdd")
    assert resolved == (FIXTURE_DIR / "src/test/java/com/example/CalcTest.java").resolve()


def test_resolve_test_file_without_method_suffix():
    adapter = JavaMavenAdapter()
    resolved = adapter.resolve_test_file(FIXTURE_DIR, "com.example.CalcTest")
    assert resolved == (FIXTURE_DIR / "src/test/java/com/example/CalcTest.java").resolve()


def test_resolve_writable_paths_via_convention_and_imports():
    adapter = JavaMavenAdapter()
    test_file = adapter.resolve_test_file(FIXTURE_DIR, "com.example.CalcTest#testAdd")
    writable = adapter.resolve_writable_paths(FIXTURE_DIR, test_file)
    assert writable == {"src/main/java/com/example/Calc.java"}


def test_resolve_writable_paths_never_includes_test_file():
    adapter = JavaMavenAdapter()
    test_file = adapter.resolve_test_file(FIXTURE_DIR, "com.example.CalcTest#testAdd")
    writable = adapter.resolve_writable_paths(FIXTURE_DIR, test_file)
    assert "src/test/java/com/example/CalcTest.java" not in writable


def test_resolve_writable_paths_convention_handles_missing_main_class(tmp_path):
    adapter = JavaMavenAdapter()
    test_dir = tmp_path / "src" / "test" / "java" / "com" / "example"
    test_dir.mkdir(parents=True)
    test_file = test_dir / "OrphanTest.java"
    test_file.write_text("package com.example;\n\npublic class OrphanTest {}\n")
    writable = adapter.resolve_writable_paths(tmp_path, test_file)
    assert writable == set()


def test_resolve_writable_paths_via_import_of_helper_class(tmp_path):
    adapter = JavaMavenAdapter()
    main_dir = tmp_path / "src" / "main" / "java" / "com" / "example" / "util"
    main_dir.mkdir(parents=True)
    (main_dir / "Helper.java").write_text("package com.example.util;\n\npublic class Helper {}\n")

    test_dir = tmp_path / "src" / "test" / "java" / "com" / "example"
    test_dir.mkdir(parents=True)
    test_file = test_dir / "SomeTest.java"
    test_file.write_text(
        "package com.example;\n\n"
        "import com.example.util.Helper;\n"
        "import static org.junit.Assert.assertEquals;\n\n"
        "public class SomeTest {}\n"
    )
    writable = adapter.resolve_writable_paths(tmp_path, test_file)
    assert writable == {"src/main/java/com/example/util/Helper.java"}


def test_compute_signature_extracts_exception_line():
    adapter = JavaMavenAdapter()
    result = TestResult(returncode=1, stdout=SUREFIRE_FAILURE_OUTPUT, stderr="", duration=0.5)
    signature = adapter.compute_signature(result)
    assert signature.startswith("1:")
    assert "AssertionError" in signature
    assert "expected" in signature


def test_compute_signature_timeout():
    adapter = JavaMavenAdapter()
    result = TestResult(returncode=-1, stdout="", stderr="", duration=60.0, timed_out=True)
    assert adapter.compute_signature(result) == "TIMEOUT"


def test_compute_signature_deterministic_across_volatile_paths():
    """Two runs with different tmp paths/line numbers collapse to the same signature."""
    adapter = JavaMavenAdapter()
    out1 = SUREFIRE_FAILURE_OUTPUT.replace("CalcTest.java:10", "CalcTest.java:99")
    out2 = SUREFIRE_FAILURE_OUTPUT
    r1 = TestResult(returncode=1, stdout=out1, stderr="", duration=0.1)
    r2 = TestResult(returncode=1, stdout=out2, stderr="", duration=0.1)
    assert adapter.compute_signature(r1) == adapter.compute_signature(r2)


def test_run_test_raises_adapter_error_when_mvn_missing(monkeypatch):
    adapter = JavaMavenAdapter()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(AdapterError):
        adapter.run_test(FIXTURE_DIR, "com.example.CalcTest#testAdd", timeout=10.0)
