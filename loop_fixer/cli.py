from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import git_checkpoint
from .errors import GitCheckpointError, LLMError
from .fsm import LoopState, run_loop
from .llm_client import AnthropicLLMClient
from .test_runner import run_pytest

EXIT_CODES = {
    "success": 0,
    "failed_max_iter": 2,
    "failed_no_progress": 3,
    "failed_timeout": 4,
    "failed_error": 5,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="loop_fixer", description=__doc__)
    p.add_argument("--test", required=True, help="pytest target, e.g. tests/test_foo.py::test_bar")
    p.add_argument("--repo", default=".", help="repo root (default: cwd)")
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

    # Baseline pytest run — fail fast on a bad target before spending an LLM call.
    print(f"[preflight] baseline run: pytest {args.test}")
    baseline_result = run_pytest(repo_root, args.test, timeout=args.pytest_timeout)
    if baseline_result.returncode not in (0, 1):
        print(f"[preflight] target test could not be collected/run cleanly (exit {baseline_result.returncode})")
        print(baseline_result.stdout)
        print(baseline_result.stderr)
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
        llm_client = AnthropicLLMClient(model=args.model)
    except LLMError as exc:
        print(f"[preflight] {exc}")
        return EXIT_CODES["failed_error"]

    def on_event(line: str) -> None:
        print(line)

    state = LoopState(
        repo_root=repo_root,
        target_test=args.test,
        llm_client=llm_client,
        max_iterations=args.max_iters,
        max_wall_seconds=args.max_seconds,
        no_progress_window=args.no_progress_window,
        max_files_per_patch=args.max_files_per_patch,
        pytest_timeout=args.pytest_timeout,
        summarize_failures=args.summarize_failures,
        baseline_commit=baseline_sha,
        last_known_good_commit=baseline_sha,
        on_event=on_event,
    )

    state = run_loop(state)

    print(f"\n[result] status={state.status} iterations={state.iteration} branch={branch}")
    if state.status == "success":
        print(f"[result] fix committed on '{branch}'. Review with: git log {branch}")
    else:
        print(f"[result] rolled back to baseline; attempt history preserved on '{branch}'")
    print(f"[result] to return to your original branch: git checkout <your-branch>")

    if state.status == "failed_max_iter":
        print("Reached Maximum Allowed Loops", file=sys.stderr)

    return EXIT_CODES.get(state.status, 5)


if __name__ == "__main__":
    sys.exit(main())
