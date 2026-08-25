"""High-level LLM client: request/result dataclasses and response normalization."""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from deaddit.llm.errors import CapabilityError, PermanentLLMError
from deaddit.llm.provider import get_provider
from deaddit.llm.tools import ToolSpec
from deaddit.llm.transport import last_attempts

logger = logging.getLogger(__name__)
STOP_VALUES: list[str] = [
    "}\n```\n",
    "assistant",
    "}  #",
    "} #",
    "}\n\n",
    "}\n}",
    "##",
    "}\n\n",
    "```\n\n",
]


@dataclass
class Sampling:
    temperature: float | None = None  # None -> omit from payload
    max_tokens: int = 2048
    stop: list[str] | None = None  # None -> omit


@dataclass
class ChatRequest:
    system_prompt: str
    user_prompt: str
    model: str
    api_url: str  # base URL incl. /v1
    api_key: str | None = None
    sampling: Sampling | None = None
    extra_payload: dict | None = None
    read_timeout: float = 120.0
    tools: list[ToolSpec] | None = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class ChatResult:
    content: str
    model: str
    usage: dict
    latency_ms: float
    attempts: int
    request_id: str
    tool_calls: list[dict] | None = None


def _build_payload(req: ChatRequest) -> dict:
    payload: dict = {
        "model": req.model,
        "messages": [
            {"role": "system", "content": req.system_prompt},
            {"role": "user", "content": req.user_prompt},
        ],
        "max_tokens": req.sampling.max_tokens if req.sampling else 2048,
    }
    if req.sampling:
        if req.sampling.temperature is not None:
            payload["temperature"] = req.sampling.temperature
        if req.sampling.stop is not None:
            stop = list(req.sampling.stop)
            if "api.groq.com" in req.api_url:
                # Groq only supports 4 stop values.
                stop = stop[:4]
            payload["stop"] = stop
    if req.tools:
        payload["tools"] = [t.to_openai_tool() for t in req.tools]
    if req.extra_payload:
        payload.update(req.extra_payload)
    return payload


def _extract_response(response: dict) -> tuple[str, list[dict] | None]:
    """Normalize assistant text and native tool_calls out of a response.

    Native message.tool_calls are surfaced verbatim on ChatResult.tool_calls
    (with content possibly empty). No other parsing: no JSON salvage, no
    <think> stripping (Resolution 11).
    """
    try:
        message = response["choices"][0]["message"]
        content = message.get("content") or ""
        tool_calls = message.get("tool_calls") or None
        if tool_calls is not None:
            return content, tool_calls
        if not content and message.get("reasoning"):
            logger.info("Using reasoning field as content")
            return message["reasoning"], None
        if content:
            return content, None
    except (KeyError, IndexError, TypeError, AttributeError):
        pass

    for key in ("content", "response"):
        value = response.get(key)
        if isinstance(value, str) and value:
            return value, None

    raise PermanentLLMError(f"Unexpected API response format: {str(response)[:200]}")


class LLMClient:
    def complete(self, req: ChatRequest) -> ChatResult:
        if req.tools:
            from deaddit.llm.capabilities import ensure_tools_allowed

            # Raises CapabilityError on an explicit supports_tools=False verdict.
            ensure_tools_allowed(req.api_url, req.model, request_id=req.request_id)
        payload = _build_payload(req)
        started = time.monotonic()
        try:
            data = get_provider()(
                api_url=req.api_url,
                payload=payload,
                api_key=req.api_key,
                request_id=req.request_id,
                read_timeout=req.read_timeout,
            )
        except PermanentLLMError as exc:
            message = str(exc)
            if (
                req.tools
                and re.search(r"HTTP 400", message)
                and re.search(r"\b(tools?|function)\b", message, re.IGNORECASE)
            ):
                from deaddit.llm.capabilities import mark_stale

                mark_stale(req.api_url, req.model)
                raise CapabilityError(
                    message,
                    api_url=req.api_url,
                    model=req.model,
                    request_id=req.request_id,
                ) from exc
            raise
        latency_ms = (time.monotonic() - started) * 1000.0
        content, tool_calls = _extract_response(data)
        usage = data.get("usage") or {}
        attempts = last_attempts()

        logger.info(
            "LLM call complete: request_id=%s model=%s endpoint=%s attempts=%d "
            "latency_ms=%.0f chars=%d",
            req.request_id,
            req.model,
            req.api_url,
            attempts,
            latency_ms,
            len(content),
        )

        return ChatResult(
            content=content,
            model=req.model,
            usage=usage,
            latency_ms=latency_ms,
            attempts=attempts,
            request_id=req.request_id,
            tool_calls=tool_calls,
        )
