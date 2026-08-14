from __future__ import annotations


class LoopFixerError(Exception):
    """Base class for all loop_fixer errors."""


class PatchApplyError(LoopFixerError):
    """Raised when an LLM-produced diff cannot be safely parsed or applied."""


class GitCheckpointError(LoopFixerError):
    """Raised when a git preflight/checkpoint/rollback operation fails."""


class LLMError(LoopFixerError):
    """Raised when the LLM client fails to produce a response."""


class AdapterError(LoopFixerError):
    """Raised when a language adapter's required tooling is missing or misconfigured."""
