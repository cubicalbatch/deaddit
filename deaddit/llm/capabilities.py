"""Per-endpoint/per-model capability probing and gating.

Slice B of Phase LLM-2. The probe asks an OpenAI-compatible endpoint to call
one dummy echo tool with a forced ``tool_choice``; the outcome is stored as a
VERDICT in the ``endpoint_capability`` table (Resolution 11: a failed tools
probe is a verdict, never a fallback trigger).

Verdicts:

- Success with a schema-valid tool call -> ``supports_tools=True``.
- 400 response whose message mentions HTTP 400 + tools/function
  -> ``supports_tools=False`` (the provider told us outright).
- Tool-call envelope present but arguments fail schema validation
  -> ``supports_tools=False`` (unreliable tools are treated as no tools).
- Transient or any other failure -> LOW CONFIDENCE: nothing recorded, the
  exception propagates so the caller can retry later.

A manual override row (``probe_method='manual'``) always wins over probes.

CLI::

    uv run python -m deaddit.llm.capabilities --api-url URL --model NAME [--api-key K]
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import re
import uuid
from datetime import datetime, timedelta

from pydantic import BaseModel

from deaddit.extensions import db
from deaddit.llm.errors import (
    CapabilityError,
    LLMError,
    PermanentLLMError,
    SchemaValidationError,
    TransientLLMError,
)
from deaddit.llm.provider import get_provider, get_stream_provider
from deaddit.llm.tools import ToolSpec, validate_tool_args
from deaddit.models import EndpointCapability

# A 400 that names tools/function is the provider saying "I don't do tools".
_TOOLS_HINT_RE = re.compile(r"\b(tools?|function)\b", re.IGNORECASE)

# A 400 that names image/vision input is the provider saying "no vision".
_VISION_HINT_RE = re.compile(r"\b(image|vision|multimodal)\b", re.IGNORECASE)

logger = logging.getLogger(__name__)

# ponytail: a False verdict from a probe is re-checked after this long.
# Routed endpoints (OpenRouter) rotate upstream providers, so one bad
# sample can mark a tools-capable model as unsupported. Fixed TTL is fine
# until a model visibly flips verdicts often.
_FALSE_VERDICT_TTL = timedelta(hours=6)

LAST_PROBE_EVIDENCE: dict | None = None
"""Raw echo-test evidence from the most recent probe_endpoint call.

