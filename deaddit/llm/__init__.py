"""Consolidated OpenAI-compatible LLM client."""

from deaddit.llm.capabilities import (
    LAST_PROBE_EVIDENCE,
    LAST_STREAM_PROBE_EVIDENCE,
    LAST_VISION_PROBE_EVIDENCE,
    ensure_tools_allowed,
    get_capability,
    is_vision_capable,
    mark_stale,
    probe_endpoint,
    probe_streaming,
    probe_vision,
    set_manual_override,
    set_vision_manual_override,
)
from deaddit.llm.client import (
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
from deaddit.llm.vision import ImageDescriptionError, describe_image

__all__ = [
    "LAST_PROBE_EVIDENCE",
    "LAST_STREAM_PROBE_EVIDENCE",
    "LAST_VISION_PROBE_EVIDENCE",
    "CapabilityError",
    "ChatRequest",
    "ChatResult",
    "Done",
    "ImageDescriptionError",
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
    "describe_image",
    "ensure_tools_allowed",
    "get_capability",
    "get_provider",
    "get_stream_provider",
    "is_vision_capable",
    "mark_stale",
    "probe_endpoint",
    "probe_streaming",
    "probe_vision",
    "reset_provider",
    "set_manual_override",
    "set_provider",
    "set_vision_manual_override",
    "validate_tool_args",
]
