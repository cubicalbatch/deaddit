"""Deterministic tests for the LLM-4 admin live-token surface (LLM-4 slice B).

No network: all model traffic goes through FakeProvider's chunked-stream seam.
Capability rows are pinned per test so client.stream never attempts a live
probe. The per-request room join is provided by a test-private socket handler
so this suite does not depend on the concurrent UX-5 websocket.py landing.
"""

from __future__ import annotations

import time

import pytest
from flask_socketio import join_room

from deaddit.extensions import socketio
from deaddit.models import EndpointCapability, Setting

RID = "test-req-0001"
API_URL = "http://localhost/v1"
MODEL = "qwen3.8-27b"


def _register_test_join_handler() -> None:
    """Register the private room-join handler on the CURRENT socketio server.

    flask_socketio binds @socketio.on decorators to whichever Server instance
    exists at call time; every create_app() mints a fresh bare Server, so the
    registration must happen per-fixture, AFTER app setup and BEFORE the test
    client connects.
    """

    @socketio.on("_llm4_test_join", namespace="/admin")
    def _join(data):
        join_room((data or {}).get("request_id"))


@pytest.fixture()
def sio(app):
    """A connected flask-socketio test client on the /admin namespace."""
    _register_test_join_handler()
    ws = socketio.test_client(app, namespace="/admin")
    ws.get_received(namespace="/admin")  # drain the connect handshake event
    return ws


def _drain_stream_events(sio):
    events = []
    deadline = time.time() + 2.0
    while time.time() < deadline:
        batch = sio.get_received(namespace="/admin")
        for msg in batch:
            if msg.get("name") == "llm_stream":
                events.append(msg["args"][0])
        if not batch:
            break
    return events


def _stream(client, payload):
    return client.post("/admin/llm/stream-token", json=payload)


def _cap_row(db_session, supports_streaming):
    db_session.add(
        EndpointCapability(
            api_url=API_URL,
            model_name=MODEL,
            supports_tools=False,
            supports_streaming=supports_streaming,
            probe_method="auto",
        )
    )
    db_session.commit()


def test_anonymous_blocked_when_api_token_set(app, client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "unit-test-token")
    resp = _stream(client, {"request_id": RID, "user_prompt": "hi"})
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]
    monkeypatch.delenv("API_TOKEN")


def test_production_disabled_honored(app, client, db_session):
    db_session.add(Setting(key="PRODUCTION", value="true"))
    db_session.commit()
    resp = _stream(client, {"request_id": RID, "user_prompt": "hi"})
    assert resp.status_code == 404


def test_happy_path_emits_meta_tokens_done_in_order(
    app, client, fake_llm, sio, db_session
):
    _cap_row(db_session, supports_streaming=True)
    fake_llm.enqueue_stream(
        [
            {"choices": [{"delta": {"role": "assistant", "content": "Hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
            {"choices": [{"delta": {}}], "finish_reason": "stop"},
        ],
        usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    )
    sio.emit("_llm4_test_join", {"request_id": RID}, namespace="/admin")

    resp = _stream(
        client,
        {
            "request_id": RID,
            "user_prompt": "Say hello",
            "api_url": API_URL,
            "model": MODEL,
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["request_id"] == RID
    assert body["streamed"] is True
    assert body["content_chars"] == 5

    events = _drain_stream_events(sio)
    kinds = [ev["kind"] for ev in events]
    assert kinds[0] == "meta"
    assert kinds[-1] == "done"
    assert kinds.count("token") == 2
    assert all(ev["request_id"] == RID for ev in events)

    meta = events[0]["data"]
    assert set(meta) >= {"streamed", "model", "api_url"}
    assert meta["streamed"] is True

    token_text = "".join(ev["data"]["text"] for ev in events if ev["kind"] == "token")
    assert token_text == "Hello"

    done = events[-1]["data"]
    assert done["content_chars"] == 5
    assert done["synthesized"] is False
    assert isinstance(done["attempts"], int)


def test_non_streaming_capability_falls_back_to_single_token(
    app, client, fake_llm, db_session, sio
):
    _cap_row(db_session, supports_streaming=False)
    fake_llm.enqueue_content("full text in one piece")
    sio.emit("_llm4_test_join", {"request_id": RID}, namespace="/admin")

    resp = _stream(
        client,
        {"request_id": RID, "user_prompt": "hi", "api_url": API_URL, "model": MODEL},
    )
    assert resp.status_code == 200
    assert resp.get_json()["streamed"] is False

    events = _drain_stream_events(sio)
    kinds = [ev["kind"] for ev in events]
    tokens = [ev for ev in events if ev["kind"] == "token"]
    assert kinds[0] == "meta"
    assert events[0]["data"]["streamed"] is False
    assert len(tokens) == 1
    assert tokens[0]["data"]["text"] == "full text in one piece"
    assert kinds[-1] == "done"
    assert events[-1]["data"]["synthesized"] is True


def test_malformed_payload_yields_error_kind_and_non_500(app, client, fake_llm, sio):
    resp = client.post("/admin/llm/stream-token", json={"user_prompt": "no request id"})
    assert 400 <= resp.status_code < 500
    assert resp.get_json()["error"]

    resp = client.post("/admin/llm/stream-token", data="not-json", content_type="text/plain")
    assert 400 <= resp.status_code < 500

    events = _drain_stream_events(sio)
    assert any(ev["kind"] == "error" for ev in events)


def test_upstream_failure_yields_error_kind_and_502(app, client, fake_llm, sio, db_session):
    _cap_row(db_session, supports_streaming=True)
    fake_llm.enqueue_stream_error(RuntimeError("connection reset mid-stream"))
    sio.emit("_llm4_test_join", {"request_id": RID}, namespace="/admin")

    resp = _stream(
        client,
        {"request_id": RID, "user_prompt": "hi", "api_url": API_URL, "model": MODEL},
    )
    assert resp.status_code == 502
    assert "reset" in resp.get_json()["error"]

    events = _drain_stream_events(sio)
    kinds = [ev["kind"] for ev in events]
    assert kinds[0] == "meta"
    assert kinds[-1] == "error"