Keys: ``response_id``, ``finish_reason``, ``tool_name`` and the
schema-validated ``arguments`` dict (None when validation failed). Cleared at
the start of each probe so decision-2 re-probes never see stale evidence.
"""


class EchoArgs(BaseModel):
    """Schema for the dummy echo tool used by :func:`probe_endpoint`."""

    message: str


_ECHO_SPEC = ToolSpec(
    name="echo_probe",
    description="Echo the given message back. Used to probe tool support.",
    parameters_model=EchoArgs,
)


def _probe_payload(model_name: str) -> dict:
    """Chat payload forcing a single call to the echo tool."""
    return {
        "model": model_name,
        "messages": [
            {
                "role": "user",
                "content": "Call the echo_probe tool with message set to 'ping'.",
            }
        ],
        "tools": [_ECHO_SPEC.to_openai_tool()],
        "tool_choice": {"type": "function", "function": {"name": _ECHO_SPEC.name}},
    }


def get_capability(api_url: str, model_name: str) -> EndpointCapability | None:
    """Return the cached verdict row for this endpoint/model, or None."""
    return EndpointCapability.query.filter_by(
        api_url=api_url, model_name=model_name
    ).first()


def set_manual_override(api_url: str, model_name: str, supports_tools: bool) -> None:
    """Record a human decision; it always wins over probes."""
    cap = get_capability(api_url, model_name)
    if cap is None:
        cap = EndpointCapability(api_url=api_url, model_name=model_name)
        db.session.add(cap)
    cap.supports_tools = supports_tools
    cap.probe_method = "manual"
    cap.probed_at = datetime.utcnow()
    db.session.commit()


def mark_stale(api_url: str, model_name: str) -> None:
    """Drop the cached row so the next gate/probe re-runs."""
    cap = get_capability(api_url, model_name)
    if cap is not None:
        db.session.delete(cap)
        db.session.commit()


def _record_verdict(
    api_url: str, model_name: str, *, supports_tools: bool
) -> EndpointCapability:
    cap = get_capability(api_url, model_name)
    if cap is None:
        cap = EndpointCapability(api_url=api_url, model_name=model_name)
        db.session.add(cap)
    cap.supports_tools = supports_tools
    cap.probed_at = datetime.utcnow()
    cap.probe_method = "probe"
    db.session.commit()
    return cap


def probe_endpoint(
    api_url: str,
    model_name: str,
    api_key: str | None = None,
    read_timeout: int = 30,
) -> EndpointCapability:
    """Probe one endpoint/model for tool-calling support and store a verdict.

    Returns the recorded row. A ``manual`` override is never overwritten —
    the existing row is returned unchanged. Transient failures raise
    :class:`TransientLLMError` (or any other transport error) without
    touching the cache.
    """
    global LAST_PROBE_EVIDENCE
    existing = get_capability(api_url, model_name)
    if existing is not None and existing.probe_method == "manual":
        return existing
    LAST_PROBE_EVIDENCE = None

    provider = get_provider()
    try:
        response = provider(
            api_url=api_url,
            payload=_probe_payload(model_name),
            api_key=api_key,
            request_id=f"capability-probe-{uuid.uuid4().hex[:12]}",
            read_timeout=read_timeout,
        )
    except TransientLLMError:
        # Low confidence: record nothing, let the caller retry later.
        raise
    except PermanentLLMError as exc:
        text = str(exc)
        if "HTTP 400" in text and _TOOLS_HINT_RE.search(text):
            # The provider told us outright: a VERDICT, not a fallback.
            LAST_PROBE_EVIDENCE = {"error": text}
            logger.warning(
                "Tools probe: %s/%s rejected tools (HTTP 400): %.200s",
                api_url,
                model_name,
                text,
            )
            return _record_verdict(api_url, model_name, supports_tools=False)
        raise

    choice = response["choices"][0] if response.get("choices") else {}
    message = choice.get("message") or {}
    calls = message.get("tool_calls") or []
    supports_tools = False
    validated_args = None
    tool_name = calls[0].get("function", {}).get("name") if calls else None
    if calls:
        function = calls[0].get("function") or {}
        if function.get("name") == _ECHO_SPEC.name:
            try:
                validated_args = validate_tool_args(
                    _ECHO_SPEC, function.get("arguments") or {}
                )
                supports_tools = True
            except SchemaValidationError:
                # Envelope present but args don't validate: unreliable.
                supports_tools = False
    if not supports_tools:
        logger.warning(
            "Tools probe: %s/%s returned no valid tool call "
            "(finish_reason=%s, tool_name=%s, content=%.200r) — "
            "recording NOT-SUPPORTED",
            api_url,
            model_name,
            choice.get("finish_reason"),
            tool_name,
            message.get("content") or "",
        )
    LAST_PROBE_EVIDENCE = {
        "response_id": response.get("id"),
        "finish_reason": choice.get("finish_reason"),
        "tool_name": tool_name,
        "arguments": validated_args,
    }
    return _record_verdict(api_url, model_name, supports_tools=supports_tools)


LAST_STREAM_PROBE_EVIDENCE: dict | None = None
"""Evidence from the most recent probe_streaming call.

