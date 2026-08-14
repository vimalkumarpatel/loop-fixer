from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Callable, List, Literal, Optional, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from . import context, git_checkpoint, patch_apply
from .adapters.base import LanguageAdapter
from .errors import GitCheckpointError, LLMError, PatchApplyError
from .llm_client import LLMClient

Status = Literal[
    "running",
    "success",
    "failed_max_iter",
    "failed_no_progress",
    "failed_timeout",
    "failed_error",
]


class GraphState(TypedDict, total=False):
    """Checkpoint-safe orchestration state.

    Only primitive/serializable data lives here — no LLMClient, LanguageAdapter,
    or on_event callback. Those are non-serializable runtime dependencies and are
    threaded through `config["configurable"]` instead (see `run_loop`), read by
    each node via its `config: RunnableConfig` parameter.
    """

    repo_root: str
    target_test: str
    language: str
    max_iterations: int
    max_wall_seconds: float
    no_progress_window: int
    max_files_per_patch: int
    test_timeout: float
    summarize_failures: bool

    started_at: float
    iteration: int
    attempts: List[dict]
    baseline_commit: Optional[str]
    last_known_good_commit: Optional[str]
    status: Status

    # Resolved by PLAN on first entry; test file is intentionally excluded.
    test_file: Optional[str]
    writable_paths: Optional[List[str]]
    last_summary: Optional[str]


def _emit(config: RunnableConfig, line: str) -> None:
    on_event = config.get("configurable", {}).get("on_event")
    if on_event:
        on_event(line)


def _adapter(config: RunnableConfig) -> LanguageAdapter:
    return config["configurable"]["adapter"]


def _llm_client(config: RunnableConfig) -> LLMClient:
    return config["configurable"]["llm_client"]


def plan_node(state: GraphState, config: RunnableConfig) -> dict:
    if state.get("test_file") is not None:
        return {}

    adapter = _adapter(config)
    repo_root = Path(state["repo_root"])
    test_file = adapter.resolve_test_file(repo_root, state["target_test"])
    writable_paths = adapter.resolve_writable_paths(repo_root, test_file)
    _emit(
        config,
        f"[iter {state['iteration'] + 1}] PLAN   resolved writable files: "
        f"{', '.join(sorted(writable_paths)) or '(none found)'}",
    )
    return {
        "test_file": str(test_file),
        "writable_paths": sorted(writable_paths),
    }


def _build_patch_prompt(state: GraphState) -> str:
    template = (Path(__file__).parent / "prompts" / "generate_patch.txt").read_text()
    repo_root = Path(state["repo_root"])
    test_file = Path(state["test_file"])
    writable_paths = state.get("writable_paths") or []
    test_source = test_file.read_text()
    source_files = "\n\n".join(
        f"--- {p} ---\n{(repo_root / p).read_text()}" for p in sorted(writable_paths)
    )
    attempts = state.get("attempts") or []
    last_output = ""
    if attempts:
        last_result = attempts[-1].get("test_result")
        if last_result is not None:
            last_output = context.truncate_output(last_result["stdout"] + "\n" + last_result["stderr"])
    history_lines = []
    for a in attempts:
        outcome = a.get("failure_signature") or "passed"
        history_lines.append(f"attempt {a['iteration']}: applied diff to {a['files_touched']} -> {outcome}")
    return template.format(
        test_path=state["target_test"],
        test_source=test_source,
        source_files=source_files or "(no local source files resolved)",
        test_output=last_output or "(no prior run yet)",
        attempt_history="\n".join(history_lines) or "(none yet)",
    )


def edit_node(state: GraphState, config: RunnableConfig) -> dict:
    n = state["iteration"] + 1
    llm_client = _llm_client(config)
    prompt = _build_patch_prompt(state)
    try:
        diff_text = llm_client.generate(prompt)
    except LLMError as exc:
        _emit(config, f"[iter {n}] EDIT   LLM call failed: {exc}")
        return {"status": "failed_error"}

    repo_root = Path(state["repo_root"])
    writable_paths = set(state.get("writable_paths") or [])
    try:
        touched = patch_apply.apply_unified_diff(
            repo_root,
            diff_text,
            writable_paths=writable_paths,
            max_files=state["max_files_per_patch"],
        )
    except PatchApplyError as exc:
        _emit(config, f"[iter {n}] EDIT   patch rejected: {exc}")
        attempt = {
            "iteration": n,
            "diff_applied": diff_text,
            "files_touched": [],
            "test_result": None,
            "failure_signature": f"PATCH_APPLY_ERROR:{exc}",
            "commit_sha": None,
        }
        return {"attempts": (state.get("attempts") or []) + [attempt]}

    _emit(config, f"[iter {n}] EDIT   applied diff ({len(touched)} file(s): {', '.join(touched)})")
    attempt = {
        "iteration": n,
        "diff_applied": diff_text,
        "files_touched": touched,
        "test_result": None,
        "failure_signature": None,
        "commit_sha": None,
    }
    return {"attempts": (state.get("attempts") or []) + [attempt]}


