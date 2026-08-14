from __future__ import annotations

import re

MAX_OUTPUT_CHARS = 4000

# Matches any absolute-looking file path with an extension (not just .py), so
# the same normalization helper works across languages/adapters (e.g. both
# `/repo/calc.py` and `/repo/src/main/java/com/example/Calc.java`).
_PATH_RE = re.compile(r"(?:/[\w.\-]+)+\.\w+")
_LINE_NO_RE = re.compile(r":\d+:")
_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")
_TMPDIR_RE = re.compile(r"/(tmp|var/folders)/[^\s:]+")


def truncate_output(text: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    """Shrink test-runner stdout/stderr so it stays cheap across accumulating prompts."""
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-max_chars // 2 :]
    return f"{head}\n...[truncated {len(text) - max_chars} chars]...\n{tail}"


def normalize_failure_text(text: str) -> str:
    """Strip volatile details (paths, line numbers, addresses) so semantically
    identical failures collapse to the same string across iterations.

    Shared by every LanguageAdapter's compute_signature — language-specific
    extraction of "the last meaningful line" lives in each adapter module
    (see adapters/python_pytest.py and adapters/java_maven.py).
    """
    text = _TMPDIR_RE.sub("<tmp>", text)
    text = _PATH_RE.sub("<path>", text)
    text = _LINE_NO_RE.sub(":<line>:", text)
    text = _ADDR_RE.sub("<addr>", text)
    return text.strip()
