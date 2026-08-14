# `loop_fixer` — Migrate orchestration to LangGraph

## Context

`loop_fixer`'s orchestration today (`loop_fixer/fsm.py`) is a hand-rolled FSM: a `LoopState` dataclass threaded through `state_plan`/`state_edit`/`state_test`/`state_analyze`/`state_decide` functions, each mutating state and returning the next state's name, driven by a `while` loop in `run_loop()`. The user wants to migrate this orchestration layer to [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) — reviewed LangGraph's `StateGraph`/node/conditional-edge API and its checkpointer/persistence model as background for this plan.

This is a framework swap for the *orchestration* layer only. The properties that make `loop_fixer` trustworthy — the test file is unfakeable (protected-path allowlist in `patch_apply.py`), verification is a fresh subprocess exit code (`test_runner.py`/adapters), every attempt is git-checkpointed with rollback on failure (`git_checkpoint.py`) — live in modules that don't know or care what drives them, and this migration doesn't touch them. Only `fsm.py` (rewritten around `StateGraph`), `llm_client.py` (swapped to LangChain's model abstraction, per the user's choice), and `cli.py`'s call site change.

**Sequencing**: there's a background agent mid-implementation on a separate branch (`worktree-agent-a04f2cd174df42da9`) adding Java/Maven support, which also restructures `fsm.py`/`cli.py` substantially (introducing the `LanguageAdapter` abstraction). Per the user's explicit preference, **this migration should not start until that branch lands on `main`** — doing both large `fsm.py` rewrites concurrently would produce a painful merge. This document is the design, ready to execute once that merge happens.

## Design

### State schema

