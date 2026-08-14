from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..test_runner import TestResult


class LanguageAdapter(Protocol):
    """Every language-specific behavior the FSM needs, in one place.

    Implementations must preserve the same bounded-authority guarantees as
    the original pytest-only code did: `resolve_writable_paths` must never
    include the test file itself, and `run_test` must build its subprocess
    argv entirely from code-controlled values (repo_root, target, timeout) —
    never from LLM output.
    """

    name: str

    def resolve_test_file(self, repo_root: Path, target: str) -> Path: ...

    def resolve_writable_paths(self, repo_root: Path, test_file: Path) -> set[str]: ...

    def run_test(self, repo_root: Path, target: str, timeout: float) -> TestResult: ...

    def compute_signature(self, result: TestResult) -> str: ...
