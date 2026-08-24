"""High-level LLM client: request/result dataclasses and response normalization."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field

from deaddit.llm.errors import PermanentLLMError
from deaddit.llm.transport import last_attempts, post_chat

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
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class ChatResult:
    content: str
    model: str
    usage: dict
    latency_ms: float
    attempts: int
    request_id: str


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
    if req.extra_payload:
        payload.update(req.extra_payload)
    return payload


def _extract_content(response: dict) -> str:
    """Normalize the assistant text out of a chat completion response.

    Verbatim semantics from the old jobs.py sniffing block. No other parsing:
    no JSON salvage, no <think> stripping (Resolution 11).
    """
    try:
        message = response["choices"][0]["message"]
        content = message.get("content") or ""
        if not content and message.get("reasoning"):
            logger.info("Using reasoning field as content")
            return message["reasoning"]
        if content:
            return content
    except (KeyError, IndexError, TypeError, AttributeError):
        pass

    for key in ("content", "response"):
        value = response.get(key)
        if isinstance(value, str) and value:
            return value

    raise PermanentLLMError(f"Unexpected API response format: {str(response)[:200]}")


class LLMClient:
    def complete(self, req: ChatRequest) -> ChatResult:
        payload = _build_payload(req)
        started = time.monotonic()
        data = post_chat(
            api_url=req.api_url,
            payload=payload,
            api_key=req.api_key,
            request_id=req.request_id,
            read_timeout=req.read_timeout,
        )
        latency_ms = (time.monotonic() - started) * 1000.0

        content = _extract_content(data)
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
        )
