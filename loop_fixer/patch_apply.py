from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import PatchApplyError

_FILE_HEADER_RE = re.compile(r"^--- (?:a/)?(?P<old>\S+)")
_FILE_HEADER_PLUS_RE = re.compile(r"^\+\+\+ (?:b/)?(?P<new>\S+)")
_HUNK_HEADER_RE = re.compile(r"^@@ -(?P<old_start>\d+)(?:,(?P<old_len>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_len>\d+))? @@")


@dataclass
class Hunk:
    old_start: int
    old_lines: list[str] = field(default_factory=list)  # context + removed, as they appear in the original file
    new_lines: list[str] = field(default_factory=list)  # context + added, as they should appear in the new file


@dataclass
class FileDiff:
    path: str
    hunks: list[Hunk] = field(default_factory=list)


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


def parse_unified_diff(text: str) -> list[FileDiff]:
    """Parse a unified diff into per-file hunks. Stdlib-only, no `patch` binary."""
    text = _strip_markdown_fences(text)
    lines = text.splitlines()
    file_diffs: list[FileDiff] = []
    i = 0
    current: FileDiff | None = None
    while i < len(lines):
        line = lines[i]
        m_old = _FILE_HEADER_RE.match(line)
        if m_old:
            if i + 1 >= len(lines):
                raise PatchApplyError(f"malformed diff: '---' header with no matching '+++' at line {i}")
            m_new = _FILE_HEADER_PLUS_RE.match(lines[i + 1])
            if not m_new:
                raise PatchApplyError(f"malformed diff: expected '+++' header after '---' at line {i}")
            # Use the '+++' (new) path as the target; '---' is the pre-image path.
            path = m_new.group("new")
            if path == "/dev/null":
                path = m_old.group("old")
            current = FileDiff(path=path)
            file_diffs.append(current)
            i += 2
            continue
        m_hunk = _HUNK_HEADER_RE.match(line)
        if m_hunk:
            if current is None:
                raise PatchApplyError(f"malformed diff: hunk header before any file header at line {i}")
            hunk = Hunk(old_start=int(m_hunk.group("old_start")))
            i += 1
            while i < len(lines) and not lines[i].startswith("@@") and not _FILE_HEADER_RE.match(lines[i]):
                body_line = lines[i]
                if body_line.startswith("+"):
                    hunk.new_lines.append(body_line[1:])
                elif body_line.startswith("-"):
                    hunk.old_lines.append(body_line[1:])
                elif body_line.startswith(" "):
                    hunk.old_lines.append(body_line[1:])
                    hunk.new_lines.append(body_line[1:])
                elif body_line.startswith("\\"):
                    pass  # "\ No newline at end of file" marker — ignore
                elif body_line == "":
                    hunk.old_lines.append("")
                    hunk.new_lines.append("")
                else:
                    raise PatchApplyError(f"malformed diff: unexpected line in hunk body: {body_line!r}")
                i += 1
            current.hunks.append(hunk)
            continue
        i += 1
    if not file_diffs:
        raise PatchApplyError("malformed diff: no file headers found")
    return file_diffs


def _resolve_and_guard(repo_root: Path, rel_path: str, writable_paths: set[str]) -> Path:
    """Enforce: no path traversal outside repo_root, and only writable_paths may be touched.

    This is a hard, code-level gate — the LLM's diff cannot get a rejected
    path written to disk under any circumstance.
    """
    candidate = (repo_root / rel_path).resolve()
    try:
        candidate.relative_to(repo_root.resolve())
    except ValueError:
        raise PatchApplyError(f"rejected: path '{rel_path}' escapes repo_root")

    norm_rel = str(candidate.relative_to(repo_root.resolve()))
    if norm_rel not in writable_paths:
        raise PatchApplyError(
            f"rejected: '{norm_rel}' is not in the writable-paths allowlist "
            f"(protected: test files, conftest.py, pytest config)"
        )
    return candidate


def apply_unified_diff(
    repo_root: Path,
    diff_text: str,
    writable_paths: set[str],
    max_files: int = 3,
) -> list[str]:
    """Validate and apply a unified diff. Returns the list of files touched.

    All validation happens before any file is written — a rejected diff never
    partially applies.
    """
    file_diffs = parse_unified_diff(diff_text)

    if len(file_diffs) > max_files:
        raise PatchApplyError(
            f"rejected: diff touches {len(file_diffs)} files, exceeds max_files_per_patch={max_files}"
        )

    # Pass 1: validate every file's path and hunk context before writing anything.
    resolved: list[tuple[Path, FileDiff, list[str]]] = []
    for fd in file_diffs:
        target = _resolve_and_guard(repo_root, fd.path, writable_paths)
        if not target.exists():
            raise PatchApplyError(f"rejected: target file '{fd.path}' does not exist")
        original_lines = target.read_text().splitlines()
        new_lines = _apply_hunks(original_lines, fd.hunks, fd.path)
        resolved.append((target, fd, new_lines))

    # Pass 2: write. By construction every entry already passed validation.
    touched = []
    for target, fd, new_lines in resolved:
        content = "\n".join(new_lines)
        if content and not content.endswith("\n"):
            content += "\n"
        target.write_text(content)
        touched.append(fd.path)
    return touched


def _apply_hunks(original_lines: list[str], hunks: list[Hunk], path: str) -> list[str]:
    """Apply hunks by exact context match. Fails closed — no fuzzy matching."""
    result = list(original_lines)
    # Apply from bottom to top so earlier line-number offsets stay valid.
    for hunk in sorted(hunks, key=lambda h: h.old_start, reverse=True):
        start_idx = hunk.old_start - 1
        old_block = hunk.old_lines
        end_idx = start_idx + len(old_block)
        actual = result[start_idx:end_idx]
        if actual != old_block:
            raise PatchApplyError(
                f"rejected: hunk context in '{path}' does not exactly match current file "
                f"content at line {hunk.old_start} (context drift or stale diff)"
            )
        result[start_idx:end_idx] = hunk.new_lines
    return result
