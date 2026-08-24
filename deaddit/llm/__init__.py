"""Consolidated OpenAI-compatible LLM client."""

from deaddit.llm.client import (
    STOP_VALUES,
    ChatRequest,
    ChatResult,
    LLMClient,
    Sampling,
)
from deaddit.llm.errors import LLMError, PermanentLLMError, TransientLLMError
from deaddit.llm.provider import get_provider, reset_provider, set_provider

__all__ = [
    "STOP_VALUES",
    "ChatRequest",
    "ChatResult",
    "LLMClient",
    "LLMError",
    "PermanentLLMError",
    "Sampling",
    "TransientLLMError",
    "get_provider",
    "reset_provider",
    "set_provider",
]