Keys: ``chunk_count``, ``finish_reason``, ``sample`` (first token-delta
text) and ``request_id``. Cleared at the start of each stream probe.
"""


def _stream_probe_payload(model_name: str) -> dict:
    """Minimal streaming chat payload: a tiny ping, a few tokens at most."""
    return {
        "model": model_name,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
        "stream": True,
    }


def _delta_has_token(delta: dict) -> bool:
    return bool(
        delta.get("content")
        or delta.get("reasoning")
        or delta.get("reasoning_content")
        or delta.get("tool_calls")
    )


def probe_streaming(
    api_url: str,
    model_name: str,
    api_key: str | None = None,
    read_timeout: int = 30,
) -> bool:
    """Probe-and-set ``supports_streaming`` on the EndpointCapability row.

    A ``probe_method='manual'`` row is never overwritten — its stored value
    is returned as-is. When NO row exists yet, :func:`probe_endpoint` runs
    FIRST so an honest tools verdict (``supports_tools`` is NOT NULL) lands
    before ``supports_streaming`` is updated on that same row. At least one
    token delta before ``[DONE]`` counts as streaming-capable. Transient
    failures record no streaming verdict and raise :class:`TransientLLMError`.
    """
    global LAST_STREAM_PROBE_EVIDENCE
    existing = get_capability(api_url, model_name)
    if existing is not None and existing.probe_method == "manual":
        return bool(existing.supports_streaming)
    LAST_STREAM_PROBE_EVIDENCE = None

    cap = get_capability(api_url, model_name)
    if cap is None:
        # Create the honest row first; supports_tools must not stay NULL.
        cap = probe_endpoint(
            api_url, model_name, api_key=api_key, read_timeout=read_timeout
        )

    request_id = f"stream-probe-{uuid.uuid4().hex[:12]}"
    chunk_count = 0
    finish_reason = None
    sample = None
    try:
        for chunk in get_stream_provider()(
            api_url=api_url,
            payload=_stream_probe_payload(model_name),
            api_key=api_key,
            request_id=request_id,
            read_timeout=read_timeout,
        ):
            choices = chunk.get("choices") or []
            choice = choices[0] if choices else {}
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if _delta_has_token(delta):
                chunk_count += 1
                if sample is None:
                    sample = (
                        delta.get("content")
                        or delta.get("reasoning")
                        or delta.get("reasoning_content")
                    )
    except TransientLLMError:
        raise
    except LLMError as exc:
        raise TransientLLMError(f"Streaming probe failed: {exc}") from exc

    supports_streaming = chunk_count >= 1
    LAST_STREAM_PROBE_EVIDENCE = {
        "chunk_count": chunk_count,
        "finish_reason": finish_reason,
        "sample": sample,
        "request_id": request_id,
    }
    cap.supports_streaming = supports_streaming
    db.session.commit()
    return supports_streaming


# ---------------------------------------------------------------------------
# Vision (image-input) capability -- Phase 5A.
#
# A third, independent verdict on the same row. It never reads or writes
# supports_tools, supports_streaming, probed_at or probe_method: those stay
# exactly as the tools/streaming probes left them. Its own probed_at/method
# columns give it the same "manual always wins" precedence without
# entangling it with the tools verdict.


def _probe_image_data_url() -> str:
    """A tiny solid-color PNG, generated once, encoded as a data URL."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color=(255, 0, 0)).save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


_PROBE_IMAGE_DATA_URL = _probe_image_data_url()
_VISION_PROBE_EXPECTED_WORD = "red"

LAST_VISION_PROBE_EVIDENCE: dict | None = None
"""Raw evidence from the most recent probe_vision call.

Keys: ``response_id``, ``finish_reason`` and ``reply_text``. Cleared at the
start of each probe so decision re-probes never see stale evidence.
"""


def _vision_probe_payload(model_name: str) -> dict:
    """Chat payload asking the model to name the color of a tiny image."""
    return {
        "model": model_name,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "What single color fills this image? Reply with "
                            "exactly one word and nothing else."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": _PROBE_IMAGE_DATA_URL},
                    },
                ],
            }
        ],
        "max_tokens": 10,
    }


def is_vision_capable(api_url: str, model_name: str) -> bool:
    """Conservative vision read for callers describing images to an agent.

    A normal agent read never probes; a never-probed endpoint or an
    explicit ``supports_vision=NULL`` verdict both return False here, so
    "unknown" degrades to the stored source prompt rather than a guess.
    """
    cap = get_capability(api_url, model_name)
    return bool(cap is not None and cap.supports_vision)


def set_vision_manual_override(
    api_url: str, model_name: str, supports_vision: bool
) -> None:
    """Record a human decision on vision support; it always wins over probes."""
    cap = get_capability(api_url, model_name)
    if cap is None:
        # supports_tools is NOT NULL; a vision-only override on a
        # never-probed endpoint has no tools evidence, so it defaults to
        # False and probe_method is left unset -- the tools verdict is
        # simply not yet known, not "probed and failed".
        cap = EndpointCapability(
            api_url=api_url, model_name=model_name, supports_tools=False
        )
        db.session.add(cap)
    cap.supports_vision = supports_vision
    cap.vision_probe_method = "manual"
    cap.vision_probed_at = datetime.utcnow()
    db.session.commit()


def _record_vision_verdict(
    api_url: str, model_name: str, *, supports_vision: bool
) -> EndpointCapability:
    cap = get_capability(api_url, model_name)
    cap.supports_vision = supports_vision
    cap.vision_probed_at = datetime.utcnow()
    cap.vision_probe_method = "probe"
    db.session.commit()
    return cap


