"""Phase LLM-4 Slice A: streaming core tests.

Deterministic only — every scenario runs through FakeProvider's
chunked-stream mode registered on the provider seam. No network.
"""

from __future__ import annotations

import json

import pytest
import requests

from deaddit.llm import (
    ChatRequest,
    Done,
    LLMClient,
    ReasoningDelta,
    ToolCallDelta,
    capabilities,
    transport,
)
from deaddit.llm.errors import TransientLLMError
from deaddit.models import EndpointCapability, LLMUsage

API_URL = "http://llm.test/v1"
MODEL = "test-model"

_USAGE = {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}


def _request(**overrides) -> ChatRequest:
    kwargs = {
        "system_prompt": "You are a helpful assistant.",
        "user_prompt": "Say something.",
        "model": MODEL,
        "api_url": API_URL,
        "api_key": "sk-test",
    }
    kwargs.update(overrides)
    return ChatRequest(**kwargs)


def _content_chunk(text: str) -> dict:
    return {"choices": [{"delta": {"content": text}}]}


def _reasoning_chunk(text: str) -> dict:
    return {"choices": [{"delta": {"reasoning_content": text}}]}


def _tool_call(arguments) -> list[dict]:
    """A valid OpenAI tool_calls envelope for the probe's echo tool."""
    return [
        {
            "id": "call_probe",
            "type": "function",
            "function": {"name": "echo_probe", "arguments": arguments},
        }
    ]


def _rows(db_session) -> list[LLMUsage]:
    return LLMUsage.query.order_by(LLMUsage.attempt).all()


def _seed_cap(
    db_session,
    *,
    supports_tools=True,
    supports_streaming=None,
    method="probe",
):
    db_session.add(
        EndpointCapability(
            api_url=API_URL,
            model_name=MODEL,
            supports_tools=supports_tools,
            supports_streaming=supports_streaming,
            probe_method=method,
        )
    )
    db_session.commit()


# ---------------------------------------------------------------------------
# event stream shape


def test_multi_chunk_ordering_yields_typed_events(app, db_session, fake_llm):
    _seed_cap(db_session, supports_tools=True, supports_streaming=True, method="manual")
    fake_llm.enqueue_stream(
        [
            _content_chunk("Hel"),
            _reasoning_chunk("pondering"),
            _content_chunk("lo"),
        ],
        usage=_USAGE,
    )
    events = list(LLMClient().stream(_request()))
    assert [(type(e).__name__, getattr(e, "text", None)) for e in events[:-1]] == [
        ("TokenDelta", "Hel"),
        ("ReasoningDelta", "pondering"),
        ("TokenDelta", "lo"),
    ]
    done = events[-1]
    assert isinstance(done, Done)
    assert done.synthesized is False
    assert done.result.content == "Hello"
    assert done.result.usage == _USAGE
    assert done.result.attempts == 1


def test_finish_reason_survives_real_stream(app, db_session, fake_llm):
    _seed_cap(db_session, supports_tools=True, supports_streaming=True, method="manual")
    fake_llm.enqueue_stream(
        [
            _content_chunk("Hel"),
            _content_chunk("lo"),
            {"choices": [{"delta": {}, "finish_reason": "length"}]},
        ]
    )
    events = list(LLMClient().stream(_request()))
    done = events[-1]
    assert isinstance(done, Done)
    assert done.synthesized is False
    assert done.result.finish_reason == "length"


def test_missing_finish_reason_normalizes_to_none_in_stream(app, db_session, fake_llm):
    _seed_cap(db_session, supports_tools=True, supports_streaming=True, method="manual")
    fake_llm.enqueue_stream([_content_chunk("Hello")])
    events = list(LLMClient().stream(_request()))
    done = events[-1]
    assert done.result.finish_reason is None


