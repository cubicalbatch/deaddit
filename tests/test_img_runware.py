"""Deterministic tests for the Runware adapter.

Every response is a hand-written fixture matching Runware's documented
task-array/getResponse/modelSearch shapes - no test contacts runware.ai, reads
RUNWARE_API_KEY, or spends money. HTTP is replaced end-to-end via
FakeHTTPTransport/FakeHTTPResponse. ``taskUUID`` generation is pinned to a fixed
value so fixtures can be written before the adapter runs, exactly like the real
submit/poll cycle reuses one taskUUID throughout.
"""

from __future__ import annotations

import time
import uuid as uuid_module

import pytest
import requests

import deaddit.images.providers.runware as runware_module
from deaddit.images.providers.runware import RunwareAdapter
from deaddit.images.types import (
    Deadline,
    ImageAuthError,
    ImageContentPolicyError,
    ImageProviderTransientError,
    ImageTimeoutError,
    ImageValidationError,
    MalformedImageResultError,
)
from deaddit.models import ImageProvider
from tests.fakes import FakeHTTPResponse, FakeHTTPTransport

_TASK_UUID = "39d7207a-87ef-4c93-8082-1431f9c1dc97"
MODEL = "civitai:102438@133677"
IMAGE_URL = "https://im.runware.ai/image/os/a14d18/ws/2/ii/b7db282d.jpg"


@pytest.fixture(autouse=True)
def _deterministic_task_uuid(monkeypatch):
    """Pin every minted taskUUID so fixtures can be written up front."""
    monkeypatch.setattr(
        runware_module.uuid, "uuid4", lambda: uuid_module.UUID(_TASK_UUID)
    )
    yield


def _provider(**overrides) -> ImageProvider:
    fields = {
        "name": "runware",
        "provider_type": "runware",
        "credential_env": "RUNWARE_API_KEY",
        "default_model": MODEL,
        "is_enabled": True,
    }
    fields.update(overrides)
    return ImageProvider(**fields)


def _adapter(transport, *, poll_interval: float = 0.0) -> RunwareAdapter:
    return RunwareAdapter(
        transport=transport, sleep=lambda _seconds: None, poll_interval=poll_interval
    )


def _ack_response(*, status: str | None = "processing") -> FakeHTTPResponse:
    entry = {"taskType": "imageInference", "taskUUID": _TASK_UUID}
    if status is not None:
        entry["status"] = status
    return FakeHTTPResponse(200, {"data": [entry]})


def _success_response(
    *, width=1024, height=1024, seed=42, cost=0.0013, nsfw=False
) -> FakeHTTPResponse:
    entry = {
        "taskType": "imageInference",
        "taskUUID": _TASK_UUID,
        "imageUUID": "b7db282d-2943-4f12-992f-77df3ad3ec71",
        "imageURL": IMAGE_URL,
        "seed": seed,
        "cost": cost,
    }
    if width is not None:
        entry["width"] = width
    if height is not None:
        entry["height"] = height
    if nsfw is not None:
        entry["NSFWContent"] = nsfw
    return FakeHTTPResponse(200, {"data": [entry]})


def _task_error(code: str, message: str = "failed") -> FakeHTTPResponse:
    return FakeHTTPResponse(
        200,
        {
            "errors": [
                {
                    "code": code,
                    "message": message,
                    "taskType": "imageInference",
                    "taskUUID": _TASK_UUID,
                }
            ]
        },
    )


def _http_error(status: int, *, code: str = "error", message: str = "failed"):
    return FakeHTTPResponse(status, {"errors": [{"code": code, "message": message}]})


def _search_response(*, results: list, total: int | None = None) -> FakeHTTPResponse:
    entry = {"taskType": "modelSearch", "taskUUID": _TASK_UUID, "results": results}
    if total is not None:
        entry["totalResults"] = total
    return FakeHTTPResponse(200, {"data": [entry]})


def _model_entry(air: str, *, name=None, category="checkpoint") -> dict:
    return {
        "air": air,
        "name": name or air,
        "category": category,
        "architecture": "flux1",
        "capabilities": ["text-to-image"],
        "source": "featured",
        "provider": "civitai",
        "shortDescription": "A model.",
        "heroImage": "https://im.runware.ai/hero.png",
        "private": False,
    }


def _generate(adapter, *, deadline: Deadline | None = None):
    return adapter.generate(
        _provider(),
        "secret-key",
        MODEL,
        "a cat in a hat",
        deadline or Deadline.after(30),
    )


def _expect(error, *responses):
    transport = FakeHTTPTransport()
    for response in responses:
        if isinstance(response, Exception):
            transport.enqueue_error(response)
        else:
            transport.enqueue(response)
    with pytest.raises(error):
        _generate(_adapter(transport))
    return transport


