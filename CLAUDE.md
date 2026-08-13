# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`loop_fixer` is a deterministic FSM agent loop that takes a failing pytest test and iterates **Plan → Edit → Test → Analyze → Decide** — generating a patch via an LLM, applying it, re-running the test, and deciding whether to continue, stop-success, or stop-fail — until the test passes or a bounded stop condition is hit.

The core design guarantee: **the agent cannot fake success.** The test file, fixtures, and pytest config are hard-excluded from the set of files a patch is allowed to touch (enforced in code, not by prompting), and every verification run is a fresh `pytest` subprocess whose exit code is the only signal that matters.

## Commands

Setup:
```bash
python3 -m venv .venv && .venv/bin/pip install -e .
```

Run the full test suite (unit tests + hermetic end-to-end demo, no network calls — this is what CI/a reviewer would run):
```bash
.venv/bin/python -m pytest tests/ -v
```

Run a single test:
```bash
.venv/bin/python -m pytest tests/test_fsm.py::test_name -v
```

Run the CLI against a real target repo (requires `ANTHROPIC_API_KEY`):
```bash
export ANTHROPIC_API_KEY=sk-...
.venv/bin/python -m loop_fixer --test path/to/test_file.py::test_name --repo /path/to/target/repo
```
The target repo must be a git repo with a clean working tree, or the tool refuses to start.

Verify the loop manually without an API key, using `scripts/demo_run.py` (runs the real `fsm.py`/`git_checkpoint.py`/`patch_apply.py`/`test_runner.py` pipeline with a scripted `FakeLLMClient`):
```bash
rm -rf /tmp/demo
cp -r tests/fixtures/broken_repo /tmp/demo
cd /tmp/demo && git init -q && git config user.email demo@example.com \
  && git config user.name Demo && git add -A && git commit -q -m init
cd /Users/vimalkumarpatel/git/test
.venv/bin/python scripts/demo_run.py /tmp/demo
```
Expected output ends with `[result] status=success iterations=1 ...`, preceded by one line per FSM state transition (PLAN/EDIT/TEST/ANALYZE/DECIDE).

Note: `tests/fixtures` is excluded from normal pytest collection via `[tool.pytest.ini_options] addopts = "--ignore=tests/fixtures"` in `pyproject.toml` — those files are fixture inputs for the demo/e2e test, not tests to run directly.

## Architecture

### Five FSM states, one loop (`loop_fixer/fsm.py`)

```
PLAN --> EDIT --> TEST --> ANALYZE --> DECIDE
DECIDE -- "continue" --> EDIT
DECIDE -- "success or bound hit" --> TERMINATE
```

Each state does exactly one job, and responsibilities are strictly partitioned — this separation is what makes the anti-cheating guarantee possible:
- **PLAN** resolves which files are writable, statically, from the failing test's imports.
- **EDIT** calls the LLM for a patch and applies it via `patch_apply.py`.
- **TEST** is the *only* state allowed to run pytest (`test_runner.py`).
- **ANALYZE** computes a deterministic failure signature (`context.py`).
- **DECIDE** is the *only* state allowed to check stop conditions or touch git (`git_checkpoint.py`).

### Module map (`loop_fixer/`)
- `fsm.py` — `LoopState`/`Attempt` dataclasses + the five FSM states + `run_loop()`. The orchestration core.
- `patch_apply.py` — unified-diff parsing and application, plus every safety guard: rejects diff hunks touching paths outside `writable_paths` or containing path traversal, before a byte reaches disk.
- `test_runner.py` — subprocess wrapper around pytest; the only place that shells out to run tests. This is the verification signal — an OS exit code, never an LLM's self-report.
- `git_checkpoint.py` — preflight checks (clean tree required), branch/commit-per-attempt, rollback on failure.
- `llm_client.py` — `LLMClient` Protocol + `AnthropicLLMClient` (real) + `FakeLLMClient` (scripted, used by tests/demo — no network).
- `context.py` — output truncation and failure-signature normalization (used by ANALYZE for the no-progress check).
- `cli.py` — argparse entrypoint; runs a baseline pytest pass before spending any LLM call, then drives `run_loop()`.
- `prompts/` — patch-generation and failure-summary prompt templates (`.txt`, packaged via `[tool.setuptools.package-data]`).

### Bounded authority model
What the loop can read/write/execute is an explicit allowlist, not a convention — see the README's "Bounded authority" table for the full enforcement mapping. Key points when touching this code:
- Writes are restricted to `writable_paths`, computed from the target test's static import graph — never the test file, `conftest.py`, or pytest config.
- The only subprocess call site for running tests is `test_runner.py`, invoked as `python -m pytest <target>` with code-controlled args — LLM output is never placed in a command line.
- Every attempt is git-committed on a disposable `loop-fixer/<slug>/<timestamp>` branch; the user's original branch is never checked out or modified. Any terminal failure runs `git reset --hard` back to the pre-loop commit.

### Stop conditions and exit codes (`cli.py: EXIT_CODES`)
| Condition | Flag (default) | Status | Exit code |
|---|---|---|---|
| Test passes | — | `success` | `0` |
| Iteration cap | `--max-iters` (5) | `failed_max_iter` | `2` |
| No progress (same failure signature repeats) | `--no-progress-window` (3) | `failed_no_progress` | `3` |
| Wall-clock budget | `--max-seconds` (300) | `failed_timeout` | `4` |
| Unrecoverable error (e.g. LLM/network failure) | — | `failed_error` | `5` |

### Why an FSM, not a ReAct agent or actor-critic
The README documents four patterns considered before settling on the deterministic FSM (see "Architecture: four patterns considered, one chosen" in README.md). The chosen pattern puts all control flow and stop-condition logic in code; the LLM is called only as a tool for two narrow sub-tasks (generate a patch, summarize a failure) — never to judge its own success.

## Repo layout
```
loop_fixer/            # the package (see module map above)
tests/                 # unit tests + hermetic end-to-end demo test
tests/fixtures/        # scratch repo fixtures (excluded from normal pytest collection)
scripts/demo_run.py    # live demo using FakeLLMClient, no API key needed
docs/plans/            # planning docs from the design/build session
```
