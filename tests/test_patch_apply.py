from __future__ import annotations

import pytest

from loop_fixer.errors import PatchApplyError
from loop_fixer.patch_apply import apply_unified_diff


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    return tmp_path


VALID_DIFF = """\
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""


def test_valid_diff_applies(repo):
    touched = apply_unified_diff(repo, VALID_DIFF, writable_paths={"calc.py"})
    assert touched == ["calc.py"]
    assert "return a + b" in (repo / "calc.py").read_text()


def test_malformed_diff_rejected(repo):
    with pytest.raises(PatchApplyError):
        apply_unified_diff(repo, "not a diff at all", writable_paths={"calc.py"})


def test_path_traversal_rejected(repo):
    (repo.parent / "outside.py").write_text("x = 1\n")
    evil = """\
--- a/../outside.py
+++ b/../outside.py
@@ -1,1 +1,1 @@
-x = 1
+x = 2
"""
    with pytest.raises(PatchApplyError, match="escapes repo_root"):
        apply_unified_diff(repo, evil, writable_paths={"calc.py"})


def test_protected_test_file_rejected(repo):
    """The test file is never in writable_paths — this is the unfakeable-signal guard."""
    evil = """\
--- a/test_calc.py
+++ b/test_calc.py
@@ -1,4 +1,4 @@
 from calc import add

 def test_add():
-    assert add(2, 3) == 5
+    assert True
"""
    with pytest.raises(PatchApplyError, match="not in the writable-paths allowlist"):
        apply_unified_diff(repo, evil, writable_paths={"calc.py"})
    # File must be untouched.
    assert "assert add(2, 3) == 5" in (repo / "test_calc.py").read_text()


def test_too_many_files_rejected(repo):
    (repo / "extra1.py").write_text("a = 1\n")
    (repo / "extra2.py").write_text("b = 1\n")
    diff = """\
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
--- a/extra1.py
+++ b/extra1.py
@@ -1,1 +1,1 @@
-a = 1
+a = 2
--- a/extra2.py
+++ b/extra2.py
@@ -1,1 +1,1 @@
-b = 1
+b = 2
"""
    with pytest.raises(PatchApplyError, match="max_files_per_patch"):
        apply_unified_diff(repo, diff, writable_paths={"calc.py", "extra1.py", "extra2.py"}, max_files=2)


def test_stale_context_rejected(repo):
    stale = """\
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a * b
+    return a + b
"""
    with pytest.raises(PatchApplyError, match="does not exactly match"):
        apply_unified_diff(repo, stale, writable_paths={"calc.py"})


def test_nonexistent_target_file_rejected(repo):
    diff = """\
--- a/missing.py
+++ b/missing.py
@@ -1,1 +1,1 @@
-x = 1
+x = 2
"""
    with pytest.raises(PatchApplyError, match="does not exist"):
        apply_unified_diff(repo, diff, writable_paths={"missing.py"})


def test_markdown_fences_stripped(repo):
    fenced = "```diff\n" + VALID_DIFF + "```"
    touched = apply_unified_diff(repo, fenced, writable_paths={"calc.py"})
    assert touched == ["calc.py"]