def test_reasoning_field_named_reasoning_is_normalized(app, db_session, fake_llm):
    _seed_cap(db_session, supports_tools=True, supports_streaming=True, method="manual")
    fake_llm.enqueue_stream([{"choices": [{"delta": {"reasoning": "hmm"}}]}])
    events = list(LLMClient().stream(_request()))
    assert isinstance(events[0], ReasoningDelta)
    assert events[0].text == "hmm"


def test_tool_call_delta_accumulation_lands_on_done_result(app, db_session, fake_llm):
    _seed_cap(db_session, supports_tools=True, supports_streaming=True, method="manual")
    fake_llm.enqueue_stream(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": "",
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '{"city": "Oslo"}'},
                                }
                            ]
                        }
                    }
                ]
            },
        ]
    )
    events = list(LLMClient().stream(_request()))
    tool_events = [e for e in events if isinstance(e, ToolCallDelta)]
    assert len(tool_events) == 2
    assert tool_events[0].name == "get_weather"
    assert tool_events[0].args_partial == ""
    assert tool_events[1].args_partial == '{"city": "Oslo"}'
    done = events[-1]
    assert done.result.tool_calls == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Oslo"}'},
        }
    ]


def test_stream_payload_sets_stream_true(app, db_session, fake_llm):
    fake_llm.enqueue_stream([_content_chunk("hi")])
    _seed_cap(db_session, supports_tools=True, supports_streaming=True, method="manual")
    list(LLMClient().stream(_request()))
    stream_calls = fake_llm.requests[0]
    assert stream_calls["payload"]["stream"] is True
    assert stream_calls["request_id"]


# ---------------------------------------------------------------------------
# ledger invariant: exactly one row per attempt


def test_ledger_one_ok_row_on_stream_success(app, db_session, fake_llm):
    fake_llm.enqueue_stream([_content_chunk("a"), _content_chunk("b")], usage=_USAGE)
    _seed_cap(db_session, supports_tools=True, supports_streaming=True, method="manual")
    list(LLMClient().stream(_request()))
    rows = _rows(db_session)
    assert len(rows) == 1
    assert rows[0].status == "ok"
    assert rows[0].total_tokens == 12
    assert rows[0].error_type is None


def test_estimated_cost_none_when_unpriced(app, db_session, fake_llm):
    fake_llm.enqueue_stream([_content_chunk("priceless")], usage=_USAGE)
    _seed_cap(db_session, supports_tools=True, supports_streaming=True, method="manual")
    list(LLMClient().stream(_request()))
    row = _rows(db_session)[0]
    # hosted endpoint, no ModelPrice rows -> NULL cost, never 0.0
    assert row.estimated_cost is None


def test_ledger_failed_row_on_stream_error(app, db_session, fake_llm):
    fake_llm.enqueue_stream_error(TransientLLMError("connection reset"))
    _seed_cap(db_session, supports_tools=True, supports_streaming=True, method="manual")
    with pytest.raises(TransientLLMError):
        list(LLMClient().stream(_request()))
    rows = _rows(db_session)
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].error_type == "TransientLLMError"
    assert rows[0].estimated_cost is None


def test_fallback_path_records_exactly_one_row(app, db_session, fake_llm):
    _seed_cap(db_session, supports_streaming=False)
    fake_llm.enqueue_content("full text")
    list(LLMClient().stream(_request()))
    rows = _rows(db_session)
    assert len(rows) == 1
    assert rows[0].status == "ok"


# ---------------------------------------------------------------------------
# fallback law


def test_synthesized_fallback_when_supports_streaming_false(app, db_session, fake_llm):
    _seed_cap(db_session, supports_streaming=False)
    fake_llm.enqueue_content("the whole answer at once")
    events = list(LLMClient().stream(_request()))
    assert [(type(e).__name__) for e in events] == ["TokenDelta", "Done"]
    assert events[0].text == "the whole answer at once"
    assert events[-1].synthesized is True
    assert events[-1].result.content == "the whole answer at once"


