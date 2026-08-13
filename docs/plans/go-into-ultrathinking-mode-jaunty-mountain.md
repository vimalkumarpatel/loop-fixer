# `loop_fixer` — Add explicit max-loops exit message

## Context

`loop_fixer` (built earlier this session at `/Users/vimalkumarpatel/git/test/loop_fixer/`) already exposes CLI parameters via `argparse` in `cli.py`, including `--max-iters` (default 5), which bounds the FSM loop — `fsm.py`'s `state_decide()` already stops with `state.status = "failed_max_iter"` once `iteration >= max_iterations`, and `cli.py`'s `EXIT_CODES` already maps `"failed_max_iter"` to process exit code `2`. So the CLI-parameter and exit-code mechanics the user is asking for already exist.

What's missing is the specific, literal error message: when the loop stops because it hit the iteration cap, the CLI should print exactly `Reached Maximum Allowed Loops` to stderr (confirmed with the user: stderr, bare string, no prefix — so it's exact-match friendly for scripts/graders) in addition to the existing `[result] ...` summary lines, and still exit with code `2`.

## Change

In `loop_fixer/cli.py`, in `main()`, after `state = run_loop(state)` and before/alongside the existing `[result]` print block: when `state.status == "failed_max_iter"`, print `Reached Maximum Allowed Loops` to `sys.stderr` (via `print(..., file=sys.stderr)`) as its own line — keep the existing `[result] status=... iterations=... branch=...` stdout summary line unchanged for the other stop conditions/success, since only this one exact string was requested. `sys` is already imported in `cli.py`.

No changes needed to `fsm.py` (its internal `_rollback_and_report` reason string like "max iterations reached" stays as the per-iteration trace log — that's a different, more verbose diagnostic line, not the literal error message being added) or to `EXIT_CODES` (already `2`).

## Critical file
- `loop_fixer/cli.py` — add the stderr print in `main()`'s result-handling block.

## Verification
1. Reuse the existing `--max-iters` flag against a scenario that can never converge (e.g. the existing `USELESS_DIFF` fake-LLM setup already used in `tests/test_e2e_demo.py::test_loop_stops_on_no_progress_when_unfixable`, but with `--no-progress-window` set high enough that the iteration cap is hit first instead of the no-progress detector) to confirm the message and exit code.
2. Add/extend a CLI-level test (e.g. in `tests/test_cli.py`, new file) that invokes `cli.main()` with a `FakeLLMClient` monkeypatched in place of `AnthropicLLMClient`, `--max-iters 2`, and an unfixable diff; assert the return value is `2` and that `Reached Maximum Allowed Loops` appears on captured stderr (via `capsys`).
3. Manually run `.venv/bin/python -m loop_fixer --test test_calc.py::test_add --repo /tmp/demo --max-iters 1` against a scratch broken repo (reusing the `scripts/demo_run.py` pattern, or a small ad hoc script swapping in `FakeLLMClient`) and confirm the process exit code is `2` and stderr contains exactly `Reached Maximum Allowed Loops`.
4. Run the full suite (`.venv/bin/python -m pytest tests/ -v`) to confirm no regressions.
