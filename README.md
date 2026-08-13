# loop_fixer

A goal-based agent loop that takes a failing pytest test and iterates
**Act → Verify → Decide** — generating a patch, applying it, re-running the
test, and deciding whether to continue, stop-success, or stop-fail — until
the test passes or a bounded stop condition is hit.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
export ANTHROPIC_API_KEY=sk-...
.venv/bin/python -m loop_fixer --test path/to/test_file.py::test_name --repo /path/to/target/repo
```

The target repo must be a git repository with a clean working tree (the tool
refuses to start otherwise). It creates a dedicated `loop-fixer/...` branch,
prints one line per iteration as it happens, and leaves that branch checked
out with the result — it never touches your original branch.

## Verify it manually (no API key needed)

`scripts/demo_run.py` runs the real production pipeline — the same
`fsm.py`/`git_checkpoint.py`/`patch_apply.py`/`test_runner.py` code the CLI
uses — with a scripted `FakeLLMClient` standing in for the network call, so
you can see the whole loop converge without `ANTHROPIC_API_KEY` set.

```bash
# 1. Seed a fresh scratch repo from the intentionally-broken fixture
rm -rf /tmp/demo
cp -r tests/fixtures/broken_repo /tmp/demo
cd /tmp/demo && git init -q && git config user.email demo@example.com \
  && git config user.name Demo && git add -A && git commit -q -m init
cd /Users/vimalkumarpatel/git/test

# 2. Run the demo loop against it
.venv/bin/python scripts/demo_run.py /tmp/demo
```

Expected output ends with `[result] status=success iterations=1 ...`, preceded
by one line per FSM state transition (PLAN/EDIT/TEST/ANALYZE/DECIDE).

Then inspect what it actually did:

```bash
cd /tmp/demo
cat calc.py                 # should show `return a + b` (fixed)
cat test_calc.py            # byte-identical to the original — never touched
git log --oneline           # "init" + "loop_fixer attempt 1: passed"
/Users/vimalkumarpatel/git/test/.venv/bin/python -m pytest test_calc.py -q   # 1 passed
cd /Users/vimalkumarpatel/git/test
```

To run the real CLI end-to-end against the same scratch repo with an actual
model instead of the fake client:

```bash
export ANTHROPIC_API_KEY=sk-...
.venv/bin/python -m loop_fixer --test test_calc.py::test_add --repo /tmp/demo
```

Run the automated test suite (unit tests + hermetic end-to-end demo, no
network calls — this is what CI/a reviewer would run):

```bash
.venv/bin/python -m pytest tests/ -v
```

## Design

The loop is a deterministic finite-state machine — `PLAN → EDIT → TEST →
ANALYZE → DECIDE`, looping DECIDE→EDIT or terminating — where plain Python
owns all control flow and the LLM is invoked only for two narrow sub-tasks
(generate a patch, optionally summarize a failure). **The verification
signal is pytest's exit code from a freshly spawned subprocess, checked in
exactly one place**; no LLM is ever asked whether its own fix worked. That
signal can't be faked because the test file (and `conftest.py`/pytest
config) is never on the loop's writable-paths allowlist — `patch_apply.py`
rejects any diff hunk targeting a protected path in code, before a byte
touches disk, so the only way to make the test pass is to fix the actual
source under test. **Stop conditions**, all enforced in DECIDE: success,
an iteration cap (`--max-iters`, default 5), a wall-clock budget
(`--max-seconds`, default 300), and a no-progress detector that halts after
the same normalized failure signature repeats `--no-progress-window`
(default 3) times in a row — so an unfixable bug can't loop forever.

**What the loop may touch**: only the source file(s) statically imported by
the target test, via patches applied to a dedicated git branch, and exactly
one subprocess shape (`python -m pytest <target>`) to verify. **Deliberately
out of reach**: the test file itself, pytest/conftest config, anything
outside the repo root, and any command execution beyond that fixed pytest
invocation — every failure path (`git reset --hard` back to the pre-loop
commit) leaves the working tree exactly as it started, never half-edited.
