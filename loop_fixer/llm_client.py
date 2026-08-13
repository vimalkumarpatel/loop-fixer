from __future__ import annotations

import os
from typing import Protocol

from .errors import LLMError


class LLMClient(Protocol):
    def generate(self, prompt: str, *, max_tokens: int = 2048) -> str: ...


class AnthropicLLMClient:
    """Concrete v1 LLM client, backed by the Anthropic SDK.

    Reads ANTHROPIC_API_KEY from the environment. The rest of loop_fixer only
    depends on the LLMClient Protocol above, so this class is swappable for
    any other provider by implementing the same single method.
    """

    def __init__(self, model: str = "claude-sonnet-4-5"):
        self.model = model
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set in the environment")
        try:
            import anthropic
        except ImportError as exc:
            raise LLMError("the 'anthropic' package is not installed") from exc
        self._client = anthropic.Anthropic(api_key=api_key)

    def generate(self, prompt: str, *, max_tokens: int = 2048) -> str:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # network/API errors are not a stop-condition category
            raise LLMError(f"Anthropic API call failed: {exc}") from exc

        parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
        if not parts:
            raise LLMError("Anthropic response contained no text content")
        return "\n".join(parts)


class FakeLLMClient:
    """Scripted client for hermetic tests — returns canned responses in order."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[str] = []

    def generate(self, prompt: str, *, max_tokens: int = 2048) -> str:
        self.calls.append(prompt)
        if not self._responses:
            raise LLMError("FakeLLMClient exhausted its scripted responses")
        return self._responses.pop(0)
