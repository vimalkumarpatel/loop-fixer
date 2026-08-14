from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

from .. import context
from ..errors import AdapterError
from ..test_runner import TestResult

_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;")
_EXCEPTION_RE = re.compile(r"^(?:Caused by:\s*)?[\w.$]+(?:Exception|Error|Failure)\b.*")
_TESTS_RUN_RE = re.compile(r"^Tests run:")


def _target_to_class(target: str) -> str:
    """`com.example.FooTest#testBar` -> `com.example.FooTest` (method part optional)."""
    return target.split("#", 1)[0]


def _class_to_test_path(class_name: str) -> Path:
    return Path("src") / "test" / "java" / Path(*class_name.split(".")) .with_suffix(".java")


def _class_to_main_path(class_name: str) -> Path:
    return Path("src") / "main" / "java" / Path(*class_name.split(".")).with_suffix(".java")


def _strip_test_affix(simple_name: str) -> str:
    """Strip a leading or trailing `Test` from a class's simple name."""
    if simple_name.endswith("Test") and len(simple_name) > len("Test"):
        return simple_name[: -len("Test")]
    if simple_name.endswith("Tests") and len(simple_name) > len("Tests"):
        return simple_name[: -len("Tests")]
    if simple_name.startswith("Test") and len(simple_name) > len("Test"):
        return simple_name[len("Test") :]
    return simple_name


def _last_meaningful_line(output: str) -> str:
    """Best-effort extraction of the final exception/assertion/summary line
    from Maven Surefire output."""
    lines = [l for l in output.splitlines() if l.strip()]
    if not lines:
        return ""
    for line in reversed(lines):
        if _EXCEPTION_RE.match(line.strip()):
            return line.strip()
    for line in reversed(lines):
        if _TESTS_RUN_RE.match(line.strip()):
            return line.strip()
    return lines[-1].strip()


class JavaMavenAdapter:
    """Language adapter for Java/Maven, driven via Surefire's `-Dtest=` target syntax."""

    name = "java"

    def resolve_test_file(self, repo_root: Path, target: str) -> Path:
        class_name = _target_to_class(target)
        return (repo_root / _class_to_test_path(class_name)).resolve()

    def resolve_writable_paths(self, repo_root: Path, test_file: Path) -> set[str]:
        writable: set[str] = set()

        # (a) directory-convention mirroring: FooTest -> Foo, TestFoo -> Foo, etc.
        try:
            rel = test_file.resolve().relative_to((repo_root / "src" / "test" / "java").resolve())
        except ValueError:
            rel = None
        if rel is not None:
            parts = list(rel.parts)
            simple_name = Path(parts[-1]).stem
            target_simple = _strip_test_affix(simple_name)
            candidate_parts = parts[:-1] + [f"{target_simple}.java"]
            candidate = repo_root / "src" / "main" / "java" / Path(*candidate_parts)
            if candidate.exists():
                writable.add(str(candidate.relative_to(repo_root)))

        # (b) regex-parse the test file's imports for same-repo helper classes.
        if test_file.exists():
            for line in test_file.read_text().splitlines():
                m = _IMPORT_RE.match(line)
                if not m:
                    continue
                imported = m.group(1)
                candidate = repo_root / _class_to_main_path(imported)
                if candidate.exists():
                    writable.add(str(candidate.relative_to(repo_root)))

        return writable

    def run_test(self, repo_root: Path, target: str, timeout: float) -> TestResult:
        """Run exactly one fixed command shape: `mvn -q -Dtest=<spec> ... test`.

        The argv is built entirely from code-controlled values (repo_root,
        target, timeout) — no LLM output is ever concatenated into a command
        line. Mirrors test_runner.run_pytest's subprocess pattern.
        """
        if shutil.which("mvn") is None:
            raise AdapterError(
                "the 'mvn' executable was not found on PATH — install Maven to use --language java"
            )

        start = time.monotonic()
        argv = [
            "mvn",
            "-q",
            f"-Dtest={target}",
            "-Dsurefire.failIfNoSpecifiedTests=false",
            "test",
        ]
        try:
            proc = subprocess.run(
                argv,
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            duration = time.monotonic() - start
            return TestResult(
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                duration=duration,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start
            return TestResult(
                returncode=-1,
                stdout=exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                stderr=exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
                duration=duration,
                timed_out=True,
            )

    def compute_signature(self, result: TestResult) -> str:
        """Deterministic failure signature: return code + normalized last
        exception/summary line. Used solely by the no-progress stop condition."""
        if result.timed_out:
            return "TIMEOUT"
        tail = _last_meaningful_line(result.stdout + "\n" + result.stderr)
        return f"{result.returncode}:{context.normalize_failure_text(tail)}"
