"""Deterministic tests for the Runware adapter (Phase 2B).

Every response is a hand-written fixture matching Runware's documented
task-array/getResponse/modelSearch shapes - no test contacts runware.ai,
reads RUNWARE_API_KEY, or spends money. HTTP is replaced end-to-end via
FakeHTTPTransport/FakeHTTPResponse. ``taskUUID`` generation is pinned to a
fixed value so fixtures can be written before the adapter runs, exactly
like the real submit/poll cycle reuses one taskUUID throughout.
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
        "default_model": "civitai:102438@133677",
        "is_enabled": True,
    }
    fields.update(overrides)
    return ImageProvider(**fields)


def _adapter(
    transport: FakeHTTPTransport, *, poll_interval: float = 0.0
) -> RunwareAdapter:
    return RunwareAdapter(
        transport=transport, sleep=lambda _seconds: None, poll_interval=poll_interval
    )


def _ack_response(*, status: str | None = "processing") -> FakeHTTPResponse:
    entry = {"taskType": "imageInference", "taskUUID": _TASK_UUID}
    if status is not None:
        entry["status"] = status
    return FakeHTTPResponse(200, {"data": [entry]})


def _empty_ack_response() -> FakeHTTPResponse:
    return FakeHTTPResponse(200, {"data": []})


def _success_response(
    *,
    image_uuid: str = "b7db282d-2943-4f12-992f-77df3ad3ec71",
    image_url: str = "https://im.runware.ai/image/os/a14d18/ws/2/ii/b7db282d.jpg",
    width: int | None = 1024,
    height: int | None = 1024,
    seed: int | None = 42,
    cost: float | None = 0.0013,
    nsfw: bool | None = False,
) -> FakeHTTPResponse:
    entry = {
        "taskType": "imageInference",
        "taskUUID": _TASK_UUID,
        "imageUUID": image_uuid,
        "imageURL": image_url,
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


def _task_error_response(*, code: str, message: str) -> FakeHTTPResponse:
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


def _http_error_response(status: int, *, code: str = "error", message: str = "failed"):
    return FakeHTTPResponse(status, {"errors": [{"code": code, "message": message}]})


def _search_response(*, results: list, total: int | None = None) -> FakeHTTPResponse:
    entry = {
        "taskType": "modelSearch",
        "taskUUID": _TASK_UUID,
        "results": results,
    }
    if total is not None:
        entry["totalResults"] = total
    return FakeHTTPResponse(200, {"data": [entry]})


def _generate(adapter: RunwareAdapter, *, deadline: Deadline | None = None):
    return adapter.generate(
        _provider(),
        "secret-key",
        "civitai:102438@133677",
        "a cat in a hat",
        deadline or Deadline.after(30),
    )


# --- generation: acknowledgement + polling ----------------------------------


def test_generate_polls_through_processing_states_to_success():
    transport = FakeHTTPTransport()
    transport.enqueue(_ack_response(status="processing"))
    transport.enqueue(_ack_response(status="queued"))
    transport.enqueue(_success_response())
    adapter = _adapter(transport)

    result = _generate(adapter)

    assert (
        result.image_url == "https://im.runware.ai/image/os/a14d18/ws/2/ii/b7db282d.jpg"
    )
    assert result.request_id == "b7db282d-2943-4f12-992f-77df3ad3ec71"
    assert result.image_bytes is None
    assert result.mime_type == "image/png"
    assert result.width == 1024
    assert result.height == 1024
    assert result.seed == 42
    assert result.cost == 0.0013
    assert result.safety_verdict == "passed"

    submit_call = transport.calls[0]
    assert submit_call["method"] == "POST"
    assert submit_call["url"] == "https://api.runware.ai/v1"
    assert submit_call["headers"]["Authorization"] == "Bearer secret-key"
    submitted_task = submit_call["json"][0]
    assert submitted_task["taskType"] == "imageInference"
    assert submitted_task["taskUUID"] == _TASK_UUID
    assert submitted_task["positivePrompt"] == "a cat in a hat"
    assert submitted_task["model"] == "civitai:102438@133677"
    assert submitted_task["deliveryMethod"] == "async"
    assert submitted_task["checkNSFWContent"] is True
    assert submitted_task["includeCost"] is True
    assert submitted_task["numberResults"] == 1

    # Every subsequent poll reuses the exact same taskUUID as the submission,
    # so a resend can never be mistaken for a second billable task.
    for call in transport.calls[1:]:
        assert call["json"] == [{"taskType": "getResponse", "taskUUID": _TASK_UUID}]
    assert len(transport.calls) == 3


def test_generate_treats_empty_ack_data_as_still_pending():
    transport = FakeHTTPTransport()
    transport.enqueue(_empty_ack_response())
    transport.enqueue(_success_response())
    adapter = _adapter(transport)

    result = _generate(adapter)
    assert result.image_url


def test_generate_reports_unknown_safety_verdict_when_nsfw_absent():
    transport = FakeHTTPTransport()
    transport.enqueue(_success_response(nsfw=None))
    adapter = _adapter(transport)

    result = _generate(adapter)
    assert result.safety_verdict == "unknown"


def test_generate_uses_default_dimensions_when_not_echoed_back():
    transport = FakeHTTPTransport()
    transport.enqueue(_success_response(width=None, height=None))
    adapter = _adapter(transport)

    result = _generate(adapter)
    assert result.width == 1024
    assert result.height == 1024


# --- safety rejection ---------------------------------------------------


def test_generate_raises_content_policy_error_when_result_flagged_nsfw():
    transport = FakeHTTPTransport()
    transport.enqueue(_success_response(nsfw=True))
    adapter = _adapter(transport)

    with pytest.raises(ImageContentPolicyError):
        _generate(adapter)


def test_generate_raises_content_policy_error_on_task_level_moderation_error():
    transport = FakeHTTPTransport()
    transport.enqueue(
        _task_error_response(
            code="contentModerationFlagged",
            message="The prompt was flagged by content moderation.",
        )
    )
    adapter = _adapter(transport)

    with pytest.raises(ImageContentPolicyError):
        _generate(adapter)


def test_generate_raises_content_policy_error_on_400_with_moderation_detail():
    transport = FakeHTTPTransport()
    transport.enqueue(
        _http_error_response(
            400, code="nsfwPromptDetected", message="prompt rejected on safety grounds"
        )
    )
    adapter = _adapter(transport)

    with pytest.raises(ImageContentPolicyError):
        _generate(adapter)


# --- quota / auth / validation errors ---------------------------------------


def test_generate_raises_auth_error_on_401():
    transport = FakeHTTPTransport()
    transport.enqueue(
        _http_error_response(401, code="invalidApiKey", message="bad key")
    )
    adapter = _adapter(transport)

    with pytest.raises(ImageAuthError):
        _generate(adapter)


def test_generate_raises_auth_error_on_402_quota_exhausted():
    transport = FakeHTTPTransport()
    transport.enqueue(
        _http_error_response(
            402, code="insufficientBalance", message="account balance too low"
        )
    )
    adapter = _adapter(transport)

    with pytest.raises(ImageAuthError):
        _generate(adapter)


def test_generate_raises_auth_error_on_403():
    transport = FakeHTTPTransport()
    transport.enqueue(
        _http_error_response(403, code="forbidden", message="not permitted")
    )
    adapter = _adapter(transport)

    with pytest.raises(ImageAuthError):
        _generate(adapter)


def test_generate_raises_validation_error_on_400():
    transport = FakeHTTPTransport()
    transport.enqueue(
        _http_error_response(
            400, code="invalidParameter", message="width must be a multiple of 64"
        )
    )
    adapter = _adapter(transport)

    with pytest.raises(ImageValidationError):
        _generate(adapter)


def test_generate_raises_validation_error_on_task_level_error():
    transport = FakeHTTPTransport()
    transport.enqueue(
        _task_error_response(code="modelNotFound", message="unknown model")
    )
    adapter = _adapter(transport)

    with pytest.raises(ImageValidationError):
        _generate(adapter)


# --- retryable 429 / 5xx / provider errors ----------------------------------


def test_generate_retries_transport_errors_then_succeeds():
    transport = FakeHTTPTransport()
    transport.enqueue_error(requests.ConnectionError("connection reset"))
    transport.enqueue(_success_response())
    adapter = _adapter(transport)

    result = _generate(adapter)
    assert result.image_url


def test_generate_raises_transient_error_after_exhausting_transport_retries():
    transport = FakeHTTPTransport()
    for _ in range(3):
        transport.enqueue_error(requests.ConnectionError("connection reset"))
    adapter = _adapter(transport)

    with pytest.raises(ImageProviderTransientError):
        _generate(adapter)
    assert len(transport.calls) == 3


def test_generate_raises_transient_error_after_exhausting_429_retries():
    transport = FakeHTTPTransport()
    for _ in range(3):
        transport.enqueue(FakeHTTPResponse(429, {"errors": [{"code": "rateLimited"}]}))
    adapter = _adapter(transport)

    with pytest.raises(ImageProviderTransientError):
        _generate(adapter)
    assert len(transport.calls) == 3


def test_generate_raises_transient_error_after_exhausting_5xx_retries():
    transport = FakeHTTPTransport()
    for _ in range(3):
        transport.enqueue(FakeHTTPResponse(503, {"errors": [{"code": "capacity"}]}))
    adapter = _adapter(transport)

    with pytest.raises(ImageProviderTransientError):
        _generate(adapter)
    assert len(transport.calls) == 3


def test_generate_retries_503_then_succeeds():
    transport = FakeHTTPTransport()
    transport.enqueue(FakeHTTPResponse(503, {"errors": [{"code": "capacity"}]}))
    transport.enqueue(_success_response())
    adapter = _adapter(transport)

    result = _generate(adapter)
    assert result.image_url


def test_generate_raises_transient_error_on_task_level_provider_timeout():
    transport = FakeHTTPTransport()
    transport.enqueue(
        _task_error_response(code="timeoutProvider", message="provider timed out")
    )
    adapter = _adapter(transport)

    with pytest.raises(ImageProviderTransientError):
        _generate(adapter)


def test_generate_raises_transient_error_on_provider_rate_limit_task_error():
    transport = FakeHTTPTransport()
    transport.enqueue(
        _task_error_response(
            code="providerRateLimitExceeded", message="provider is over capacity"
        )
    )
    adapter = _adapter(transport)

    with pytest.raises(ImageProviderTransientError):
        _generate(adapter)


# --- malformed output ---------------------------------------------------


def test_generate_raises_malformed_result_when_image_url_missing():
    transport = FakeHTTPTransport()
    transport.enqueue(
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
        )
    )
    adapter = _adapter(transport)

    with pytest.raises(MalformedImageResultError):
        _generate(adapter)


def test_generate_raises_malformed_result_on_unrecognized_status():
    transport = FakeHTTPTransport()
    transport.enqueue(_ack_response(status="weird_state"))
    adapter = _adapter(transport)

    with pytest.raises(MalformedImageResultError):
        _generate(adapter)


def test_generate_raises_malformed_result_on_invalid_json():
    transport = FakeHTTPTransport()
    transport.enqueue(FakeHTTPResponse(200, malformed_json=True, text="not json"))
    adapter = _adapter(transport)

    with pytest.raises(MalformedImageResultError):
        _generate(adapter)


def test_generate_raises_malformed_result_when_response_not_a_json_object():
    transport = FakeHTTPTransport()
    transport.enqueue(FakeHTTPResponse(200, [1, 2, 3]))
    adapter = _adapter(transport)

    with pytest.raises(MalformedImageResultError):
        _generate(adapter)


# --- timeout -------------------------------------------------------------


def test_generate_times_out_mid_poll(monkeypatch):
    """A fake clock advanced only by the injected sleep() drives the
    deadline, so the poll loop deterministically expires without any real
    delay. Runware has no documented cancel endpoint, so the adapter simply
    stops polling and raises rather than issuing an extra HTTP call."""

    clock = {"now": 0.0}

    def fake_monotonic() -> float:
        return clock["now"]

    def fake_sleep(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    transport = FakeHTTPTransport()
    # The 5-second deadline at a 1.5s poll interval allows exactly 4 polls
    # before expiry (0 -> 1.5 -> 3.0 -> 4.5 -> 5.0).
    for _ in range(4):
        transport.enqueue(_ack_response(status="processing"))
    adapter = RunwareAdapter(transport=transport, sleep=fake_sleep, poll_interval=1.5)
    deadline = Deadline.after(5)

    with pytest.raises(ImageTimeoutError):
        adapter.generate(
            _provider(), "secret-key", "civitai:102438@133677", "prompt", deadline
        )
    assert len(transport.calls) == 4


def test_generate_raises_timeout_immediately_when_submit_exceeds_deadline(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    def fake_sleep(seconds: float) -> None:
        clock["now"] += seconds

    transport = FakeHTTPTransport()
    adapter = RunwareAdapter(transport=transport, sleep=fake_sleep)
    clock["now"] = 100.0  # already past the deadline created before "now" advanced
    deadline = Deadline(expires_at=1.0)

    with pytest.raises(ImageTimeoutError):
        adapter.generate(
            _provider(), "secret-key", "civitai:102438@133677", "prompt", deadline
        )
    assert transport.calls == []


# --- catalog: search_models -------------------------------------------------


def _model_entry(
    air: str,
    *,
    name: str | None = None,
    category: str = "checkpoint",
    architecture: str = "flux1",
) -> dict:
    return {
        "air": air,
        "name": name or air,
        "category": category,
        "architecture": architecture,
        "capabilities": ["text-to-image"],
        "source": "featured",
        "provider": "civitai",
        "shortDescription": "A model.",
        "heroImage": "https://im.runware.ai/hero.png",
        "private": False,
    }


def test_search_models_filters_to_checkpoint_air_entries():
    transport = FakeHTTPTransport()
    transport.enqueue(
        _search_response(
            total=40,
            results=[
                _model_entry("civitai:102438@133677", name="Flux Base"),
                _model_entry("civitai:999@1", category="lora"),
                {"air": "malformed-not-an-air", "category": "checkpoint"},
                {"name": "no air field", "category": "checkpoint"},
                "not-a-dict",
            ],
        )
    )
    adapter = _adapter(transport)

    result = adapter.search_models(_provider(), "secret-key", "flux", None)

    assert [option.model_id for option in result.options] == ["civitai:102438@133677"]
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

    call = transport.calls[0]
    assert call["json"][0]["taskType"] == "modelSearch"
    assert call["json"][0]["search"] == "flux"
    assert call["json"][0]["category"] == "checkpoint"
    assert call["json"][0]["offset"] == 0
    assert call["headers"]["Authorization"] == "Bearer secret-key"


def test_search_models_passes_offset_cursor_and_omits_next_when_exhausted():
    transport = FakeHTTPTransport()
    transport.enqueue(_search_response(total=20, results=[]))
    adapter = _adapter(transport)

    result = adapter.search_models(_provider(), "secret-key", "", "20")

    assert transport.calls[0]["json"][0]["offset"] == 20
    assert result.next_cursor is None


def test_search_models_raises_auth_error_on_401():
    transport = FakeHTTPTransport()
    transport.enqueue(_http_error_response(401, code="invalidApiKey"))
    adapter = _adapter(transport)

    with pytest.raises(ImageAuthError):
        adapter.search_models(_provider(), "bad-key", "", None)


def test_search_models_raises_transient_error_after_exhausting_5xx_retries():
    transport = FakeHTTPTransport()
    for _ in range(3):
        transport.enqueue(FakeHTTPResponse(500, {"errors": [{"code": "internal"}]}))
    adapter = _adapter(transport)

    with pytest.raises(ImageProviderTransientError):
        adapter.search_models(_provider(), "secret-key", "", None)


def test_search_models_raises_malformed_result_when_results_key_missing():
    transport = FakeHTTPTransport()
    transport.enqueue(
        FakeHTTPResponse(
            200, {"data": [{"taskType": "modelSearch", "taskUUID": _TASK_UUID}]}
        )
    )
    adapter = _adapter(transport)

    with pytest.raises(MalformedImageResultError):
        adapter.search_models(_provider(), "secret-key", "", None)


def test_search_models_raises_malformed_result_when_data_missing():
    transport = FakeHTTPTransport()
    transport.enqueue(FakeHTTPResponse(200, {"data": []}))
    adapter = _adapter(transport)

    with pytest.raises(MalformedImageResultError):
        adapter.search_models(_provider(), "secret-key", "", None)


# --- catalog: validate_model -------------------------------------------------


def test_validate_model_returns_compatible_true():
    transport = FakeHTTPTransport()
    transport.enqueue(
        _search_response(total=1, results=[_model_entry("civitai:102438@133677")])
    )
    adapter = _adapter(transport)

    result = adapter.validate_model(_provider(), "secret-key", "civitai:102438@133677")
    assert result.compatible is True
    assert transport.calls[0]["json"][0]["search"] == "civitai:102438@133677"


def test_validate_model_returns_incompatible_when_malformed_air():
    transport = FakeHTTPTransport()
    adapter = _adapter(transport)

    result = adapter.validate_model(_provider(), "secret-key", "not-an-air-id")
    assert result.compatible is False
    assert transport.calls == []  # rejected before any network request


def test_validate_model_returns_incompatible_when_not_found():
    transport = FakeHTTPTransport()
    transport.enqueue(_search_response(total=0, results=[]))
    adapter = _adapter(transport)

    result = adapter.validate_model(_provider(), "secret-key", "civitai:1@1")
    assert result.compatible is False
    assert result.reason


def test_validate_model_returns_incompatible_when_wrong_category():
    transport = FakeHTTPTransport()
    transport.enqueue(
        _search_response(
            total=1, results=[_model_entry("civitai:1@1", category="lora")]
        )
    )
    adapter = _adapter(transport)

    result = adapter.validate_model(_provider(), "secret-key", "civitai:1@1")
    assert result.compatible is False
    assert "lora" in result.reason
