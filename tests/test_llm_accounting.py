"""Phase LLM-3 Slice Ledger: accounting ledger tests.

Covers the ``LLMUsage`` ledger wiring: one ok row per successful
complete(), failed-attempt rows, multi-attempt retries through the real
transport, local-endpoint vs unpriced/priced cost rules, longest-pattern
price matching, pre-flight CapabilityError recording nothing, and a
failing ledger never breaking generation.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from deaddit.llm import accounting, transport
from deaddit.llm.capabilities import EchoArgs, set_manual_override
from deaddit.llm.client import ChatRequest, LLMClient
from deaddit.llm.errors import CapabilityError, TransientLLMError
from deaddit.llm.tools import ToolSpec
from deaddit.models import LLMUsage, ModelPrice

HOSTED_URL = "http://llm.test/v1"
MODEL = "test-model"


def _req(**overrides) -> ChatRequest:
    kwargs = {
        "system_prompt": "sys",
        "user_prompt": "usr",
        "model": MODEL,
        "api_url": HOSTED_URL,
        "request_id": "req123",
    }
    kwargs.update(overrides)
    return ChatRequest(**kwargs)


def _rows(db_session) -> list[LLMUsage]:
    return LLMUsage.query.order_by(LLMUsage.attempt).all()


# ---------------------------------------------------------------------------
# successful + failed completes through FakeProvider (backfill path)


def test_successful_complete_records_ok_row_with_usage_and_cost(
    app, db_session, fake_llm
):
    db_session.add(
        ModelPrice(
            pattern=MODEL, prompt_price_per_1k=0.5, completion_price_per_1k=1.5
        )
    )
    db_session.commit()
    fake_llm.enqueue_content("hello")  # usage: 1 prompt / 1 completion / 2 total

    result = LLMClient().complete(_req(action="post", agent="alice"))

    assert result.content == "hello"
    rows = _rows(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "ok"
    assert row.request_id == "req123"
    assert row.api_url == HOSTED_URL
    assert row.model == MODEL
    assert row.action == "post"
    assert row.agent == "alice"
    assert row.prompt_tokens == 1
    assert row.completion_tokens == 1
    assert row.total_tokens == 2
    assert row.estimated_cost == pytest.approx(1 * 0.5 + 1 * 1.5)
    assert row.error_type is None


def test_failed_complete_backfills_single_failed_row(app, db_session, fake_llm):
    fake_llm.enqueue_error(TransientLLMError("connection timed out"))

    with pytest.raises(TransientLLMError):
        LLMClient().complete(_req())

    rows = _rows(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "failed"
    assert row.error_type == "TransientLLMError"
    assert row.attempt == 1
    assert row.request_id == "req123"
    assert row.prompt_tokens is None
    assert row.estimated_cost is None


def test_preflight_capability_error_records_nothing(app, db_session, fake_llm):
    set_manual_override(HOSTED_URL, MODEL, supports_tools=False)
    fake_llm.enqueue_content("never consumed")

    tool = ToolSpec(
        name="echo_probe",
        description="Echo the given message back.",
        parameters_model=EchoArgs,
    )

    with pytest.raises(CapabilityError):
        LLMClient().complete(_req(tools=[tool]))

    assert _rows(db_session) == []
    # The queued response proves the provider was never invoked.
    assert len(fake_llm.requests) == 0


# ---------------------------------------------------------------------------
# multi-attempt retry through the REAL transport (per-attempt callback path)


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


_OK_PAYLOAD = {
    "choices": [{"message": {"role": "assistant", "content": "recovered"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
}


def test_retry_500_500_200_records_fail_fail_ok_rows(
    app, db_session, monkeypatch
):
    session = _FakeSession(
        [
            _FakeResponse(500, text="server oops"),
            _FakeResponse(500, text="server oops"),
            _FakeResponse(200, payload=_OK_PAYLOAD),
        ]
    )
    monkeypatch.setattr(transport, "get_session", lambda: session)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    result = LLMClient().complete(_req())

    assert result.content == "recovered"
    # X-Request-Id scheme intact across the retry loop.
    assert [c["headers"]["X-Request-Id"] for c in session.calls] == [
        "req123-1",
        "req123-2",
        "req123-3",
    ]

    rows = _rows(db_session)
    assert [r.status for r in rows] == ["failed", "failed", "ok"]
    assert [r.attempt for r in rows] == [1, 2, 3]
    assert all(r.request_id == "req123" for r in rows)
    assert [r.error_type for r in rows[:2]] == ["LLMError", "LLMError"]
    ok_row = rows[2]
    assert ok_row.error_type is None
    assert ok_row.total_tokens == 7


# ---------------------------------------------------------------------------
# cost rules


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:8080/v1",
        "http://[::1]:11434/v1",
        "http://10.1.2.3:8000/v1",
        "http://192.168.10.4:8000/v1",
    ],
)
def test_local_endpoint_always_costs_zero(app, db_session, fake_llm, url):
    fake_llm.enqueue_content("local")

    LLMClient().complete(_req(api_url=url))

    row = _rows(db_session)[0]
    assert row.status == "ok"
    assert row.total_tokens == 2  # metered...
    assert row.estimated_cost == 0.0  # ...but free


def test_unpriced_hosted_model_has_null_cost_not_zero(app, db_session, fake_llm):
    fake_llm.enqueue_content("expensive?")

    LLMClient().complete(_req())

    row = _rows(db_session)[0]
    assert row.estimated_cost is None


def test_longest_pattern_wins_case_insensitively(app, db_session, fake_llm):
    db_session.add(
        ModelPrice(
            pattern="GPT-*", prompt_price_per_1k=0.01, completion_price_per_1k=0.02
        )
    )
    db_session.add(
        ModelPrice(
            pattern="gpt-4o-mini",
            prompt_price_per_1k=1.0,
            completion_price_per_1k=2.0,
        )
    )
    db_session.commit()
    # usage 1/1/2 from enqueue_content
    fake_llm.enqueue_content("a")
    LLMClient().complete(_req(model="gpt-4o-mini"))
    fake_llm.enqueue_content("b")
    LLMClient().complete(_req(model="gpt-3.5-turbo"))

    rows = _rows(db_session)
    assert rows[0].estimated_cost == pytest.approx(3.0)  # exact pattern wins
    assert rows[1].estimated_cost == pytest.approx(0.03)  # glob fallback


# ---------------------------------------------------------------------------
# ledger resilience


def test_ledger_db_failure_does_not_break_generation(
    app, db_session, fake_llm, monkeypatch
):
    class _FailingSession:
        def add(self, obj):
            pass

        def commit(self):
            raise RuntimeError("disk on fire")

        def rollback(self):
            pass

    monkeypatch.setattr(
        accounting, "db", SimpleNamespace(session=_FailingSession())
    )
    fake_llm.enqueue_content("still works")

    result = LLMClient().complete(_req())

    assert result.content == "still works"
    # Nothing was written through the REAL session either.
    assert _rows(db_session) == []


def test_estimate_cost_unit_rules(app, db_session):
    assert accounting.estimate_cost("http://127.0.0.1:1/v1", "m", None, None) == 0.0
    assert (
        accounting.estimate_cost("http://llm.test/v1", "unknown-model", 100, 50)
        is None
    )
    db_session.add(
        ModelPrice(
            pattern="m", prompt_price_per_1k=1.0, completion_price_per_1k=2.0
        )
    )
    db_session.commit()
    assert accounting.estimate_cost(
        "http://llm.test/v1", "m", 100, 50
    ) == pytest.approx(200.0)
