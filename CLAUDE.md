# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`loop_fixer` is a deterministic FSM agent loop that takes a failing test and iterates **Plan → Edit → Test → Analyze → Decide** — generating a patch via an LLM, applying it, re-running the test, and deciding whether to continue, stop-success, or stop-fail — until the test passes or a bounded stop condition is hit. It's language-agnostic: a `LanguageAdapter` abstraction (`loop_fixer/adapters/`) supplies the language-specific mechanics, and `python` (pytest) or `java` (Maven) is selected via `--language` (default `python`).

The core design guarantee: **the agent cannot fake success.** The test file (and, for Python, fixtures/pytest config) is hard-excluded from the set of files a patch is allowed to touch (enforced in code, not by prompting), and every verification run is a fresh test-runner subprocess (`pytest` or `mvn test`, depending on the adapter) whose exit code is the only signal that matters.

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

For Java targets, add `--language java` and use a Maven Surefire spec as `--test` (`com.example.FooTest#testBar` — what `mvn -Dtest=...` already expects):
```bash
.venv/bin/python -m loop_fixer --language java --test com.example.FooTest#testBar --repo /path/to/target/repo
```
Requires `mvn` on `PATH`; the CLI raises `AdapterError` and exits before any LLM call if it's missing.

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

The same demo pattern exists for Java via `scripts/demo_run_java.py` and `tests/fixtures/broken_repo_java/` (requires `mvn` on `PATH`; `tests/test_e2e_demo_java.py` skips automatically when it's absent).

## Architecture

### Five graph nodes, one loop (`loop_fixer/fsm.py`, LangGraph-orchestrated)

Orchestration is a compiled LangGraph `StateGraph`, not a hand-rolled `while` loop:

```
PLAN --> EDIT --> TEST --> ANALYZE --> DECIDE
DECIDE -- "continue" --> EDIT
DECIDE -- "success or bound hit" --> END
```

State is a `GraphState` `TypedDict` of checkpoint-safe primitives only (`repo_root`, `target_test`, `language`, bounds, `iteration`, `attempts: list[dict]`, `status`, etc.) — no `LLMClient`, `LanguageAdapter`, or callback ever rides in state. Each node is `node(state, config) -> dict` and returns a **dict of updates**, which LangGraph merges into state (this is why nodes never mutate `state` in place, unlike the old dataclass version). The non-serializable runtime dependencies — the `LLMClient` instance, the `LanguageAdapter` instance, and the `on_event` observability callback — are passed via `config["configurable"]` at invoke time and read inside each node (`config["configurable"]["adapter"]`, etc.). This keeps the `InMemorySaver` checkpointer meaningful (nothing unpicklable in state) and is what lets `compiled.invoke(state, config=config)` drive the whole loop.

Each node does exactly one job, and responsibilities are strictly partitioned — this separation is what makes the anti-cheating guarantee possible:
- **plan_node** resolves which files are writable, statically, via `config["configurable"]["adapter"].resolve_test_file`/`resolve_writable_paths`.
- **edit_node** calls the LLM for a patch and applies it via `patch_apply.py`.
- **test_node** is the *only* node allowed to run the test runner, via `adapter.run_test`.
- **analyze_node** computes a deterministic failure signature, via `adapter.compute_signature`.
- **decide_node** is the *only* node allowed to check stop conditions or touch git (`git_checkpoint.py`) — language-neutral, untouched by the adapter abstraction. The conditional edge out of `decide` (`"continue"` → `edit`, `"terminate"` → `END`) is the only branch in the graph; everything else is a fixed edge.

`run_loop(initial_state: dict, *, llm_client, adapter, on_event=None, thread_id=None) -> dict` is the public entrypoint: it builds the `config["configurable"]` dict, calls `compiled.invoke(...)`, and returns the final state dict (read `result["status"]`, `result["iteration"]`, etc. — same shape callers already expected, just a dict instead of a `LoopState`). `build_initial_state(...)` is the convenience constructor for the initial `GraphState` dict, replacing the old `LoopState(...)` call sites.

An LLM/network failure in `edit_node` (`LLMError`) sets `status="failed_error"` without appending an attempt; `test_node`/`analyze_node`/`decide_node` all check for this and short-circuit as a no-op (matching the original hand-rolled FSM's behavior of ending the loop immediately, with no rollback and no iteration incremented, since the graph's edges are fixed rather than dynamically chosen per node like the old dispatch table was).

### Language adapters (`loop_fixer/adapters/`)
All language-specific mechanics live behind the `LanguageAdapter` Protocol (`adapters/base.py`): `resolve_test_file`, `resolve_writable_paths`, `run_test`, `compute_signature`. The concrete instance is passed to `run_loop(..., adapter=...)` and threaded through `config["configurable"]["adapter"]`; nodes call through it instead of hardcoding pytest.
- `python_pytest.py` — `PythonPytestAdapter`: pytest `file::test` node-id parsing, `ast`-based same-repo import resolution, `python -m pytest <target>`, `"E "`-prefix assertion-line extraction. This is the original behavior, relocated verbatim.
- `java_maven.py` — `JavaMavenAdapter`: Surefire spec parsing (`com.example.FooTest#testBar`), writable-path resolution via directory-convention mirroring (`FooTest`/`TestFoo` → `Foo` under `src/main/java/...`) unioned with regex-parsed `import` statements, `mvn -q -Dtest=<spec> -Dsurefire.failIfNoSpecifiedTests=false test` (raises `AdapterError` if `mvn` isn't on `PATH`), and Surefire-output exception-line extraction.
- Both adapters exclude the test file from `writable_paths` unconditionally — the same non-negotiable guarantee, enforced independently in each adapter.
- Adding a language = one new adapter module + a registry entry in `cli.py`'s `ADAPTERS` dict. No changes needed to `fsm.py`, `patch_apply.py`, or `git_checkpoint.py`.

### Module map (`loop_fixer/`)
- `fsm.py` — `GraphState` `TypedDict` + the five graph nodes + graph wiring (`StateGraph`/`InMemorySaver`) + `run_loop()`/`build_initial_state()`. The orchestration core, now a compiled LangGraph `StateGraph` instead of a hand-rolled dispatch loop.
- `patch_apply.py` — unified-diff parsing and application, plus every safety guard: rejects diff hunks touching paths outside `writable_paths` or containing path traversal, before a byte reaches disk. Language-neutral, unchanged by the adapter abstraction or the LangGraph migration.
- `test_runner.py` — `TestResult` dataclass (the shared, language-neutral return type) + subprocess wrapper around pytest, used by `PythonPytestAdapter.run_test`.
- `git_checkpoint.py` — preflight checks (clean tree required), branch/commit-per-attempt, rollback on failure. Language-neutral, unchanged.
- `llm_client.py` — `LLMClient` Protocol + `LangChainAnthropicClient` (real, wraps `langchain_anthropic.ChatAnthropic`) + `FakeLLMClient` (scripted, used by tests/demo — no network). The Protocol's `generate(prompt, *, max_tokens=2048) -> str` shape didn't change, so `FakeLLMClient` and every call site needed zero edits.
- `context.py` — `truncate_output`/`normalize_failure_text`, the shared helpers both adapters' `compute_signature` call. Language-specific "last meaningful line" extraction lives in each adapter module, not here.
- `cli.py` — argparse entrypoint; `--language {python,java}` selects the adapter; runs a baseline test pass before spending any LLM call (0 = already passing, anything else = proceed), then drives `run_loop()`.
- `adapters/` — see "Language adapters" above.
- `prompts/` — patch-generation and failure-summary prompt templates (`.txt`, packaged via `[tool.setuptools.package-data]`); language-neutral wording, shared by both adapters.

### Bounded authority model
What the loop can read/write/execute is an explicit allowlist, not a convention — see the README's "Bounded authority" table for the full enforcement mapping. Key points when touching this code:
- Writes are restricted to `writable_paths`, computed by the active adapter (static import graph for Python; convention + import resolution for Java) — never the test file, and for Python never `conftest.py`/pytest config either.
- The only subprocess call site for running tests is each adapter's `run_test` (`test_runner.run_pytest` for Python, a fixed `mvn` argv for Java), invoked with code-controlled args — LLM output is never placed in a command line, for either adapter.
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
loop_fixer/                       # the package (see module map above)
loop_fixer/adapters/              # LanguageAdapter Protocol + python_pytest/java_maven implementations
tests/                            # unit tests + hermetic/live end-to-end demo tests
tests/fixtures/                   # scratch repo fixtures (excluded from normal pytest collection)
tests/fixtures/broken_repo_java/  # Maven demo fixture (needs mvn on PATH for the e2e test)
scripts/demo_run.py               # live Python demo using FakeLLMClient, no API key needed
scripts/demo_run_java.py          # live Java demo using FakeLLMClient, requires mvn on PATH
docs/plans/                       # planning docs from the design/build session
```
