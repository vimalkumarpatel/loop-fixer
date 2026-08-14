"""MCP server front door for loop_fixer.

Exposes a single tool, `fix_test`, that mirrors the `loop-fixer` CLI's
argument surface and drives the same preflight -> git_checkpoint.preflight()
-> LangChainAnthropicClient -> run_loop() sequence as `cli.main()`. The value
this adds over the CLI is MCP-native tool discovery plus live PLAN/EDIT/TEST/
ANALYZE/DECIDE progress via `ctx.info(...)` notifications, streamed as
`run_loop()` runs — not a change in who pays for the model: this still uses
loop_fixer's own ANTHROPIC_API_KEY, same as the CLI (MCP client-side sampling
is not yet supported by Claude Code, the primary target harness, so
delegating generation to the caller's own model isn't viable yet).

Nothing in this module may write to stdout: the stdio transport uses stdout
for the JSON-RPC stream, so all progress must go through `ctx.info(...)`.
"""

from __future__ import annotations

from pathlib import Path

import anyio
from mcp.server.fastmcp import Context, FastMCP

from . import git_checkpoint
from .cli import ADAPTERS
from .errors import AdapterError, GitCheckpointError, LLMError
from .fsm import build_initial_state, run_loop
from .llm_client import LangChainAnthropicClient

mcp = FastMCP("loop-fixer")


def _make_on_event(ctx: Context, portal: anyio.from_thread.BlockingPortal):
    def on_event(line: str) -> None:
        portal.call(ctx.info, line)

    return on_event


async def _run_fix_test(
    ctx: Context,
    *,
    test: str,
    repo: str = ".",
    language: str = "python",
    max_iters: int = 5,
    max_seconds: float = 300.0,
    no_progress_window: int = 3,
    max_files_per_patch: int = 3,
    test_timeout: float = 60.0,
    summarize_failures: bool = False,
    model: str = "claude-sonnet-4-5",
) -> str:
    """Testable body behind the `fix_test` tool. Mirrors `cli.main()` step for step."""
    repo_root = Path(repo).resolve()
    adapter = ADAPTERS[language]()

    await ctx.info(f"[preflight] baseline run: {adapter.name} {test}")
    try:
        baseline_result = await anyio.to_thread.run_sync(
            lambda: adapter.run_test(repo_root, test, timeout=test_timeout)
        )
    except AdapterError as exc:
        return f"[preflight] {exc}"
    if baseline_result.returncode == 0:
        return "[preflight] target test already passes — nothing to fix"

    try:
        branch, baseline_sha = git_checkpoint.preflight(repo_root, test)
    except GitCheckpointError as exc:
        return f"[preflight] {exc}"
    await ctx.info(f"[preflight] branch={branch} baseline={baseline_sha[:8]}")

    try:
        llm_client = LangChainAnthropicClient(model=model)
    except LLMError as exc:
        return f"[preflight] {exc}"

    initial_state = build_initial_state(
        repo_root=str(repo_root),
        target_test=test,
        language=language,
        max_iterations=max_iters,
        max_wall_seconds=max_seconds,
        no_progress_window=no_progress_window,
        max_files_per_patch=max_files_per_patch,
        test_timeout=test_timeout,
        summarize_failures=summarize_failures,
        baseline_commit=baseline_sha,
        last_known_good_commit=baseline_sha,
    )

    async with anyio.from_thread.BlockingPortal() as portal:
        on_event = _make_on_event(ctx, portal)
        result = await anyio.to_thread.run_sync(
            lambda: run_loop(initial_state, llm_client=llm_client, adapter=adapter, on_event=on_event)
        )

    summary = f"[result] status={result['status']} iterations={result['iteration']} branch={branch}"
    if result["status"] == "success":
        summary += f"\n[result] fix committed on '{branch}'. Review with: git log {branch}"
    else:
        summary += f"\n[result] rolled back to baseline; attempt history preserved on '{branch}'"
    return summary


@mcp.tool()
async def fix_test(
    ctx: Context,
    test: str,
    repo: str = ".",
    language: str = "python",
    max_iters: int = 5,
    max_seconds: float = 300.0,
    no_progress_window: int = 3,
    max_files_per_patch: int = 3,
    test_timeout: float = 60.0,
    summarize_failures: bool = False,
    model: str = "claude-sonnet-4-5",
) -> str:
    """Fix a failing test by iteratively generating and applying patches.

    Mirrors the loop-fixer CLI's argument surface: `test` is a pytest node-id
    (tests/test_foo.py::test_bar) for language "python", or a Maven Surefire
    spec (com.example.FooTest#testBar) for language "java". Streams
    PLAN/EDIT/TEST/ANALYZE/DECIDE progress as MCP log notifications while it
    runs. Requires ANTHROPIC_API_KEY to be set in the server's environment.
    """
    return await _run_fix_test(
        ctx,
        test=test,
        repo=repo,
        language=language,
        max_iters=max_iters,
        max_seconds=max_seconds,
        no_progress_window=no_progress_window,
        max_files_per_patch=max_files_per_patch,
        test_timeout=test_timeout,
        summarize_failures=summarize_failures,
        model=model,
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