def test_node(state: GraphState, config: RunnableConfig) -> dict:
    if state.get("status") == "failed_error":
        # EDIT's LLM call failed before any attempt was recorded; short-circuit
        # exactly like the original FSM (no rollback, iteration untouched).
        return {}

    n = state["iteration"] + 1
    attempts = list(state.get("attempts") or [])
    last = attempts[-1]

    if last.get("failure_signature", "") and str(last.get("failure_signature")).startswith("PATCH_APPLY_ERROR"):
        # EDIT already recorded a terminal-for-this-attempt failure (patch rejected);
        # nothing to run — skip straight through with the existing attempt record.
        return {}

    adapter = _adapter(config)
    repo_root = Path(state["repo_root"])
    result = adapter.run_test(repo_root, state["target_test"], timeout=state["test_timeout"])
    outcome = "exit 0 (PASS)" if result.passed else f"exit {result.returncode}"
    _emit(config, f"[iter {n}] TEST   {adapter.name} {state['target_test']} -> {outcome} ({result.duration:.2f}s)")

    last = dict(last)
    last["test_result"] = {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration": result.duration,
        "timed_out": result.timed_out,
    }
    attempts[-1] = last
    return {"attempts": attempts}


def analyze_node(state: GraphState, config: RunnableConfig) -> dict:
    if state.get("status") == "failed_error":
        return {}

    n = state["iteration"] + 1
    attempts = list(state.get("attempts") or [])
    attempt = dict(attempts[-1])
    result_dict = attempt.get("test_result")

    if result_dict is None:
        # A PATCH_APPLY_ERROR attempt never ran the test; nothing to analyze.
        return {}

    if result_dict["returncode"] == 0 and not result_dict["timed_out"]:
        attempt["failure_signature"] = None
        attempts[-1] = attempt
        _emit(config, f"[iter {n}] ANALYZE  test passed")
        return {"attempts": attempts}

    adapter = _adapter(config)
    from .test_runner import TestResult

    result = TestResult(
        returncode=result_dict["returncode"],
        stdout=result_dict["stdout"],
        stderr=result_dict["stderr"],
        duration=result_dict["duration"],
        timed_out=result_dict["timed_out"],
    )
    signature = adapter.compute_signature(result)
    attempt["failure_signature"] = signature
    attempts[-1] = attempt
    _emit(config, f"[iter {n}] ANALYZE  signature={signature}")

    updates: dict[str, Any] = {"attempts": attempts}
    if state.get("summarize_failures"):
        llm_client = _llm_client(config)
        try:
            template = (Path(__file__).parent / "prompts" / "summarize_failure.txt").read_text()
            summary_prompt = template.format(
                test_output=context.truncate_output(result.stdout + "\n" + result.stderr)
            )
            updates["last_summary"] = llm_client.generate(summary_prompt, max_tokens=200)
        except LLMError:
            updates["last_summary"] = None  # non-fatal; ANALYZE's summary is a nice-to-have

    return updates


def _rollback_and_report(config: RunnableConfig, state: GraphState, n: int, reason: str) -> None:
    repo_root = Path(state["repo_root"])
    target = state.get("baseline_commit") or state.get("last_known_good_commit")
    try:
        git_checkpoint.rollback(repo_root, target)
    except GitCheckpointError as exc:
        _emit(config, f"[iter {n}] DECIDE  rollback failed: {exc}")
    _emit(config, f"[iter {n}] DECIDE  stop ({reason}) -> rolled back to baseline, terminate")


