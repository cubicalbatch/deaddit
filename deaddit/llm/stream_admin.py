"""Admin live-token streaming surface (LLM-4 slice B).

POST /admin/llm/stream-token consumes ``LLMClient.stream`` and relays every
delta as a socket.io ``llm_stream`` event on namespace "/admin", room
``request_id``. Event-name registry (shared contract): server->client
"llm_stream"; client->server "join_llm_stream"/"leave_llm_stream" (see
deaddit/websocket.py). UX-5's "job_log" channel is separate — never emit it
here.
"""

from __future__ import annotations

import time

from flask import Blueprint, jsonify, request

from deaddit.admin import admin_required
from deaddit.extensions import socketio
from deaddit.llm import capabilities, routing
from deaddit.llm.client import (
    ChatRequest,
    Done,
    LLMClient,
    ReasoningDelta,
    Sampling,
    TokenDelta,
    ToolCallDelta,
)
from deaddit.utils import production_disabled

stream_admin_bp = Blueprint("llm_stream_admin", __name__, url_prefix="/admin")

NAMESPACE = "/admin"


def _emit(request_id: str, kind: str, data: dict) -> None:
    """Fire-and-forget relay into the per-request admin room."""
    socketio.emit(
        "llm_stream",
        {"request_id": request_id, "kind": kind, "data": data, "ts": time.time()},
        namespace=NAMESPACE,
        to=request_id,
    )


def _fail(request_id: str, message: str, status: int = 400):
    _emit(request_id, "error", {"message": message})
    return (
        jsonify(
            {
                "request_id": request_id,
                "streamed": False,
                "content_chars": 0,
                "attempts": 0,
                "latency_ms": 0.0,
                "error": message,
            }
        ),
        status,
    )


@stream_admin_bp.route("/llm/stream-token", methods=["POST"])
@production_disabled
@admin_required
def llm_stream_token():
    started = time.time()
    payload = request.get_json(silent=True) or {}

    request_id = payload.get("request_id")
    user_prompt = payload.get("user_prompt")
    if (
        not isinstance(request_id, str)
        or not request_id.strip()
        or not isinstance(user_prompt, str)
        or not user_prompt.strip()
    ):
        return _fail(
            request_id if isinstance(request_id, str) else "",
            "request_id and user_prompt are required",
        )
    request_id = request_id.strip()

    max_tokens = payload.get("max_tokens")
    if max_tokens is not None:
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            return _fail(request_id, "max_tokens must be an integer")

    try:
        api_url = payload.get("api_url") or None
        model = payload.get("model") or None
        if api_url is None or model is None:
            resolved_url, resolved_model = routing.resolve()
            api_url = api_url or resolved_url
            model = model or resolved_model
    except Exception as exc:
        return _fail(request_id, f"routing failed: {exc}", status=502)

    # Best-effort verdict for the meta event; unknown capability assumes live
    # streaming and the synthesized fallback path still lands cleanly.
    cap = capabilities.get_capability(api_url, model)
    streamed_meta = bool(cap.supports_streaming) if cap is not None else True

    req = ChatRequest(
        system_prompt=payload.get("system_prompt") or "",
        user_prompt=user_prompt,
        model=model,
        api_url=api_url,
        sampling=Sampling(max_tokens=max_tokens) if max_tokens is not None else None,
    )

    _emit(request_id, "meta", {"streamed": streamed_meta, "model": model, "api_url": api_url})

    def observer(event) -> None:
        # Done is emitted by the consuming loop below exactly once, so the
        # observer deliberately skips it (double-emission guard).
        if isinstance(event, Done):
            return
        if isinstance(event, TokenDelta):
            _emit(request_id, "token", {"text": event.text})
        elif isinstance(event, ReasoningDelta):
            _emit(request_id, "reasoning", {"text": event.text})
        elif isinstance(event, ToolCallDelta):
            _emit(request_id, "tool", {"name": event.name, "args_partial": event.args_partial})

    done: Done | None = None
    try:
        for event in LLMClient().stream(req, observer=observer):
            if isinstance(event, Done):
                done = event
    except Exception as exc:
        latency_ms = round((time.time() - started) * 1000, 2)
        _emit(request_id, "error", {"message": str(exc)})
        resp = {
            "request_id": request_id,
            "streamed": False,
            "content_chars": 0,
            "attempts": 0,
            "latency_ms": latency_ms,
            "error": str(exc),
        }
        return jsonify(resp), 502

    if done is None:
        return _fail(request_id, "stream ended without a Done event", status=502)

    result = done.result
    content_chars = len(result.content or "")
    summary = {
        "synthesized": bool(done.synthesized),
        "content": result.content or "",
        "content_chars": content_chars,
        "attempts": result.attempts,
        "latency_ms": result.latency_ms,
        "usage": result.usage or {},
        "model": result.model,
    }
    _emit(request_id, "done", summary)

    return jsonify(
        {
            "request_id": request_id,
            "streamed": not done.synthesized,
            "content_chars": content_chars,
            "attempts": result.attempts,
            "latency_ms": result.latency_ms,
        }
    )
