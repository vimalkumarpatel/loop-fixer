# Upgrade loop_fixer to target Python 3.13

## Context

The project currently declares `requires-python = ">=3.9"` and the dev `.venv` was built against Apple's bundled Python 3.9.6. When the MCP server work (previous session) needed the `mcp` SDK, which requires Python ≥3.10, a second venv (`.venv-mcp`, Python 3.11) had to be created just for that extra, and both `README.md`/`CLAUDE.md` now carry a "two venvs" caveat. The user wants to move the whole project onto a current Python version so this split goes away and the project stops trailing the ecosystem by several major versions.

I verified directly (real installs + full test suite, not just research) that both Python 3.13 and 3.14 work today with the project's **existing pinned dependency floors unchanged** (`langgraph>=0.2` resolving to the same `0.6.11` currently used, `langchain-core>=0.3`→`0.3.86`, `langchain-anthropic>=0.3`→`0.3.22`, `mcp>=1.6,<2.0`→`1.29.0`): 37/37 tests pass on each. Decision (confirmed): **target Python 3.13** — one release behind bleeding-edge 3.14, with broader ecosystem/tooling maturity, and no behavior difference observed between the two in this project's own suite.

Important nuance found during verification: `pyproject.toml`'s dependencies have no upper bounds, so a naive `pip install` on a newer Python resolves to whatever the latest compatible major is — for langgraph/langchain-core that turned out to be `1.2.11`/`1.5.4` (a major-version jump from what's in use today), which is **out of scope for this task**. This plan only moves the Python floor; it does not upgrade langgraph/langchain-core/langchain-anthropic to their new majors (that's a separate, larger undertaking involving verifying `fsm.py`'s `StateGraph`/`RunnableConfig`/`InMemorySaver` usage against a new API surface). To keep today's proven dependency versions after the floor bump, `pyproject.toml` needs explicit upper-bound pins it doesn't have today.

Testing also surfaced a real latent bug, confirmed reproducible and now in scope to fix: `git_checkpoint.preflight()`'s clean-tree check trips on a `__pycache__/` directory left behind by the baseline pytest run (`test_runner.run_pytest`) — but only on Homebrew/python.org Pythons, because Apple's system Python 3.9 happens to redirect bytecode caches elsewhere via `sys.pycache_prefix`. Since this upgrade moves every user off Apple's Python, this would otherwise start breaking `loop_fixer` for real users on their first run against any freshly-cloned target repo.

## Changes

### 1. `pyproject.toml` — bump the floor, pin the ceiling
- `requires-python = ">=3.9"` → `">=3.13"`.
- Add explicit upper bounds to `dependencies` so a fresh install on 3.13 reproduces exactly what's proven to work, not whatever the resolver picks: `"langgraph>=0.2,<0.7"`, `"langchain-anthropic>=0.3,<0.4"`, `"langchain-core>=0.3,<0.4"` (i.e., cap each dependency to stay within the major/minor line already verified — `0.6.11`/`0.3.86`/`0.3.22`). `mcp`'s extra already has an explicit ceiling (`<2.0`); leave it as-is, since Python ≥3.13 no longer needs a separate floor caveat for it.
- `pytest>=7.0` stays unbounded (no evidence of a compatibility issue; pytest 9.x was what resolved cleanly in both test runs).

### 2. `loop_fixer/test_runner.py` — fix the `__pycache__` dirty-tree bug
In `run_pytest`, pass `env` to `subprocess.run` with `PYTHONDONTWRITEBYTECODE=1` set (merged on top of the inherited environment via `os.environ | {"PYTHONDONTWRITEBYTECODE": "1"}`), so the baseline/verification pytest subprocess never leaves a `__pycache__/` directory in the target repo regardless of host interpreter. This is the only adapter that needs it — `JavaMavenAdapter.run_test` shells out to `mvn` directly (no Python interpreter involved in the target repo), and its `target/` build-output equivalent is already handled via the Maven fixture's own `.gitignore` (documented in README's "Verified live" section).

### 3. Collapse `.venv-mcp` back into a single `.venv`
- Remove the `.venv-mcp` directory (it was only needed because the base package's floor was below the `mcp` extra's Python requirement; that gap no longer exists once the floor is 3.13).
- Remove `.venv-mcp/` from `.gitignore` (added in the prior MCP-server commit).
- Rebuild `.venv` against Python 3.13: `python3.13 -m venv .venv && .venv/bin/pip install -e ".[mcp]"` — everyone, including the `mcp` extra and its tests, now lives in one venv.

### 4. Documentation — remove the dual-venv caveats
- **`README.md`**: delete the "Python version note" paragraph (currently right after the MCP server install snippet) that explains the `.venv-mcp` workaround; update the `pip install -e ".[mcp]"` snippet's comment (currently `# requires Python 3.10+ — see note below`) since that's no longer a special case — just note Python 3.13+ is required for the whole package now, stated once at the top-level `## Run it` section instead of only for the MCP path.
- **`CLAUDE.md`**: delete the entire "## Two local venvs" section; update the "Run the MCP server" bullet's comment (`# requires Python >=3.10, unlike the rest of the package`) since it's no longer a special case for that path specifically — the whole package now requires it. Update the "What this is"/Setup section if it references 3.9 anywhere.
- Both docs' top-level `Setup`/`Run it` sections should state the new floor once (`python3 -m venv .venv && .venv/bin/pip install -e .` assuming `python3` resolves to 3.13+, or `python3.13 -m venv .venv ...` to be explicit).

### 5. `tests/test_mcp_server.py`
No code change needed — `pytest.importorskip("mcp")` continues to work correctly; it's just that now the base `.venv` always has `mcp` importable too (once installed with the `[mcp]` extra), so the skip becomes purely about whether that extra was installed, not about Python version.

## Out of scope (explicitly, per the confirmed decision above)
- Upgrading `langgraph`, `langchain-core`, or `langchain-anthropic` to their new major versions (`1.x`) — that needs its own compatibility pass against `fsm.py`'s LangGraph usage and is a separate task.
- Any change to `loop_fixer/adapters/java_maven.py` or the Maven/Java path — unaffected by this Python-version change.

## Verification
1. Remove `.venv-mcp`; create a fresh `.venv` with Python 3.13 (`python3.13 -m venv .venv && .venv/bin/pip install -e ".[mcp]"`).
2. `.venv/bin/python -m pytest tests/ -v` — expect all tests (including `test_mcp_server.py`, no longer skipped) to pass, 37/37, matching what was already confirmed manually with the pinned dependency versions on 3.13.
3. Confirm the `__pycache__` fix directly: run the CLI (or `scripts/demo_run.py`) against a fresh copy of `tests/fixtures/broken_repo` and confirm no `__pycache__/` is left behind after the baseline test run — `git status --porcelain` should be empty immediately before `git_checkpoint.preflight()`'s clean-tree check runs, without needing `PYTHONDONTWRITEBYTECODE=1` set externally.
4. Spot-check `pip show langgraph langchain-core langchain-anthropic` in the rebuilt `.venv` to confirm the pinned versions (`0.6.11`/`0.3.86`/`0.3.22`) were installed, not newer majors, proving the new upper bounds in `pyproject.toml` are doing their job.