def probe_vision(
    api_url: str,
    model_name: str,
    api_key: str | None = None,
    read_timeout: int = 30,
) -> EndpointCapability:
    """Probe one endpoint/model for image-input (vision) support.

    Sends a tiny solid-color image as an OpenAI-compatible data URL and
    asks the model to name the color; a matching answer is the only way to
    earn a True verdict. A ``vision_probe_method='manual'`` row is never
    overwritten -- the existing row is returned unchanged. When no row
    exists yet, :func:`probe_endpoint` runs FIRST so an honest tools
    verdict lands before the vision verdict is added to that same row.
    Transient failures raise :class:`TransientLLMError` without touching
    the cache. This never changes supports_tools, supports_streaming,
    probed_at or probe_method.
    """
    global LAST_VISION_PROBE_EVIDENCE
    existing = get_capability(api_url, model_name)
    if existing is not None and existing.vision_probe_method == "manual":
        return existing
    LAST_VISION_PROBE_EVIDENCE = None

    cap = get_capability(api_url, model_name)
    if cap is None:
        # Create the honest row first; supports_tools must not stay NULL.
        cap = probe_endpoint(
            api_url, model_name, api_key=api_key, read_timeout=read_timeout
        )

    provider = get_provider()
    try:
        response = provider(
            api_url=api_url,
            payload=_vision_probe_payload(model_name),
            api_key=api_key,
            request_id=f"vision-probe-{uuid.uuid4().hex[:12]}",
            read_timeout=read_timeout,
        )
    except TransientLLMError:
        # Low confidence: record nothing, let the caller retry later.
        raise
    except PermanentLLMError as exc:
        text = str(exc)
        if "HTTP 400" in text and _VISION_HINT_RE.search(text):
            # The provider told us outright: a VERDICT, not a fallback.
            LAST_VISION_PROBE_EVIDENCE = {"error": text}
            return _record_vision_verdict(api_url, model_name, supports_vision=False)
        raise

    choice = response["choices"][0] if response.get("choices") else {}
    message = choice.get("message") or {}
    reply_text = message.get("content")
    if not isinstance(reply_text, str):
        reply_text = ""
    supports_vision = _VISION_PROBE_EXPECTED_WORD in reply_text.lower()
    LAST_VISION_PROBE_EVIDENCE = {
        "response_id": response.get("id"),
        "finish_reason": choice.get("finish_reason"),
        "reply_text": reply_text,
    }
    return _record_vision_verdict(api_url, model_name, supports_vision=supports_vision)


def ensure_tools_allowed(
    api_url: str,
    model_name: str,
    *,
    api_key: str | None = None,
    request_id: str | None = None,
    auto_probe: bool = False,
) -> None:
    """Gate tool use on a stored (or freshly probed) verdict.

    Raises :class:`CapabilityError` when the cached row says
    ``supports_tools=False``. No row passes unless ``auto_probe`` triggers a
    probe first; a manual override with ``True`` passes even though earlier
    probes failed.

    A stale ``probe_method='probe'`` row with ``supports_tools=False`` is
    re-probed instead of trusted: routed endpoints rotate upstreams, so a
    single bad sample must not block the model forever. Manual overrides
    are never re-probed. If the re-probe fails to determine a verdict, the
    cached False still governs (same error as before).
    """
    cap = get_capability(api_url, model_name)
    if (
        cap is not None
        and not cap.supports_tools
        and cap.probe_method == "probe"
        and cap.probed_at is not None
        and datetime.utcnow() - cap.probed_at > _FALSE_VERDICT_TTL
    ):
        try:
            cap = probe_endpoint(api_url, model_name, api_key=api_key)
        except LLMError:
            logger.warning(
                "Stale tools re-probe failed for %s/%s; keeping cached verdict",
                api_url,
                model_name,
                exc_info=True,
            )
    if cap is None and auto_probe:
        cap = probe_endpoint(api_url, model_name, api_key=api_key)
    if cap is None or cap.supports_tools:
        return
    raise CapabilityError(
        f"Model '{model_name}' at {api_url} does not support tool calling "
        f"(probe_method={cap.probe_method}).",
        api_url=api_url,
        model=model_name,
        request_id=request_id,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m deaddit.llm.capabilities",
        description="Probe an endpoint/model for tool-calling support.",
    )
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key", default=None)
    args = parser.parse_args(argv)

    from deaddit import create_app

    app = create_app()
    with app.app_context():
        try:
            cap = probe_endpoint(args.api_url, args.model, api_key=args.api_key)
        except TransientLLMError as exc:
            # Failure-to-determine: nothing was recorded; safe to retry.
            print(json.dumps({"verdict": "unknown", "error": str(exc)}))
            return 2
        evidence = {
            "api_url": cap.api_url,
            "model_name": cap.model_name,
            "supports_tools": cap.supports_tools,
            "supports_streaming": cap.supports_streaming,
            "probed_at": cap.probed_at.isoformat() if cap.probed_at else None,
            "probe_method": cap.probe_method,
            "probe_evidence": LAST_PROBE_EVIDENCE,
        }
        verdict = "supported" if cap.supports_tools else "not supported"
        print(f"verdict: tools {verdict}")
        print(json.dumps(evidence, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
