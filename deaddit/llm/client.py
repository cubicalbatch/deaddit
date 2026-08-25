"""High-level LLM client: request/result dataclasses and response normalization."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field

from deaddit.llm import accounting
from deaddit.llm.errors import CapabilityError, PermanentLLMError
from deaddit.llm.provider import get_provider, get_stream_provider
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


def _assemble_tool_calls(acc: dict[int, dict]) -> list[dict]:
    """Rebuild native OpenAI message.tool_calls from streamed fragments."""
    calls = []
    for index in sorted(acc):
        entry = acc[index]
        calls.append(
            {
                "id": entry["id"] or f"call_{index}",
                "type": "function",
                "function": {"name": entry["name"], "arguments": entry["arguments"]},
            }
        )
    return calls


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
    action: str | None = None
    agent: str | None = None


@dataclass
class ChatResult:
    content: str
    model: str
    usage: dict
    latency_ms: float
    attempts: int
    request_id: str
    tool_calls: list[dict] | None = None


@dataclass
class TokenDelta:
    """A fragment of assistant content text."""

    text: str


@dataclass
class ReasoningDelta:
    """A fragment of hidden reasoning text (delta.reasoning / _content)."""

    text: str


@dataclass
class ToolCallDelta:
    """One raw argument fragment of a streaming tool call."""

    name: str | None
    args_partial: str


@dataclass
class Done:
    """Terminal event of a stream; exactly one per LLMClient.stream call."""

    result: ChatResult
    synthesized: bool = False


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
        if not content and (
            message.get("reasoning") or message.get("reasoning_content")
        ):
            # Some OpenAI-compatible servers (e.g. qwen deployments) name the
            # hidden-reasoning field `reasoning_content` instead of `reasoning`.
            logger.info("Using reasoning field as content")
            return message.get("reasoning") or message.get("reasoning_content"), None
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
        rec = accounting.AttemptRecorder(req)
        data: dict | None = None
        failure: BaseException | None = None
        try:
            rec.mark_invoked()
            data = get_provider()(
                api_url=req.api_url,
                payload=payload,
                api_key=req.api_key,
                request_id=req.request_id,
                read_timeout=req.read_timeout,
                on_attempt=rec.on_attempt,
            )
        except PermanentLLMError as exc:
            failure = exc
            message = str(exc)
            if (
                req.tools
                and re.search(r"HTTP 400", message)
                and re.search(r"\b(tools?|function)\b", message, re.IGNORECASE)
            ):
                from deaddit.llm.capabilities import mark_stale

                mark_stale(req.api_url, req.model)
                failure = CapabilityError(
                    message,
                    api_url=req.api_url,
                    model=req.model,
                    request_id=req.request_id,
                )
                raise failure from exc
            raise
        except Exception as exc:
            failure = exc
            raise
        finally:
            rec.finalize(
                exc=failure,
                data=data if failure is None else None,
            )
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

    def _resolve_stream_support(self, req: ChatRequest) -> bool:
        """Decide whether real streaming may be attempted for this request.

        Never raises: a missing/unknown/unprovable capability means the
        synthesized (non-streaming) fallback, so the UI can never hang on
        an unknown capability. Probes at most once per call.
        """
        from deaddit.llm.capabilities import get_capability, probe_streaming

        try:
            cap = get_capability(req.api_url, req.model)
        except Exception:
            logger.warning(
                "Capability lookup failed for %s/%s; using non-streaming fallback",
                req.api_url,
                req.model,
                exc_info=True,
            )
            return False
        if cap is not None and cap.supports_streaming is not None:
            return bool(cap.supports_streaming)
        try:
            return bool(probe_streaming(req.api_url, req.model, api_key=req.api_key))
        except Exception:
            # TransientLLMError from the probe: no verdict was recorded;
            # fall back rather than hang or error on an unknown capability.
            logger.warning(
                "Streaming probe failed for %s/%s; using non-streaming fallback",
                req.api_url,
                req.model,
                exc_info=True,
            )
            return False

    def stream(self, req: ChatRequest, *, observer=None) -> Iterator[StreamEvent]:
        """Yield TokenDelta/ReasoningDelta/ToolCallDelta events, then one Done.

        ``observer(event)`` is fire-and-forget: exceptions are swallowed and
        counted (see observer_errors), never allowed to slow generation.
        Fallback law: when streaming is unsupported/unknown, complete() runs
        and its result is delivered as ONE TokenDelta + Done(synthesized=True).
        """
        self.observer_errors = 0

        def emit(event: StreamEvent) -> None:
            if observer is None:
                return
            try:
                observer(event)
            except Exception:
                self.observer_errors += 1
                logger.warning("stream observer failed", exc_info=True)

        if not self._resolve_stream_support(req):
            result = self.complete(req)
            token = TokenDelta(result.content)
            emit(token)
            yield token
            done = Done(result=result, synthesized=True)
            emit(done)
            yield done
            return

        if req.tools:
            from deaddit.llm.capabilities import ensure_tools_allowed

            ensure_tools_allowed(req.api_url, req.model, request_id=req.request_id)
        payload = _build_payload(req)
        started = time.monotonic()
        rec = accounting.AttemptRecorder(req)
        usage: dict = {}
        failure: BaseException | None = None
        try:
            rec.mark_invoked()
            chunks = get_stream_provider()(
                api_url=req.api_url,
                payload=payload,
                api_key=req.api_key,
                request_id=req.request_id,
                read_timeout=req.read_timeout,
                on_attempt=rec.on_attempt,
            )
            content_parts: list[str] = []
            tool_acc: dict[int, dict] = {}
            for chunk in chunks:
                choices = chunk.get("choices") or []
                choice = choices[0] if choices else {}
                delta = choice.get("delta") or {}
                text = delta.get("content")
                if text:
                    content_parts.append(text)
                    event = TokenDelta(text)
                    emit(event)
                    yield event
                reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                if reasoning:
                    event = ReasoningDelta(reasoning)
                    emit(event)
                    yield event
                for frag in delta.get("tool_calls") or []:
                    function = frag.get("function") or {}
                    index = frag.get("index", 0)
                    entry = tool_acc.setdefault(
                        index, {"id": None, "name": None, "arguments": ""}
                    )
                    name = function.get("name")
                    args_partial = function.get("arguments") or ""
                    if frag.get("id"):
                        entry["id"] = frag["id"]
                    if name:
                        entry["name"] = name
                    entry["arguments"] += args_partial
                    event = ToolCallDelta(name=name, args_partial=args_partial)
                    emit(event)
                    yield event
                chunk_usage = chunk.get("usage")
                if isinstance(chunk_usage, dict):
                    usage.update(chunk_usage)
        except Exception as exc:
            failure = exc
            raise
        finally:
            rec.finalize(
                exc=failure, data={"usage": usage} if failure is None else None
            )

        latency_ms = (time.monotonic() - started) * 1000.0
        tool_calls = _assemble_tool_calls(tool_acc)
        result = ChatResult(
            content="".join(content_parts),
            model=req.model,
            usage=usage,
            latency_ms=latency_ms,
            attempts=last_attempts(),
            request_id=req.request_id,
            tool_calls=tool_calls or None,
        )
        logger.info(
            "LLM stream complete: request_id=%s model=%s endpoint=%s "
            "latency_ms=%.0f chars=%d",
            req.request_id,
            req.model,
            req.api_url,
            latency_ms,
            len(result.content),
        )
        done = Done(result=result, synthesized=False)
        emit(done)
        yield done


StreamEvent = TokenDelta | ReasoningDelta | ToolCallDelta | Done