def test_null_capability_probes_then_streams_and_persists_true(
    app, db_session, fake_llm
):
    fake_llm.enqueue_tool_calls(_tool_call(json.dumps({"message": "ping"})))
    fake_llm.enqueue_stream([_content_chunk("tok")])  # consumed by the probe
    fake_llm.enqueue_stream([_content_chunk("real")])
    events = list(LLMClient().stream(_request()))
    assert isinstance(events[-1], Done) and events[-1].synthesized is False
    assert events[-1].result.content == "real"
    cap = capabilities.get_capability(API_URL, MODEL)
    assert cap.supports_streaming is True
    assert capabilities.LAST_STREAM_PROBE_EVIDENCE == {
        "chunk_count": 1,
        "finish_reason": None,
        "sample": "tok",
        "request_id": capabilities.LAST_STREAM_PROBE_EVIDENCE["request_id"],
    }
    assert capabilities.LAST_STREAM_PROBE_EVIDENCE["request_id"].startswith(
        "stream-probe-"
    )


def test_probe_verdict_false_persists_zero_and_falls_back(app, db_session, fake_llm):
    fake_llm.enqueue_tool_calls(_tool_call(json.dumps({"message": "ping"})))
    fake_llm.enqueue_stream([])  # connected but zero token deltas
    fake_llm.enqueue_content("non-streamed answer")
    events = list(LLMClient().stream(_request()))
    cap = capabilities.get_capability(API_URL, MODEL)
    assert cap.supports_streaming is False
    assert capabilities.LAST_STREAM_PROBE_EVIDENCE["chunk_count"] == 0
    assert [type(e).__name__ for e in events] == ["TokenDelta", "Done"]
    assert events[-1].synthesized is True


def test_probe_transient_failure_no_verdict_and_client_still_answers(
    app, db_session, fake_llm
):
    _seed_cap(db_session, supports_tools=True, supports_streaming=None)
    fake_llm.enqueue_stream_error(TransientLLMError("endpoint hiccup"))
    fake_llm.enqueue_content("graceful answer")
    # The UI must never hang or error on an unknown capability: the probe
    # fails transiently inside stream(), records nothing, and the client
    # falls back to complete().
    events = list(LLMClient().stream(_request()))
    assert capabilities.LAST_STREAM_PROBE_EVIDENCE is None
    cap = capabilities.get_capability(API_URL, MODEL)
    assert cap.supports_streaming is None  # no verdict recorded on failure
    assert [type(e).__name__ for e in events] == ["TokenDelta", "Done"]
    assert events[-1].synthesized is True
    assert events[-1].result.content == "graceful answer"


def test_manual_override_never_overwritten_by_stream_probe(app, db_session, fake_llm):
    _seed_cap(
        db_session, supports_tools=False, supports_streaming=False, method="manual"
    )
    before = len(fake_llm._queue)
    assert capabilities.probe_streaming(API_URL, MODEL) is False
    assert len(fake_llm._queue) == before
    cap = capabilities.get_capability(API_URL, MODEL)
    assert cap.supports_streaming is False
    assert cap.probe_method == "manual"


def test_manual_true_override_short_circuits_to_streaming(app, db_session, fake_llm):
    _seed_cap(db_session, supports_tools=True, supports_streaming=True, method="manual")
    fake_llm.enqueue_stream([_content_chunk("direct")])
    events = list(LLMClient().stream(_request()))
    assert events[-1].synthesized is False
    assert events[-1].result.content == "direct"


# ---------------------------------------------------------------------------
# observer contract


def test_observer_receives_every_event_and_exceptions_are_swallowed(
    app, db_session, fake_llm
):
    fake_llm.enqueue_stream([_content_chunk("x"), _content_chunk("y")], usage=_USAGE)
    _seed_cap(db_session, supports_tools=True, supports_streaming=True, method="manual")
    seen: list = []

    def flaky_observer(event):
        seen.append(event)
        if isinstance(event, Done):
            raise RuntimeError("observer blew up")

    events = list(LLMClient().stream(_request(), observer=flaky_observer))
    assert seen == events
    assert isinstance(seen[-1], Done)
    # Swallowed, counted, generation unaffected:
    assert len(events) == 3
    assert events[-1].result.content == "xy"


