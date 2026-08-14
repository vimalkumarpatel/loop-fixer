from __future__ import annotations

import os
from typing import Protocol

from .errors import LLMError


class LLMClient(Protocol):
    def generate(self, prompt: str, *, max_tokens: int = 2048) -> str: ...


class LangChainAnthropicClient:
    """Concrete v1 LLM client, backed by LangChain's Anthropic chat model.

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
            from langchain_anthropic import ChatAnthropic
        except ImportError as exc:
            raise LLMError("the 'langchain-anthropic' package is not installed") from exc
        self._chat = ChatAnthropic(model=model, api_key=api_key)

    def generate(self, prompt: str, *, max_tokens: int = 2048) -> str:
        from langchain_core.messages import HumanMessage

        try:
            response = self._chat.invoke([HumanMessage(content=prompt)], max_tokens=max_tokens)
        except Exception as exc:  # network/API errors are not a stop-condition category
            raise LLMError(f"Anthropic API call failed: {exc}") from exc

        content = response.content
        if isinstance(content, list):
            # Some content blocks may be non-text (e.g. tool_use); keep only text.
            text_parts = [
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
                if not isinstance(block, dict) or block.get("type", "text") == "text"
            ]
            content = "\n".join(part for part in text_parts if part)
        if not content:
            raise LLMError("Anthropic response contained no text content")
        return content


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
