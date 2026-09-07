from __future__ import annotations

from types import SimpleNamespace

import pytest

from deaddit.images.types import Deadline
from deaddit.llm import transport
from deaddit.llm.errors import TransientLLMError


class _Session:
    def __init__(self, clock: list[float]):
        self.clock = clock
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append(kwargs)
        self.clock[0] += 0.2
        status = 503 if len(self.calls) < 3 else 200
        return SimpleNamespace(status_code=status, text="busy", json=lambda: {"choices": []})


def test_post_chat_recomputes_timeout_and_caps_retry_sleep(monkeypatch):
    clock = [100.0]
    session = _Session(clock)
    monkeypatch.setattr("deaddit.images.types.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(transport, "_session", session)
    monkeypatch.setattr(transport.random, "uniform", lambda low, high: 0.1)
    monkeypatch.setattr(transport.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    result = transport.post_chat(
        "https://llm.example/v1",
        {"model": "test"},
        None,
        "request",
        read_timeout=30.0,
        deadline=Deadline(expires_at=101.0),
    )

    assert result["choices"] == []
    assert [call["timeout"] for call in session.calls] == [
        (1.0, 1.0),
        pytest.approx((0.7, 0.7)),
        pytest.approx((0.4, 0.4)),
    ]


def test_post_chat_expired_deadline_fails_before_io(monkeypatch):
    clock = [100.0]
    session = _Session(clock)
    monkeypatch.setattr("deaddit.images.types.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(transport, "_session", session)

    with pytest.raises(TransientLLMError, match="deadline elapsed"):
        transport.post_chat(
            "https://llm.example/v1",
            {"model": "test"},
            None,
            "request",
            deadline=Deadline(expires_at=99.0),
        )

    assert session.calls == []


def test_post_chat_does_not_start_an_attempt_after_retry_budget_expires(monkeypatch):
    clock = [100.0]
    session = _Session(clock)
    monkeypatch.setattr("deaddit.images.types.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(transport, "_session", session)
    monkeypatch.setattr(transport.random, "uniform", lambda low, high: 0.1)
    monkeypatch.setattr(
        transport.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    with pytest.raises(TransientLLMError, match="deadline elapsed"):
        transport.post_chat(
            "https://llm.example/v1",
            {"model": "test"},
            None,
            "request",
            deadline=Deadline(expires_at=100.5),
        )

    assert len(session.calls) == 2
