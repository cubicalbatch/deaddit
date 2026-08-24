"""Typed errors for the LLM client."""


class LLMError(Exception):
    """Base class for LLM client failures."""


class TransientLLMError(LLMError):
    """Retryable failure; raised after the retry budget is exhausted."""


class PermanentLLMError(LLMError):
    """Non-retryable failure (HTTP 400/401/403/422 or unusable response shape)."""
