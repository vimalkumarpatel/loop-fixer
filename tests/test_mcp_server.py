"""Tests for the MCP server front door. Skipped when the optional `mcp` extra
isn't installed (e.g. Python < 3.10, or a plain `pip install -e .` without
`[mcp]`) so the base hermetic suite stays runnable everywhere loop_fixer's
CLI path works."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import anyio
import pytest

pytest.importorskip("mcp")

from loop_fixer import mcp_server
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


class FakeContext:
    def __init__(self):
        self.messages: list[str] = []

    async def info(self, line: str) -> None:
        self.messages.append(line)


def _seed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE_DIR, repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def test_fix_test_converges_and_streams_progress(tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path)
    monkeypatch.setattr(mcp_server, "LangChainAnthropicClient", lambda model: FakeLLMClient(responses=[FIXING_DIFF]))

    ctx = FakeContext()
    summary = anyio.run(
        lambda: mcp_server._run_fix_test(ctx, test="test_calc.py::test_add", repo=str(repo))
    )

    assert "status=success" in summary
    assert "fix committed" in summary
    assert (repo / "calc.py").read_text().strip().endswith("return a + b")
    assert any("preflight" in m for m in ctx.messages)
    branches = subprocess.run(
        ["git", "branch", "--list", "loop-fixer/*"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert "loop-fixer/" in branches


def test_fix_test_max_iters_reports_failed_status(tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path)
    monkeypatch.setattr(
        mcp_server, "LangChainAnthropicClient", lambda model: FakeLLMClient(responses=[USELESS_DIFF] * 10)
    )

    ctx = FakeContext()
    summary = anyio.run(
        lambda: mcp_server._run_fix_test(
            ctx,
            test="test_calc.py::test_add",
            repo=str(repo),
            max_iters=2,
            no_progress_window=10,
        )
    )

    assert "status=failed_max_iter" in summary
    assert "rolled back" in summary


def test_fix_test_already_passing_skips_llm(tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path)
    # calc.py's bug makes test_add fail; patch it directly so the baseline passes.
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(["git", "commit", "-am", "fix"], cwd=repo, check=True)

    def _fail_construct(model):
        raise AssertionError("LLM client should never be constructed when baseline already passes")

    monkeypatch.setattr(mcp_server, "LangChainAnthropicClient", _fail_construct)

    ctx = FakeContext()
    summary = anyio.run(lambda: mcp_server._run_fix_test(ctx, test="test_calc.py::test_add", repo=str(repo)))

    assert "already passes" in summary
