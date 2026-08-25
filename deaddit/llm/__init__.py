"""Consolidated OpenAI-compatible LLM client."""

from deaddit.llm.capabilities import (
    LAST_PROBE_EVIDENCE,
    LAST_STREAM_PROBE_EVIDENCE,
    ensure_tools_allowed,
    get_capability,
    mark_stale,
    probe_endpoint,
    probe_streaming,
    set_manual_override,
)
from deaddit.llm.client import (
    STOP_VALUES,
    ChatRequest,
    ChatResult,
    Done,
    LLMClient,
    ReasoningDelta,
    Sampling,
    StreamEvent,
    TokenDelta,
    ToolCallDelta,
)
from deaddit.llm.errors import (
    CapabilityError,
    LLMError,
    PermanentLLMError,
    SchemaValidationError,
    TransientLLMError,
)
from deaddit.llm.provider import (
    get_provider,
    get_stream_provider,
    reset_provider,
    set_provider,
)
from deaddit.llm.tools import ToolSpec, validate_tool_args

__all__ = [
    "LAST_PROBE_EVIDENCE",
    "LAST_STREAM_PROBE_EVIDENCE",
    "STOP_VALUES",
    "CapabilityError",
    "ChatRequest",
    "ChatResult",
    "Done",
    "LLMClient",
    "LLMError",
    "PermanentLLMError",
    "ReasoningDelta",
    "Sampling",
    "SchemaValidationError",
    "StreamEvent",
    "TokenDelta",
    "ToolCallDelta",
    "ToolSpec",
    "TransientLLMError",
    "ensure_tools_allowed",
    "get_capability",
    "get_provider",
    "get_stream_provider",
    "mark_stale",
    "probe_endpoint",
    "probe_streaming",
    "reset_provider",
    "set_manual_override",
    "set_provider",
    "validate_tool_args",
]
