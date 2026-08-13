from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from loop_fixer import cli
from loop_fixer.llm_client import FakeLLMClient

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "broken_repo"

USELESS_DIFF = """\
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a - b  # noop
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


def test_max_iters_exits_2_with_exact_message(tmp_path, monkeypatch, capsys):
    repo = _seed_repo(tmp_path)

    monkeypatch.setattr(cli, "AnthropicLLMClient", lambda model: FakeLLMClient(responses=[USELESS_DIFF] * 10))

    exit_code = cli.main(
        [
            "--test",
            "test_calc.py::test_add",
            "--repo",
            str(repo),
            "--max-iters",
            "2",
            "--no-progress-window",
            "10",  # high enough that the iteration cap triggers first, not no-progress
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Reached Maximum Allowed Loops" in captured.err
    assert captured.err.strip().splitlines()[-1] == "Reached Maximum Allowed Loops"