def test_generate_acknowledges_then_polls_one_task_uuid_to_success():
    transport = FakeHTTPTransport()
    transport.enqueue(_ack_response(status="processing"))
    transport.enqueue(_ack_response(status="queued"))
    transport.enqueue(_success_response())

    result = _generate(_adapter(transport))

    assert result.image_url == IMAGE_URL
    assert result.request_id == "b7db282d-2943-4f12-992f-77df3ad3ec71"
    assert result.image_bytes is None
    assert result.mime_type == "image/png"
    assert (result.width, result.height) == (1024, 1024)
    assert (result.seed, result.cost) == (42, 0.0013)
    assert result.safety_verdict == "passed"

    submit_call = transport.calls[0]
    assert submit_call["method"] == "POST"
    assert submit_call["url"] == "https://api.runware.ai/v1"
    assert submit_call["headers"]["Authorization"] == "Bearer secret-key"
    assert submit_call["json"][0] == {
        **submit_call["json"][0],
        "taskType": "imageInference",
        "taskUUID": _TASK_UUID,
        "positivePrompt": "a cat in a hat",
        "model": MODEL,
        "deliveryMethod": "async",
        "checkNSFWContent": True,
        "includeCost": True,
        "numberResults": 1,
    }
    # Every poll reuses the submission's taskUUID, so a resend can never be
    # mistaken for a second billable task.
    for call in transport.calls[1:]:
        assert call["json"] == [{"taskType": "getResponse", "taskUUID": _TASK_UUID}]
    assert len(transport.calls) == 3

    # An empty ack payload means "not ready yet", not "failed".
    pending = FakeHTTPTransport()
    pending.enqueue(FakeHTTPResponse(200, {"data": []}))
    pending.enqueue(_success_response())
    assert _generate(_adapter(pending)).image_url

    # Missing NSFW metadata is unknown, and missing dimensions fall back.
    unknown = FakeHTTPTransport()
    unknown.enqueue(_success_response(nsfw=None, width=None, height=None))
    result = _generate(_adapter(unknown))
    assert result.safety_verdict == "unknown"
    assert (result.width, result.height) == (1024, 1024)


def test_generate_maps_task_and_http_failures_to_typed_errors():
    # Safety, both as a flagged result and as a task- or HTTP-level rejection.
    _expect(ImageContentPolicyError, _success_response(nsfw=True))
    _expect(ImageContentPolicyError, _task_error("contentModerationFlagged"))
    _expect(ImageContentPolicyError, _http_error(400, code="nsfwPromptDetected"))

    # Credentials and quota are both terminal auth failures.
    for status, code in (
        (401, "invalidApiKey"),
        (402, "insufficientBalance"),
        (403, "forbidden"),
    ):
        _expect(ImageAuthError, _http_error(status, code=code))

    _expect(ImageValidationError, _http_error(400, code="invalidParameter"))
    _expect(ImageValidationError, _task_error("modelNotFound"))

    # Task-level capacity problems are transient, not validation failures.
    _expect(ImageProviderTransientError, _task_error("timeoutProvider"))
    _expect(ImageProviderTransientError, _task_error("providerRateLimitExceeded"))

    # Bodies that cannot yield an image are malformed.
    _expect(
        MalformedImageResultError,
        FakeHTTPResponse(
            200,
            {
                "data": [
                    {
                        "taskType": "imageInference",
                        "taskUUID": _TASK_UUID,
                        "seed": 1,
                        "status": "success",
                    }
                ]
            },
        ),
    )
    _expect(MalformedImageResultError, _ack_response(status="weird_state"))
    _expect(
        MalformedImageResultError,
        FakeHTTPResponse(200, malformed_json=True, text="not json"),
    )
    _expect(MalformedImageResultError, FakeHTTPResponse(200, [1, 2, 3]))


