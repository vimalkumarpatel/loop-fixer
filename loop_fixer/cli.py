from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import git_checkpoint
from .adapters import JavaMavenAdapter, PythonPytestAdapter
from .adapters.base import LanguageAdapter
from .errors import AdapterError, GitCheckpointError, LLMError
from .fsm import build_initial_state, run_loop
from .llm_client import LangChainAnthropicClient

EXIT_CODES = {
    "success": 0,
    "failed_max_iter": 2,
    "failed_no_progress": 3,
    "failed_timeout": 4,
    "failed_error": 5,
}

ADAPTERS: dict[str, type[LanguageAdapter]] = {
    "python": PythonPytestAdapter,
    "java": JavaMavenAdapter,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="loop_fixer", description=__doc__)
    p.add_argument(
        "--test",
        required=True,
        help="test target: pytest node-id (tests/test_foo.py::test_bar) for --language python, "
        "or Surefire spec (com.example.FooTest#testBar) for --language java",
    )
    p.add_argument("--repo", default=".", help="repo root (default: cwd)")
    p.add_argument(
        "--language",
        choices=sorted(ADAPTERS),
        default="python",
        help="language/test-runner adapter to use (default: python)",
    )
    p.add_argument("--max-iters", type=int, default=5)
    p.add_argument("--max-seconds", type=float, default=300.0)
    p.add_argument("--no-progress-window", type=int, default=3)
    p.add_argument("--max-files-per-patch", type=int, default=3)
    p.add_argument("--pytest-timeout", type=float, default=60.0)
    p.add_argument("--summarize-failures", action="store_true")
    p.add_argument("--model", default="claude-sonnet-4-5")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(args.repo).resolve()
    adapter = ADAPTERS[args.language]()

    # Baseline test run — fail fast on a bad target before spending an LLM call.
    # Language-neutral: 0 means "already passing" (nothing to fix), anything
    # else means "proceed" — the no-progress detector guards against a
    # fundamentally broken invocation, so no per-adapter special case is needed.
    print(f"[preflight] baseline run: {adapter.name} {args.test}")
    try:
        baseline_result = adapter.run_test(repo_root, args.test, timeout=args.pytest_timeout)
    except AdapterError as exc:
        print(f"[preflight] {exc}")
        return EXIT_CODES["failed_error"]
    if baseline_result.returncode == 0:
        print("[preflight] target test already passes — nothing to fix")
        return EXIT_CODES["success"]

    try:
        branch, baseline_sha = git_checkpoint.preflight(repo_root, args.test)
    except GitCheckpointError as exc:
        print(f"[preflight] {exc}")
        return EXIT_CODES["failed_error"]
    print(f"[preflight] branch={branch} baseline={baseline_sha[:8]}")

    try:
        llm_client = LangChainAnthropicClient(model=args.model)
    except LLMError as exc:
        print(f"[preflight] {exc}")
        return EXIT_CODES["failed_error"]

    def on_event(line: str) -> None:
        print(line)

    initial_state = build_initial_state(
        repo_root=str(repo_root),
        target_test=args.test,
        language=args.language,
        max_iterations=args.max_iters,
        max_wall_seconds=args.max_seconds,
        no_progress_window=args.no_progress_window,
        max_files_per_patch=args.max_files_per_patch,
        test_timeout=args.pytest_timeout,
        summarize_failures=args.summarize_failures,
        baseline_commit=baseline_sha,
        last_known_good_commit=baseline_sha,
    )

    result = run_loop(initial_state, llm_client=llm_client, adapter=adapter, on_event=on_event)

    print(f"\n[result] status={result['status']} iterations={result['iteration']} branch={branch}")
    if result["status"] == "success":
        print(f"[result] fix committed on '{branch}'. Review with: git log {branch}")
    else:
        print(f"[result] rolled back to baseline; attempt history preserved on '{branch}'")
    print(f"[result] to return to your original branch: git checkout <your-branch>")

    if result["status"] == "failed_max_iter":
        print("Reached Maximum Allowed Loops", file=sys.stderr)

    return EXIT_CODES.get(result["status"], 5)


if __name__ == "__main__":
    sys.exit(main())