def decide_node(state: GraphState, config: RunnableConfig) -> dict:
    if state.get("status") == "failed_error":
        # Matches the original FSM: LLM/network failure short-circuits without
        # ever reaching DECIDE's rollback/checkpoint logic.
        return {}

    n = state["iteration"] + 1
    attempts = list(state.get("attempts") or [])
    attempt = dict(attempts[-1])
    repo_root = Path(state["repo_root"])
    result_dict = attempt.get("test_result")

    if result_dict is not None and result_dict["returncode"] == 0 and not result_dict["timed_out"]:
        sha = git_checkpoint.commit_attempt(repo_root, f"loop_fixer attempt {n}: passed")
        attempt["commit_sha"] = sha
        attempts[-1] = attempt
        _emit(config, f"[iter {n}] DECIDE  success -> checkpoint {sha[:8]}, terminate")
        return {
            "attempts": attempts,
            "status": "success",
            "iteration": n,
            "last_known_good_commit": sha,
        }

    if n >= state["max_iterations"]:
        _rollback_and_report(config, state, n, "max iterations reached")
        return {"status": "failed_max_iter", "iteration": n}

    if time.monotonic() - state["started_at"] > state["max_wall_seconds"]:
        _rollback_and_report(config, state, n, "wall-clock budget exceeded")
        return {"status": "failed_timeout", "iteration": n}

    window = attempts[-state["no_progress_window"] :]
    if len(window) == state["no_progress_window"] and len({a.get("failure_signature") for a in window}) == 1:
        _rollback_and_report(config, state, n, f"same failure signature repeated {state['no_progress_window']}x")
        return {"status": "failed_no_progress", "iteration": n}

    sha = git_checkpoint.commit_attempt(repo_root, f"loop_fixer attempt {n}: {attempt.get('failure_signature')}")
    attempt["commit_sha"] = sha
    attempts[-1] = attempt
    _emit(
        config,
        f"[iter {n}] DECIDE  continue -> checkpoint {sha[:8]} "
        f"({n}/{state['max_iterations']} iters)",
    )
    return {"attempts": attempts, "status": "running", "iteration": n}


_graph = StateGraph(GraphState)
_graph.add_node("plan", plan_node)
_graph.add_node("edit", edit_node)
_graph.add_node("test", test_node)
_graph.add_node("analyze", analyze_node)
_graph.add_node("decide", decide_node)
_graph.add_edge(START, "plan")
_graph.add_edge("plan", "edit")
_graph.add_edge("edit", "test")
_graph.add_edge("test", "analyze")
_graph.add_edge("analyze", "decide")
_graph.add_conditional_edges(
    "decide",
    lambda state: "continue" if state["status"] == "running" else "terminate",
    {"continue": "edit", "terminate": END},
)
compiled = _graph.compile(checkpointer=InMemorySaver())


def run_loop(
    initial_state: dict,
    *,
    llm_client: LLMClient,
    adapter: LanguageAdapter,
    on_event: Callable[[str], None] | None = None,
    thread_id: str | None = None,
) -> dict:
    """Public entrypoint: drives the compiled StateGraph to completion.

    `initial_state` is a plain dict of checkpoint-safe `GraphState` fields (see
    `build_initial_state` for a convenience constructor). `llm_client`, `adapter`,
    and `on_event` are non-serializable runtime dependencies passed via
    `config["configurable"]` rather than embedded in state.
    """
    config: RunnableConfig = {
        "configurable": {
            "llm_client": llm_client,
            "adapter": adapter,
            "on_event": on_event,
            "thread_id": thread_id or str(uuid.uuid4()),
        },
        "recursion_limit": max(100, initial_state.get("max_iterations", 5) * 10 + 50),
    }
    return compiled.invoke(initial_state, config=config)


def build_initial_state(
    *,
    repo_root: str,
    target_test: str,
    language: str,
    max_iterations: int = 5,
    max_wall_seconds: float = 300.0,
    no_progress_window: int = 3,
    max_files_per_patch: int = 3,
    test_timeout: float = 60.0,
    summarize_failures: bool = False,
    baseline_commit: str | None = None,
    last_known_good_commit: str | None = None,
) -> dict:
    """Convenience constructor for a fresh `GraphState` dict, mirroring the old
    `LoopState(...)` call sites (tests, `cli.py`, demo scripts)."""
    return {
        "repo_root": repo_root,
        "target_test": target_test,
        "language": language,
        "max_iterations": max_iterations,
        "max_wall_seconds": max_wall_seconds,
        "no_progress_window": no_progress_window,
        "max_files_per_patch": max_files_per_patch,
        "test_timeout": test_timeout,
        "summarize_failures": summarize_failures,
        "started_at": time.monotonic(),
        "iteration": 0,
        "attempts": [],
        "baseline_commit": baseline_commit,
        "last_known_good_commit": last_known_good_commit,
        "status": "running",
        "test_file": None,
        "writable_paths": None,
        "last_summary": None,
    }
