from __future__ import annotations

import re

from .test_runner import TestResult

MAX_OUTPUT_CHARS = 4000

_PATH_RE = re.compile(r"(?:/[\w.\-]+)+\.py")
_LINE_NO_RE = re.compile(r":\d+:")
_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")
_TMPDIR_RE = re.compile(r"/(tmp|var/folders)/[^\s:]+")


def truncate_output(text: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    """Shrink pytest stdout/stderr so it stays cheap across accumulating prompts."""
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-max_chars // 2 :]
    return f"{head}\n...[truncated {len(text) - max_chars} chars]...\n{tail}"


def normalize_failure_text(text: str) -> str:
    """Strip volatile details (paths, line numbers, addresses) so semantically
    identical failures collapse to the same string across iterations."""
    text = _TMPDIR_RE.sub("<tmp>", text)
    text = _PATH_RE.sub("<path>", text)
    text = _LINE_NO_RE.sub(":<line>:", text)
    text = _ADDR_RE.sub("<addr>", text)
    return text.strip()


def last_meaningful_line(output: str) -> str:
    """Best-effort extraction of the final traceback/assertion line from pytest output."""
    lines = [l for l in output.splitlines() if l.strip()]
    if not lines:
        return ""
    # Prefer the final "E   ..." pytest assertion line if present.
    for line in reversed(lines):
        if line.strip().startswith("E "):
            return line.strip()
    return lines[-1].strip()


def compute_signature(result: TestResult) -> str:
    """Deterministic failure signature: return code + normalized last error line.

    Used solely by the no-progress stop condition (fsm.py DECIDE state).
    """
    if result.timed_out:
        return "TIMEOUT"
    tail = last_meaningful_line(result.stdout + "\n" + result.stderr)
    return f"{result.returncode}:{normalize_failure_text(tail)}"
