from __future__ import annotations

import ast
from pathlib import Path

from .. import context
from ..test_runner import TestResult, run_pytest


def _target_to_path(repo_root: Path, target_test: str) -> Path:
    file_part = target_test.split("::", 1)[0]
    return (repo_root / file_part).resolve()


def _resolve_import_files(repo_root: Path, test_file: Path) -> set[str]:
    """Statically resolve local (same-repo) modules imported by the test file.

    This is the only mechanism used to populate writable_paths — the test
    file itself is never included, which is what makes it non-writable.
    """
    tree = ast.parse(test_file.read_text())
    module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                module_names.add(node.module.split(".")[0])

    writable: set[str] = set()
    for name in module_names:
        candidate = repo_root / f"{name}.py"
        if candidate.exists():
            writable.add(str(candidate.relative_to(repo_root)))
    return writable


def _last_meaningful_line(output: str) -> str:
    """Best-effort extraction of the final traceback/assertion line from pytest output."""
    lines = [l for l in output.splitlines() if l.strip()]
    if not lines:
        return ""
    # Prefer the final "E   ..." pytest assertion line if present.
    for line in reversed(lines):
        if line.strip().startswith("E "):
            return line.strip()
    return lines[-1].strip()


class PythonPytestAdapter:
    """Language adapter for Python/pytest — the original, still-default behavior."""

    name = "python"

    def resolve_test_file(self, repo_root: Path, target: str) -> Path:
        return _target_to_path(repo_root, target)

    def resolve_writable_paths(self, repo_root: Path, test_file: Path) -> set[str]:
        return _resolve_import_files(repo_root, test_file)

    def run_test(self, repo_root: Path, target: str, timeout: float) -> TestResult:
        return run_pytest(repo_root, target, timeout=timeout)

    def compute_signature(self, result: TestResult) -> str:
        """Deterministic failure signature: return code + normalized last error line.

        Used solely by the no-progress stop condition (fsm.py DECIDE state).
        """
        if result.timed_out:
            return "TIMEOUT"
        tail = _last_meaningful_line(result.stdout + "\n" + result.stderr)
        return f"{result.returncode}:{context.normalize_failure_text(tail)}"
