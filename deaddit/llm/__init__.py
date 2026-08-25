"""Consolidated OpenAI-compatible LLM client."""

from deaddit.llm.capabilities import (
    ensure_tools_allowed,
    get_capability,
    mark_stale,
    probe_endpoint,
    set_manual_override,
)
from deaddit.llm.client import (
    STOP_VALUES,
    ChatRequest,
    ChatResult,
    LLMClient,
    Sampling,
)
from deaddit.llm.errors import (
    CapabilityError,
    LLMError,
    PermanentLLMError,
    SchemaValidationError,
    TransientLLMError,
)
from deaddit.llm.provider import get_provider, reset_provider, set_provider
from deaddit.llm.tools import ToolSpec, validate_tool_args

__all__ = [
    "STOP_VALUES",
    "CapabilityError",
    "ChatRequest",
    "ChatResult",
    "LLMClient",
    "LLMError",
    "PermanentLLMError",
    "Sampling",
    "SchemaValidationError",
    "ToolSpec",
    "TransientLLMError",
    "ensure_tools_allowed",
    "get_capability",
    "get_provider",
    "mark_stale",
    "probe_endpoint",
    "reset_provider",
    "set_manual_override",
    "set_provider",
    "validate_tool_args",
]