Replace the `LoopState`/`Attempt` dataclasses with a `GraphState` `TypedDict` holding only checkpoint-safe primitive data: `repo_root: str`, `target_test: str`, `max_iterations`, `max_wall_seconds`, `no_progress_window`, `max_files_per_patch`, `test_timeout`, `summarize_failures`, `started_at`, `iteration`, `attempts: list[dict]` (each attempt's fields as plain dicts, not dataclass instances), `baseline_commit`, `last_known_good_commit`, `status`, `test_file: str`, `writable_paths: list[str]`, `last_summary`.

Non-serializable runtime dependencies — the `llm_client` (`LLMClient` Protocol instance), the `adapter` (`LanguageAdapter` instance, from the merged Java-adapter work), and the `on_event` observability callback — are **not** part of `GraphState`. They're passed via `config["configurable"]` at invoke time and read inside each node via a `config: RunnableConfig` parameter, following LangGraph's idiomatic split between checkpointed state and runtime context. This keeps the `InMemorySaver` checkpointer meaningful (nothing unpicklable riding in state) and avoids rework if a persistent checkpointer (Sqlite/Postgres) is added later.

### Nodes (loop_fixer/fsm.py)

`plan_node(state, config)`, `edit_node(state, config)`, `test_node(state, config)`, `analyze_node(state, config)`, `decide_node(state, config)` — same responsibilities as today's `state_plan`/`state_edit`/`state_test`/`state_analyze`/`state_decide`, restated LangGraph-style: read `config["configurable"]["llm_client"]` / `["adapter"]` / `["on_event"]`, return a **dict of state updates** (LangGraph merges these into state) instead of mutating in place and returning a next-node-name string.

All existing logic is *relocated*, not redesigned: EDIT's `PatchApplyError`/`LLMError` handling and the synthetic `PATCH_APPLY_ERROR` attempt, TEST's "only this node runs the test, always a fresh subprocess" rule, DECIDE's exact stop-condition order (success → iteration cap → wall-clock → no-progress) and git checkpoint/rollback calls, including the literal `Reached Maximum Allowed Loops` stderr message on `failed_max_iter` — all preserved byte-for-byte in behavior.

### Graph wiring

```python
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver

graph = StateGraph(GraphState)
graph.add_node("plan", plan_node)
graph.add_node("edit", edit_node)
graph.add_node("test", test_node)
graph.add_node("analyze", analyze_node)
graph.add_node("decide", decide_node)
graph.add_edge(START, "plan")
graph.add_edge("plan", "edit")
graph.add_edge("edit", "test")
graph.add_edge("test", "analyze")
graph.add_edge("analyze", "decide")
graph.add_conditional_edges(
    "decide",
    lambda state: "continue" if state["status"] == "running" else "terminate",
    {"continue": "edit", "terminate": END},
)
compiled = graph.compile(checkpointer=InMemorySaver())
```

### Public entrypoint (preserving `cli.py` compatibility)

```python
def run_loop(initial_state: dict, *, llm_client, adapter, on_event=None, thread_id=None) -> dict:
    config = {"configurable": {"llm_client": llm_client, "adapter": adapter, "on_event": on_event},
              "thread_id": thread_id or str(uuid4())}
    return compiled.invoke(initial_state, config=config)
```
`cli.py`'s call site changes minimally — same inputs (repo, target, bounds, llm_client, adapter), same shape of output (a dict it already reads `status`/`iteration`/etc. off of).

### LLM client (loop_fixer/llm_client.py)

Add `langchain-anthropic` + `langchain-core` to `pyproject.toml`; drop the direct `anthropic` SDK dependency (transitive via `langchain-anthropic`). Replace `AnthropicLLMClient` with `LangChainAnthropicClient`, wrapping `ChatAnthropic(model=..., api_key=...)` and implementing the *same* `generate(prompt: str, *, max_tokens=2048) -> str` method via `.invoke([HumanMessage(content=prompt)]).content`. This keeps the `LLMClient` Protocol and the `FakeLLMClient` test double completely unchanged — a drop-in swap of the real implementation, not a rewrite of every call site or prompt template.

### Tests

- `tests/test_fsm.py`: update to call node functions directly with hand-built `state` dicts + a minimal `config` (same "no I/O needed to test transition logic" property as today), and/or `compiled.invoke(...)` for full-loop tests.
- `tests/test_e2e_demo.py` / the Java equivalent from the merged branch: call-site updates only (new `run_loop()` signature, dict-based state) — assertions on outcomes (convergence, rollback, protected-test-file rejection) don't change, since they test behavior, not mechanism.
- `tests/test_cli.py`: call-site update only; the `Reached Maximum Allowed Loops` / exit-code-2 assertion must still pass unchanged.

## Compatibility with the in-flight Java/Maven adapter work

The Java-adapter branch introduces the `LanguageAdapter` Protocol (`base.py`) with exactly four methods — `resolve_test_file`, `resolve_writable_paths`, `run_test`, `compute_signature` — plus `PythonPytestAdapter` and `JavaMavenAdapter` implementations, an `AdapterError`, a `--language {python,java}` CLI flag, and the `tests/fixtures/broken_repo_java/` fixture with `test_adapters_java.py`/`test_e2e_demo_java.py`. None of that surface changes under this migration:

- **The adapter classes themselves (`python_pytest.py`, `java_maven.py`) require zero edits.** They're plain classes implementing a Protocol — LangGraph doesn't know or care how they're constructed or what language they target.
- **Only the *call site* moves**: today (post-Java-merge) a node would call `state.adapter.resolve_test_file(...)`; under this migration the identical call becomes `config["configurable"]["adapter"].resolve_test_file(...)` inside `plan_node`. Same method, same arguments, same return type — `plan_node` still calls `resolve_test_file`+`resolve_writable_paths`, `test_node` still calls `run_test`, `analyze_node` still calls `compute_signature`, in the same order as today's `state_plan`/`state_test`/`state_analyze`.
- **`cli.py`'s `--language` flag and adapter construction (`PythonPytestAdapter()` / `JavaMavenAdapter()`) are unaffected** — that code already just builds an adapter instance and hands it to the orchestration entrypoint; only the entrypoint's name/signature changes (`LoopState(..., adapter=...)` → `run_loop(..., adapter=..., ...)`), which is exactly the "call-site update only" already scoped under `cli.py` below.
- **`tests/test_adapters_java.py` needs no changes at all** (it tests `JavaMavenAdapter` in isolation, never touches `fsm.py`). **`tests/test_e2e_demo_java.py`** gets the same mechanical call-site update as `test_e2e_demo.py` — its assertions (Java convergence, test-file-protection, rollback) stay identical.
- This is exactly why sequencing matters (see above): the Java-adapter branch should land and stabilize the `LanguageAdapter` contract on `main` first, so this migration has a fixed, tested interface to relocate calls to — rather than both changes touching `fsm.py`'s adapter-calling logic at the same time.

## Critical files
- `loop_fixer/fsm.py` — full rewrite: `GraphState`, node functions, graph wiring, `run_loop()`
- `loop_fixer/llm_client.py` — `AnthropicLLMClient` → `LangChainAnthropicClient`
- `loop_fixer/cli.py` — call-site update to the new `run_loop()` signature
- `pyproject.toml` — dependency swap (`langgraph`, `langchain-anthropic`, `langchain-core` in; direct `anthropic` out)
- `tests/test_fsm.py`, `tests/test_e2e_demo.py`, `tests/test_cli.py` — call-site updates

## Verification (once executed, post Java-branch merge)

1. `pytest tests/ -v` — full suite green, including the Java adapter tests from the merged branch.
2. Live demo run (`scripts/demo_run.py`, updated to the new `run_loop()` signature) against the Python fixture — confirm identical convergence behavior/trace shape to today's `[iter N] STATE ...` output.
3. Re-run the "LLM tries to edit the test file" and "no-progress → rollback" scenarios and confirm identical outcomes to the pre-migration behavior documented in `README.md` — the unfakeable-signal and bounded-authority guarantees must be provably unchanged.
4. Confirm `--max-iters` still exits `2` with `Reached Maximum Allowed Loops` on stderr.