def test_generate_retries_infrastructure_failures_then_times_out(monkeypatch):
    for failure in (
        requests.ConnectionError("connection reset"),
        FakeHTTPResponse(503, {"errors": [{"code": "capacity"}]}),
    ):
        transport = FakeHTTPTransport()
        if isinstance(failure, FakeHTTPResponse):
            transport.enqueue(failure)
        else:
            transport.enqueue_error(failure)
        transport.enqueue(_success_response())
        assert _generate(_adapter(transport)).image_url

    # Retries are bounded at three attempts for transport, 429 and 5xx alike.
    for failure in (
        requests.ConnectionError("connection reset"),
        FakeHTTPResponse(429, {"errors": [{"code": "rateLimited"}]}),
        FakeHTTPResponse(503, {"errors": [{"code": "capacity"}]}),
    ):
        transport = _expect(ImageProviderTransientError, *([failure] * 3))
        assert len(transport.calls) == 3

    # A fake clock advanced only by the injected sleep() drives the deadline.
    # Runware has no documented cancel endpoint, so the adapter simply stops
    # polling and raises rather than issuing an extra HTTP call.
    clock = {"now": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    def fake_sleep(seconds: float) -> None:
        clock["now"] += seconds

    transport = FakeHTTPTransport()
    # A 5s deadline at a 1.5s interval allows exactly 4 polls before expiry.
    for _ in range(4):
        transport.enqueue(_ack_response(status="processing"))
    adapter = RunwareAdapter(transport=transport, sleep=fake_sleep, poll_interval=1.5)
    with pytest.raises(ImageTimeoutError):
        adapter.generate(_provider(), "secret-key", MODEL, "prompt", Deadline.after(5))
    assert len(transport.calls) == 4

    empty = FakeHTTPTransport()
    clock["now"] = 100.0
    with pytest.raises(ImageTimeoutError):
        RunwareAdapter(transport=empty, sleep=fake_sleep).generate(
            _provider(), "secret-key", MODEL, "prompt", Deadline(expires_at=1.0)
        )
    assert empty.calls == []


def test_catalog_search_and_validation_only_accept_checkpoint_airs():
    transport = FakeHTTPTransport()
    transport.enqueue(
        _search_response(
            total=40,
            results=[
                _model_entry(MODEL, name="Flux Base"),
                _model_entry("civitai:999@1", category="lora"),
                {"air": "malformed-not-an-air", "category": "checkpoint"},
                {"name": "no air field", "category": "checkpoint"},
                "not-a-dict",
            ],
        )
    )
    adapter = _adapter(transport)

    result = adapter.search_models(_provider(), "secret-key", "flux", None)

    assert [option.model_id for option in result.options] == [MODEL]
    option = result.options[0]
    assert option.display_name == "Flux Base"
    assert option.category == "checkpoint"
    assert option.metadata == {
        "architecture": "flux1",
        "capabilities": ["text-to-image"],
        "source": "featured",
        "provider": "civitai",
        "shortDescription": "A model.",
    }
    assert result.next_cursor == "20"
    task = transport.calls[0]["json"][0]
    assert (task["taskType"], task["search"], task["category"], task["offset"]) == (
        "modelSearch",
        "flux",
        "checkpoint",
        0,
    )
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer secret-key"

    # The cursor is an offset, and an exhausted page reports no next cursor.
    paged = FakeHTTPTransport()
    paged.enqueue(_search_response(total=20, results=[]))
    assert (
        _adapter(paged).search_models(_provider(), "secret-key", "", "20").next_cursor
        is None
    )
    assert paged.calls[0]["json"][0]["offset"] == 20

    for error, responses in (
        (ImageAuthError, [_http_error(401, code="invalidApiKey")]),
        (
            ImageProviderTransientError,
            [FakeHTTPResponse(500, {"errors": [{"code": "internal"}]})] * 3,
        ),
        (
            MalformedImageResultError,
            [
                FakeHTTPResponse(
                    200, {"data": [{"taskType": "modelSearch", "taskUUID": _TASK_UUID}]}
                )
            ],
        ),
        (MalformedImageResultError, [FakeHTTPResponse(200, {"data": []})]),
    ):
        failing = FakeHTTPTransport()
        for response in responses:
            failing.enqueue(response)
        with pytest.raises(error):
            _adapter(failing).search_models(_provider(), "secret-key", "", None)

    ok = FakeHTTPTransport()
    ok.enqueue(_search_response(total=1, results=[_model_entry(MODEL)]))
    assert (
        _adapter(ok).validate_model(_provider(), "secret-key", MODEL).compatible is True
    )
    assert ok.calls[0]["json"][0]["search"] == MODEL

    # A syntactically invalid AIR is rejected before any network request.
    offline = FakeHTTPTransport()
    assert (
        _adapter(offline)
        .validate_model(_provider(), "secret-key", "not-an-air-id")
        .compatible
        is False
    )
    assert offline.calls == []

    missing = FakeHTTPTransport()
    missing.enqueue(_search_response(total=0, results=[]))
    outcome = _adapter(missing).validate_model(_provider(), "secret-key", "civitai:1@1")
    assert outcome.compatible is False and outcome.reason

    wrong_category = FakeHTTPTransport()
    wrong_category.enqueue(
        _search_response(
            total=1, results=[_model_entry("civitai:1@1", category="lora")]
        )
    )
    outcome = _adapter(wrong_category).validate_model(
        _provider(), "secret-key", "civitai:1@1"
    )
    assert outcome.compatible is False
    assert "lora" in outcome.reason
