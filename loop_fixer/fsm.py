from __future__ import annotations

import ast
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from . import context, git_checkpoint, patch_apply
from .errors import GitCheckpointError, LLMError, PatchApplyError
from .llm_client import LLMClient
from .test_runner import TestResult, run_pytest

Status = Literal[
    "running",
    "success",
    "failed_max_iter",
    "failed_no_progress",
    "failed_timeout",
    "failed_error",
]


@dataclass
class Attempt:
    iteration: int
    diff_applied: str
    files_touched: list[str]
    test_result: TestResult | None
    failure_signature: str | None  # None means passed
    commit_sha: str | None = None


@dataclass
class LoopState:
    repo_root: Path
    target_test: str
    llm_client: LLMClient
    max_iterations: int = 5
    max_wall_seconds: float = 300.0
    no_progress_window: int = 3
    max_files_per_patch: int = 3
    pytest_timeout: float = 60.0
    summarize_failures: bool = False

    started_at: float = field(default_factory=time.monotonic)
    iteration: int = 0
    attempts: list[Attempt] = field(default_factory=list)
    baseline_commit: str | None = None
    last_known_good_commit: str | None = None
    status: Status = "running"

    # Resolved by PLAN on first entry; test file is intentionally excluded.
    test_file: Path | None = None
    writable_paths: set[str] | None = None
    last_summary: str | None = None

    on_event: Callable[[str], None] | None = None  # per-iteration observability hook

    def emit(self, line: str) -> None:
        if self.on_event:
            self.on_event(line)


def _target_to_path(repo_root: Path, target_test: str) -> Path:
    file_part = target_test.split("::", 1)[0]
    return (repo_root / file_part).resolve()


def _resolve_import_files(repo_root: Path, test_file: Path) -> set[str]:
    """Statically resolve local (same-repo) modules imported by the test file.

    This is the only mechanism used to populate writable_paths — the test
    file itself is never included, which is what makes it non-writable.
    """
    tree = ast.parse(test_file.read_text())
    module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                module_names.add(node.module.split(".")[0])

    writable: set[str] = set()
    for name in module_names:
        candidate = repo_root / f"{name}.py"
        if candidate.exists():
            writable.add(str(candidate.relative_to(repo_root)))
    return writable


def state_plan(state: LoopState) -> str:
    if state.test_file is None:
        state.test_file = _target_to_path(state.repo_root, state.target_test)
        state.writable_paths = _resolve_import_files(state.repo_root, state.test_file)
        state.emit(
            f"[iter {state.iteration + 1}] PLAN   resolved writable files: "
            f"{', '.join(sorted(state.writable_paths)) or '(none found)'}"
        )
    return "EDIT"


def _build_patch_prompt(state: LoopState) -> str:
    template = (Path(__file__).parent / "prompts" / "generate_patch.txt").read_text()
    test_source = state.test_file.read_text()
    source_files = "\n\n".join(
        f"--- {p} ---\n{(state.repo_root / p).read_text()}" for p in sorted(state.writable_paths)
    )
    last_output = ""
    if state.attempts:
        last_result = state.attempts[-1].test_result
        if last_result is not None:
            last_output = context.truncate_output(last_result.stdout + "\n" + last_result.stderr)
    history_lines = []
    for a in state.attempts:
        outcome = a.failure_signature or "passed"
        history_lines.append(f"attempt {a.iteration}: applied diff to {a.files_touched} -> {outcome}")
    return template.format(
        test_path=state.target_test,
        test_source=test_source,
        source_files=source_files or "(no local source files resolved)",
        test_output=last_output or "(no prior run yet)",
        attempt_history="\n".join(history_lines) or "(none yet)",
    )


def state_edit(state: LoopState) -> str:
    n = state.iteration + 1
    prompt = _build_patch_prompt(state)
    try:
        diff_text = state.llm_client.generate(prompt)
    except LLMError as exc:
        state.status = "failed_error"
        state.emit(f"[iter {n}] EDIT   LLM call failed: {exc}")
        return "DECIDE"

    try:
        touched = patch_apply.apply_unified_diff(
            state.repo_root,
            diff_text,
            writable_paths=state.writable_paths or set(),
            max_files=state.max_files_per_patch,
        )
    except PatchApplyError as exc:
        state.emit(f"[iter {n}] EDIT   patch rejected: {exc}")
        state.attempts.append(
            Attempt(
                iteration=n,
                diff_applied=diff_text,
                files_touched=[],
                test_result=None,
                failure_signature=f"PATCH_APPLY_ERROR:{exc}",
            )
        )
        return "DECIDE"

    state.emit(f"[iter {n}] EDIT   applied diff ({len(touched)} file(s): {', '.join(touched)})")
    state.attempts.append(
        Attempt(iteration=n, diff_applied=diff_text, files_touched=touched, test_result=None, failure_signature=None)
    )
    return "TEST"


