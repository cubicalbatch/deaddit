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
import json
import re
import uuid
from datetime import datetime

from pydantic import BaseModel

from deaddit.extensions import db
from deaddit.llm.errors import (
    CapabilityError,
    PermanentLLMError,
    SchemaValidationError,
    TransientLLMError,
)
from deaddit.llm.provider import get_provider
from deaddit.llm.tools import ToolSpec, validate_tool_args
from deaddit.models import EndpointCapability

# A 400 that names tools/function is the provider saying "I don't do tools".
_TOOLS_HINT_RE = re.compile(r"\b(tools?|function)\b", re.IGNORECASE)

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
    LAST_PROBE_EVIDENCE = {
        "response_id": response.get("id"),
        "finish_reason": choice.get("finish_reason"),
        "tool_name": tool_name,
        "arguments": validated_args,
    }
    return _record_verdict(api_url, model_name, supports_tools=supports_tools)


def ensure_tools_allowed(
    api_url: str,
    model_name: str,
    *,
    request_id: str | None = None,
    auto_probe: bool = False,
) -> None:
    """Gate tool use on a stored (or freshly probed) verdict.

    Raises :class:`CapabilityError` when the cached row says
    ``supports_tools=False``. No row passes unless ``auto_probe`` triggers a
    probe first; a manual override with ``True`` passes even though earlier
    probes failed.
    """
    cap = get_capability(api_url, model_name)
    if cap is None and auto_probe:
        cap = probe_endpoint(api_url, model_name)
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
            "context_tokens": cap.context_tokens,
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