def test_observer_also_sees_synthesized_events(app, db_session, fake_llm):
    _seed_cap(db_session, supports_streaming=False)
    fake_llm.enqueue_content("one shot")
    seen: list = []
    list(LLMClient().stream(_request(), observer=seen.append))
    assert [type(e).__name__ for e in seen] == ["TokenDelta", "Done"]


# ---------------------------------------------------------------------------
# transport.stream_chat against a faked requests session


class _FakeStreamResponse:
    def __init__(self, lines, status_code=200, text=""):
        self._lines = lines
        self.status_code = status_code
        self.text = text

    def iter_lines(self):
        yield from self._lines


class _FakeStreamSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self._responses.pop(0)


def test_transport_stream_decodes_sse_skips_keepalives(monkeypatch):
    session = _FakeStreamSession(
        [
            _FakeStreamResponse(
                [
                    b'data: {"choices": [{"delta": {"content": "he"}}]}',
                    b": keep-alive",
                    b"",
                    b'data: {"choices": [{"delta": {"content": "y"}}],'
                    b' "usage": {"total_tokens": 9}}',
                    b"data: [DONE]",
                ]
            )
        ]
    )
    monkeypatch.setattr(transport, "get_session", lambda: session)
    notified = []
    chunks = list(
        transport.stream_chat(
            API_URL,
            {},
            "sk-test",
            "req-sse",
            on_attempt=lambda attempt, sid, outcome: notified.append(outcome),
        )
    )
    assert [c["choices"][0]["delta"]["content"] for c in chunks] == ["he", "y"]
    payload = session.calls[0]["json"]
    assert payload["stream"] is True
    assert session.calls[0]["headers"]["X-Request-Id"] == "req-sse-1"
    assert notified == [{"usage": {"total_tokens": 9}}]
    assert transport.last_attempts() == 1


def test_transport_midstream_failure_is_transient_and_never_retries(monkeypatch):
    class _BrokenResponse:
        status_code = 200
        text = ""

        def iter_lines(self):
            yield b'data: {"choices": [{"delta": {"content": "partial"}}]}'
            raise requests.exceptions.ChunkedEncodingError("peer closed connection")

    session = _FakeStreamSession([_BrokenResponse()])
    monkeypatch.setattr(transport, "get_session", lambda: session)
    notified = []
    with pytest.raises(TransientLLMError):
        list(
            transport.stream_chat(
                API_URL,
                {},
                None,
                "req-broken",
                on_attempt=lambda attempt, sid, outcome: notified.append(outcome),
            )
        )
    assert len(session.calls) == 1  # no retry after first byte
    assert isinstance(notified[0], BaseException)
    assert transport.last_attempts() == 1


def test_transport_connect_failure_retries_three_times(monkeypatch):
    session = _FakeStreamSession(
        [
            _FakeStreamResponse([], status_code=503),
            _FakeStreamResponse([], status_code=503),
            _FakeStreamResponse(
                [b'data: {"choices": [{"delta": {}}]}', b"data: [DONE]"]
            ),
        ]
    )
    monkeypatch.setattr(transport, "get_session", lambda: session)

    real_sleep = transport.time.sleep
    monkeypatch.setattr(
        transport.time, "sleep", lambda s: real_sleep(0)
    )  # full-jitter, but instant in tests
    outcomes = []
    chunks = list(
        transport.stream_chat(
            API_URL,
            {},
            None,
            "req-retry",
            on_attempt=lambda attempt, sid, outcome: outcomes.append(outcome),
        )
    )
    assert len(chunks) == 1
    assert len(session.calls) == 3
    assert sum(isinstance(o, Exception) for o in outcomes) == 2
    assert outcomes[-1] == {"usage": {}}
