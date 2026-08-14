from __future__ import annotations

from pathlib import Path

from loop_fixer.adapters.python_pytest import PythonPytestAdapter
from loop_fixer.fsm import Attempt, LoopState, state_decide
from loop_fixer.test_runner import TestResult


def _passing_result():
    return TestResult(returncode=0, stdout="1 passed", stderr="", duration=0.1)


def _failing_result():
    return TestResult(returncode=1, stdout="AssertionError", stderr="", duration=0.1)


def _state(tmp_path, **overrides):
    defaults = dict(
        repo_root=tmp_path,
        target_test="tests/test_x.py::test_y",
        llm_client=None,
        adapter=PythonPytestAdapter(),
        max_iterations=5,
        max_wall_seconds=300.0,
        no_progress_window=3,
    )
    defaults.update(overrides)
    return LoopState(**defaults)


def test_decide_terminates_on_success(tmp_path, monkeypatch):
    import loop_fixer.git_checkpoint as gc

    monkeypatch.setattr(gc, "commit_attempt", lambda repo, msg: "deadbeef")
    state = _state(tmp_path)
    state.attempts.append(
        Attempt(iteration=1, diff_applied="", files_touched=["x.py"], test_result=_passing_result(), failure_signature=None)
    )
    next_state = state_decide(state)
    assert next_state == "TERMINATE"
    assert state.status == "success"
    assert state.last_known_good_commit == "deadbeef"


def test_decide_stops_at_max_iterations(tmp_path, monkeypatch):
    import loop_fixer.git_checkpoint as gc

    monkeypatch.setattr(gc, "rollback", lambda repo, sha: None)
    state = _state(tmp_path, max_iterations=1)
    state.iteration = 0
    state.attempts.append(
        Attempt(iteration=1, diff_applied="", files_touched=["x.py"], test_result=_failing_result(), failure_signature="1:AssertionError")
    )
    next_state = state_decide(state)
    assert next_state == "TERMINATE"
    assert state.status == "failed_max_iter"


def test_decide_stops_on_no_progress(tmp_path, monkeypatch):
    import loop_fixer.git_checkpoint as gc

    monkeypatch.setattr(gc, "rollback", lambda repo, sha: None)
    state = _state(tmp_path, max_iterations=10, no_progress_window=3)
    for i in range(1, 3):
        state.attempts.append(
            Attempt(iteration=i, diff_applied="", files_touched=["x.py"], test_result=_failing_result(), failure_signature="1:AssertionError")
        )
        state.iteration = i
    # Third identical failure should trip the no-progress detector.
    state.attempts.append(
        Attempt(iteration=3, diff_applied="", files_touched=["x.py"], test_result=_failing_result(), failure_signature="1:AssertionError")
    )
    next_state = state_decide(state)
    assert next_state == "TERMINATE"
    assert state.status == "failed_no_progress"


def test_decide_continues_when_progressing(tmp_path, monkeypatch):
    import loop_fixer.git_checkpoint as gc

    monkeypatch.setattr(gc, "commit_attempt", lambda repo, msg: "cafebabe")
    state = _state(tmp_path, max_iterations=5, no_progress_window=3)
    state.attempts.append(
        Attempt(iteration=1, diff_applied="", files_touched=["x.py"], test_result=_failing_result(), failure_signature="1:AssertionError")
    )
    next_state = state_decide(state)
    assert next_state == "EDIT"
    assert state.status == "running"
    assert state.iteration == 1
    assert state.attempts[-1].commit_sha == "cafebabe"


def test_decide_no_progress_resets_after_different_signature(tmp_path, monkeypatch):
    import loop_fixer.git_checkpoint as gc

    monkeypatch.setattr(gc, "commit_attempt", lambda repo, msg: "cafebabe")
    state = _state(tmp_path, max_iterations=10, no_progress_window=3)
    sigs = ["1:A", "1:A", "1:B"]  # different signature breaks the streak
    for i, sig in enumerate(sigs, start=1):
        state.attempts.append(
            Attempt(iteration=i, diff_applied="", files_touched=["x.py"], test_result=_failing_result(), failure_signature=sig)
        )
        next_state = state_decide(state)
        assert next_state == "EDIT"
        assert state.status == "running"
