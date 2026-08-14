"""Live demo: runs the real CLI pipeline (fsm/git/patch_apply/test_runner) against
the seeded broken_repo fixture, with FakeLLMClient standing in for the network call
to Anthropic (no ANTHROPIC_API_KEY needed for this demo). Swap in LangChainAnthropicClient
to use a real model -- see README.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loop_fixer import git_checkpoint
from loop_fixer.adapters.python_pytest import PythonPytestAdapter
from loop_fixer.fsm import build_initial_state, run_loop
from loop_fixer.llm_client import FakeLLMClient
from loop_fixer.test_runner import run_pytest

FIXING_DIFF = """\
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""


def main() -> int:
    repo_root = Path(sys.argv[1]).resolve()
    target = "test_calc.py::test_add"

    print(f"[preflight] baseline run: pytest {target}")
    baseline_result = run_pytest(repo_root, target)
    print(f"[preflight] baseline exit={baseline_result.returncode}")

    branch, baseline_sha = git_checkpoint.preflight(repo_root, target)
    print(f"[preflight] branch={branch} baseline={baseline_sha[:8]}")

    llm_client = FakeLLMClient(responses=[FIXING_DIFF])
    adapter = PythonPytestAdapter()

    initial_state = build_initial_state(
        repo_root=str(repo_root),
        target_test=target,
        language="python",
        max_iterations=5,
        no_progress_window=3,
        baseline_commit=baseline_sha,
        last_known_good_commit=baseline_sha,
    )
    result = run_loop(initial_state, llm_client=llm_client, adapter=adapter, on_event=print)

    print(f"\n[result] status={result['status']} iterations={result['iteration']} branch={branch}")
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
