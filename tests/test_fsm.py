from __future__ import annotations

from loop_fixer.fsm import build_initial_state, decide_node
from loop_fixer.test_runner import TestResult


def _passing_result_dict():
    return {"returncode": 0, "stdout": "1 passed", "stderr": "", "duration": 0.1, "timed_out": False}


def _failing_result_dict():
    return {"returncode": 1, "stdout": "AssertionError", "stderr": "", "duration": 0.1, "timed_out": False}


def _state(tmp_path, **overrides):
    state = build_initial_state(
        repo_root=str(tmp_path),
        target_test="tests/test_x.py::test_y",
        language="python",
        max_iterations=5,
        max_wall_seconds=300.0,
        no_progress_window=3,
    )
    state.update(overrides)
    return state


def _config():
    return {"configurable": {"adapter": None, "llm_client": None, "on_event": None}}


def test_decide_terminates_on_success(tmp_path, monkeypatch):
    import loop_fixer.git_checkpoint as gc

    monkeypatch.setattr(gc, "commit_attempt", lambda repo, msg: "deadbeef")
    state = _state(tmp_path)
    state["attempts"] = [
        {
            "iteration": 1,
            "diff_applied": "",
            "files_touched": ["x.py"],
            "test_result": _passing_result_dict(),
            "failure_signature": None,
            "commit_sha": None,
        }
    ]
    updates = decide_node(state, _config())
    assert updates["status"] == "success"
    assert updates["last_known_good_commit"] == "deadbeef"


def test_decide_stops_at_max_iterations(tmp_path, monkeypatch):
    import loop_fixer.git_checkpoint as gc

    monkeypatch.setattr(gc, "rollback", lambda repo, sha: None)
    state = _state(tmp_path, max_iterations=1, iteration=0)
    state["attempts"] = [
        {
            "iteration": 1,
            "diff_applied": "",
            "files_touched": ["x.py"],
            "test_result": _failing_result_dict(),
            "failure_signature": "1:AssertionError",
            "commit_sha": None,
        }
    ]
    updates = decide_node(state, _config())
    assert updates["status"] == "failed_max_iter"


def test_decide_stops_on_no_progress(tmp_path, monkeypatch):
    import loop_fixer.git_checkpoint as gc

    monkeypatch.setattr(gc, "rollback", lambda repo, sha: None)
    state = _state(tmp_path, max_iterations=10, no_progress_window=3)
    attempts = []
    for i in range(1, 4):
        attempts.append(
            {
                "iteration": i,
                "diff_applied": "",
                "files_touched": ["x.py"],
                "test_result": _failing_result_dict(),
                "failure_signature": "1:AssertionError",
                "commit_sha": None,
            }
        )
    state["attempts"] = attempts
    state["iteration"] = 2
    updates = decide_node(state, _config())
    assert updates["status"] == "failed_no_progress"


def test_decide_continues_when_progressing(tmp_path, monkeypatch):
    import loop_fixer.git_checkpoint as gc

    monkeypatch.setattr(gc, "commit_attempt", lambda repo, msg: "cafebabe")
    state = _state(tmp_path, max_iterations=5, no_progress_window=3)
    state["attempts"] = [
        {
            "iteration": 1,
            "diff_applied": "",
            "files_touched": ["x.py"],
            "test_result": _failing_result_dict(),
            "failure_signature": "1:AssertionError",
            "commit_sha": None,
        }
    ]
    updates = decide_node(state, _config())
    assert updates["status"] == "running"
    assert updates["iteration"] == 1
    assert updates["attempts"][-1]["commit_sha"] == "cafebabe"


def test_decide_no_progress_resets_after_different_signature(tmp_path, monkeypatch):
    import loop_fixer.git_checkpoint as gc

    monkeypatch.setattr(gc, "commit_attempt", lambda repo, msg: "cafebabe")
    state = _state(tmp_path, max_iterations=10, no_progress_window=3)
    sigs = ["1:A", "1:A", "1:B"]  # different signature breaks the streak
    attempts = []
    for i, sig in enumerate(sigs, start=1):
        attempts.append(
            {
                "iteration": i,
                "diff_applied": "",
                "files_touched": ["x.py"],
                "test_result": _failing_result_dict(),
                "failure_signature": sig,
                "commit_sha": None,
            }
        )
        state["attempts"] = attempts
        state["iteration"] = i - 1
        updates = decide_node(state, _config())
        assert updates["status"] == "running"
        state.update(updates)
        assert state["iteration"] == i
