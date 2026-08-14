"""Java analog of demo_run.py: runs the real CLI pipeline (fsm/git/patch_apply/
JavaMavenAdapter) against the seeded broken_repo_java fixture, with
FakeLLMClient standing in for the network call to Anthropic (no
ANTHROPIC_API_KEY needed for this demo). Requires `mvn` on PATH. Swap in
LangChainAnthropicClient to use a real model -- see README.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loop_fixer import git_checkpoint
from loop_fixer.adapters.java_maven import JavaMavenAdapter
from loop_fixer.fsm import build_initial_state, run_loop
from loop_fixer.llm_client import FakeLLMClient

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


def main() -> int:
    repo_root = Path(sys.argv[1]).resolve()
    target = "com.example.CalcTest#testAdd"
    adapter = JavaMavenAdapter()

    print(f"[preflight] baseline run: {adapter.name} {target}")
    baseline_result = adapter.run_test(repo_root, target, timeout=120.0)
    print(f"[preflight] baseline exit={baseline_result.returncode}")

    branch, baseline_sha = git_checkpoint.preflight(repo_root, target)
    print(f"[preflight] branch={branch} baseline={baseline_sha[:8]}")

    llm_client = FakeLLMClient(responses=[FIXING_DIFF])

    initial_state = build_initial_state(
        repo_root=str(repo_root),
        target_test=target,
        language="java",
        max_iterations=5,
        no_progress_window=3,
        test_timeout=120.0,
        baseline_commit=baseline_sha,
        last_known_good_commit=baseline_sha,
    )
    result = run_loop(initial_state, llm_client=llm_client, adapter=adapter, on_event=print)

    print(f"\n[result] status={result['status']} iterations={result['iteration']} branch={branch}")
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