def state_test(state: LoopState) -> str:
    n = state.iteration + 1
    result = run_pytest(state.repo_root, state.target_test, timeout=state.pytest_timeout)
    state.attempts[-1].test_result = result
    outcome = "exit 0 (PASS)" if result.passed else f"exit {result.returncode}"
    state.emit(f"[iter {n}] TEST   pytest {state.target_test} -> {outcome} ({result.duration:.2f}s)")
    return "ANALYZE"


def state_analyze(state: LoopState) -> str:
    n = state.iteration + 1
    attempt = state.attempts[-1]
    result = attempt.test_result
    assert result is not None

    if result.passed:
        attempt.failure_signature = None
        state.emit(f"[iter {n}] ANALYZE  test passed")
        return "DECIDE"

    signature = context.compute_signature(result)
    attempt.failure_signature = signature
    state.emit(f"[iter {n}] ANALYZE  signature={signature}")

    if state.summarize_failures:
        try:
            template = (Path(__file__).parent / "prompts" / "summarize_failure.txt").read_text()
            summary_prompt = template.format(
                test_output=context.truncate_output(result.stdout + "\n" + result.stderr)
            )
            state.last_summary = state.llm_client.generate(summary_prompt, max_tokens=200)
        except LLMError:
            state.last_summary = None  # non-fatal; ANALYZE's summary is a nice-to-have

    return "DECIDE"


def state_decide(state: LoopState) -> str:
    n = state.iteration + 1
    attempt = state.attempts[-1]

    if attempt.test_result is not None and attempt.test_result.passed:
        sha = git_checkpoint.commit_attempt(state.repo_root, f"loop_fixer attempt {n}: passed")
        attempt.commit_sha = sha
        state.last_known_good_commit = sha
        state.status = "success"
        state.iteration = n
        state.emit(f"[iter {n}] DECIDE  success -> checkpoint {sha[:8]}, terminate")
        return "TERMINATE"

    if n >= state.max_iterations:
        state.status = "failed_max_iter"
        state.iteration = n
        _rollback_and_report(state, n, "max iterations reached")
        return "TERMINATE"

    if time.monotonic() - state.started_at > state.max_wall_seconds:
        state.status = "failed_timeout"
        state.iteration = n
        _rollback_and_report(state, n, "wall-clock budget exceeded")
        return "TERMINATE"

    window = state.attempts[-state.no_progress_window :]
    if len(window) == state.no_progress_window and len({a.failure_signature for a in window}) == 1:
        state.status = "failed_no_progress"
        state.iteration = n
        _rollback_and_report(state, n, f"same failure signature repeated {state.no_progress_window}x")
        return "TERMINATE"

    sha = git_checkpoint.commit_attempt(state.repo_root, f"loop_fixer attempt {n}: {attempt.failure_signature}")
    attempt.commit_sha = sha
    state.iteration = n
    state.emit(
        f"[iter {n}] DECIDE  continue -> checkpoint {sha[:8]} "
        f"({n}/{state.max_iterations} iters)"
    )
    return "EDIT"


def _rollback_and_report(state: LoopState, n: int, reason: str) -> None:
    try:
        git_checkpoint.rollback(state.repo_root, state.baseline_commit or state.last_known_good_commit)
    except GitCheckpointError as exc:
        state.emit(f"[iter {n}] DECIDE  rollback failed: {exc}")
    state.emit(f"[iter {n}] DECIDE  stop ({reason}) -> rolled back to baseline, terminate")


_DISPATCH: dict[str, Callable[[LoopState], str]] = {
    "PLAN": state_plan,
    "EDIT": state_edit,
    "TEST": state_test,
    "ANALYZE": state_analyze,
    "DECIDE": state_decide,
}


def run_loop(state: LoopState) -> LoopState:
    current = "PLAN"
    while state.status == "running":
        current = _DISPATCH[current](state)
        if current == "TERMINATE":
            break
    return state
