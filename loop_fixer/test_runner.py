from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TestResult:
    returncode: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run_pytest(repo_root: Path, target: str, timeout: float = 60.0) -> TestResult:
    """Run exactly one fixed command shape: `sys.executable -m pytest <target> ...`.

    This is the only subprocess call site reachable from the loop. The argv is
    built entirely from code-controlled values (repo_root, target, timeout) —
    no LLM output is ever concatenated into a command line.

    PYTHONDONTWRITEBYTECODE=1 is set so this never leaves a __pycache__/ behind
    in the target repo — some interpreters (e.g. Homebrew/python.org builds,
    unlike Apple's system Python which redirects bytecode caches elsewhere)
    would otherwise dirty the working tree and trip git_checkpoint.preflight()'s
    clean-tree check before the loop even starts.
    """
    start = time.monotonic()
    argv = [sys.executable, "-m", "pytest", target, "-x", "--tb=short", "-q"]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = subprocess.run(
            argv,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
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
