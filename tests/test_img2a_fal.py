"""Deterministic tests for the fal.ai adapter (Phase 2A).

Every response is a hand-written fixture matching fal's documented queue and
catalog shapes - no test contacts fal.ai, reads FALAI_API_KEY, or spends
money. HTTP is replaced end-to-end via FakeHTTPTransport/FakeHTTPResponse.
"""

from __future__ import annotations

import time

import pytest
import requests

from deaddit.images.providers.fal import FalAdapter
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


def _provider(**overrides) -> ImageProvider:
    fields = {
        "name": "fal",
        "provider_type": "fal",
        "credential_env": "FALAI_API_KEY",
        "default_model": "fal-ai/flux/schnell",
        "is_enabled": True,
    }
    fields.update(overrides)
    return ImageProvider(**fields)


def _adapter(transport: FakeHTTPTransport, *, poll_interval: float = 0.0) -> FalAdapter:
    return FalAdapter(
        transport=transport, sleep=lambda _seconds: None, poll_interval=poll_interval
    )


def _submit_response(request_id: str = "req-1") -> FakeHTTPResponse:
    return FakeHTTPResponse(
        202,
        {
            "request_id": request_id,
            "response_url": f"https://queue.fal.run/fal-ai/flux/schnell/requests/{request_id}",
            "status_url": f"https://queue.fal.run/fal-ai/flux/schnell/requests/{request_id}/status",
            "cancel_url": f"https://queue.fal.run/fal-ai/flux/schnell/requests/{request_id}/cancel",
            "queue_position": 0,
        },
    )


def _status_response(status: str, status_code: int = 200) -> FakeHTTPResponse:
    return FakeHTTPResponse(status_code, {"status": status, "request_id": "req-1"})


def _result_response(
    *,
    url: str = "https://v3.fal.media/files/rabbit/abc123.png",
    width: int = 1024,
    height: int = 1024,
    content_type: str = "image/png",
    seed: int | None = 42,
    has_nsfw_concepts: list[bool] | None = None,
) -> FakeHTTPResponse:
    body = {
        "images": [
            {"url": url, "width": width, "height": height, "content_type": content_type}
        ],
        "seed": seed,
    }
    if has_nsfw_concepts is not None:
        body["has_nsfw_concepts"] = has_nsfw_concepts
    return FakeHTTPResponse(200, body)


# --- generation: queued -> running -> completed -----------------------------


def test_generate_treats_http_202_status_poll_as_still_running():
    # fal answers the queue status endpoint with 202 while the request is still
    # queued or in progress, and only switches to 200 once it has completed.
    transport = FakeHTTPTransport()
    transport.enqueue(_submit_response())
    transport.enqueue(_status_response("IN_QUEUE", status_code=202))
    transport.enqueue(_status_response("IN_PROGRESS", status_code=202))
    transport.enqueue(_status_response("COMPLETED"))
    transport.enqueue(_result_response())
    adapter = _adapter(transport)

    result = adapter.generate(
        _provider(),
        "secret-key",
        "fal-ai/flux/schnell",
        "a cat in a hat",
        Deadline.after(30),
    )

    assert result.request_id == "req-1"
    assert result.image_url == "https://v3.fal.media/files/rabbit/abc123.png"
    assert len(transport.calls) == 5


def test_generate_polls_through_queue_states_to_completion():
    transport = FakeHTTPTransport()
    transport.enqueue(_submit_response())
    transport.enqueue(_status_response("IN_QUEUE"))
    transport.enqueue(_status_response("IN_PROGRESS"))
    transport.enqueue(_status_response("COMPLETED"))
    transport.enqueue(_result_response(has_nsfw_concepts=[False]))
    adapter = _adapter(transport)

    result = adapter.generate(
        _provider(),
        "secret-key",
        "fal-ai/flux/schnell",
        "a cat in a hat",
        Deadline.after(30),
    )

    assert result.request_id == "req-1"
    assert result.image_url == "https://v3.fal.media/files/rabbit/abc123.png"
    assert result.image_bytes is None
    assert result.mime_type == "image/png"
    assert result.width == 1024
    assert result.height == 1024
    assert result.seed == 42
    assert result.safety_verdict == "passed"

    submit_call = transport.calls[0]
    assert submit_call["method"] == "POST"
    assert submit_call["url"] == "https://queue.fal.run/fal-ai/flux/schnell"
    assert submit_call["headers"]["Authorization"] == "Key secret-key"
    assert submit_call["json"] == {"prompt": "a cat in a hat"}
    assert [call["method"] for call in transport.calls[1:4]] == ["GET", "GET", "GET"]
    assert transport.calls[4]["method"] == "GET"


def test_generate_reports_unknown_safety_verdict_when_absent():
    transport = FakeHTTPTransport()
    transport.enqueue(_submit_response())
    transport.enqueue(_status_response("COMPLETED"))
    transport.enqueue(_result_response())  # no has_nsfw_concepts key
    adapter = _adapter(transport)

    result = adapter.generate(
        _provider(), "secret-key", "fal-ai/flux/schnell", "prompt", Deadline.after(30)
    )
    assert result.safety_verdict == "unknown"


# --- content-policy: permanent -----------------------------------------------


def test_generate_raises_content_policy_error_on_422_detail():
    transport = FakeHTTPTransport()
    transport.enqueue(_submit_response())
    transport.enqueue(_status_response("COMPLETED"))
    transport.enqueue(
        FakeHTTPResponse(
            422,
            {
                "detail": [
                    {
                        "loc": ["body", "prompt"],
                        "msg": "The content could not be processed because it "
                        "contained material flagged by a content checker.",
                        "type": "content_policy_violation",
                        "url": "https://docs.fal.ai/errors#content_policy_violation",
                    }
                ]
            },
        )
    )
    adapter = _adapter(transport)

    with pytest.raises(ImageContentPolicyError):
        adapter.generate(
            _provider(),
            "secret-key",
            "fal-ai/flux/schnell",
            "prompt",
            Deadline.after(30),
        )


def test_generate_raises_content_policy_error_when_result_flagged_nsfw():
    transport = FakeHTTPTransport()
    transport.enqueue(_submit_response())
    transport.enqueue(_status_response("COMPLETED"))
    transport.enqueue(_result_response(has_nsfw_concepts=[True]))
    adapter = _adapter(transport)

    with pytest.raises(ImageContentPolicyError):
        adapter.generate(
            _provider(),
            "secret-key",
            "fal-ai/flux/schnell",
            "prompt",
            Deadline.after(30),
        )


def test_generate_raises_validation_error_on_generic_422():
    transport = FakeHTTPTransport()
    transport.enqueue(_submit_response())
    transport.enqueue(_status_response("COMPLETED"))
    transport.enqueue(
        FakeHTTPResponse(
            422,
            {
                "detail": [
                    {
                        "loc": ["body", "prompt"],
                        "msg": "field required",
                        "type": "missing",
                    }
                ]
            },
        )
    )
    adapter = _adapter(transport)

    with pytest.raises(ImageValidationError):
        adapter.generate(
            _provider(),
            "secret-key",
            "fal-ai/flux/schnell",
            "prompt",
            Deadline.after(30),
        )


def test_generate_raises_auth_error_on_401():
    transport = FakeHTTPTransport()
    transport.enqueue(FakeHTTPResponse(401, {"detail": "invalid key"}))
    adapter = _adapter(transport)

    with pytest.raises(ImageAuthError):
        adapter.generate(
            _provider(), "bad-key", "fal-ai/flux/schnell", "prompt", Deadline.after(30)
        )


# --- malformed output ---------------------------------------------------


def test_generate_raises_malformed_result_when_images_missing():
    transport = FakeHTTPTransport()
    transport.enqueue(_submit_response())
    transport.enqueue(_status_response("COMPLETED"))
    transport.enqueue(FakeHTTPResponse(200, {"seed": 1}))
    adapter = _adapter(transport)

    with pytest.raises(MalformedImageResultError):
        adapter.generate(
            _provider(),
            "secret-key",
            "fal-ai/flux/schnell",
            "prompt",
            Deadline.after(30),
        )


def test_generate_raises_malformed_result_when_images_empty():
    transport = FakeHTTPTransport()
    transport.enqueue(_submit_response())
    transport.enqueue(_status_response("COMPLETED"))
    transport.enqueue(FakeHTTPResponse(200, {"images": []}))
    adapter = _adapter(transport)

    with pytest.raises(MalformedImageResultError):
        adapter.generate(
            _provider(),
            "secret-key",
            "fal-ai/flux/schnell",
            "prompt",
            Deadline.after(30),
        )


def test_generate_raises_malformed_result_when_image_missing_url():
    transport = FakeHTTPTransport()
    transport.enqueue(_submit_response())
    transport.enqueue(_status_response("COMPLETED"))
    transport.enqueue(
        FakeHTTPResponse(200, {"images": [{"width": 512, "height": 512}]})
    )
    adapter = _adapter(transport)

    with pytest.raises(MalformedImageResultError):
        adapter.generate(
            _provider(),
            "secret-key",
            "fal-ai/flux/schnell",
            "prompt",
            Deadline.after(30),
        )


def test_generate_raises_malformed_result_on_invalid_json():
    transport = FakeHTTPTransport()
    transport.enqueue(_submit_response())
    transport.enqueue(_status_response("COMPLETED"))
    transport.enqueue(FakeHTTPResponse(200, malformed_json=True, text="not json"))
    adapter = _adapter(transport)

    with pytest.raises(MalformedImageResultError):
        adapter.generate(
            _provider(),
            "secret-key",
            "fal-ai/flux/schnell",
            "prompt",
            Deadline.after(30),
        )


def test_generate_raises_malformed_result_on_unrecognized_status():
    transport = FakeHTTPTransport()
    transport.enqueue(_submit_response())
    transport.enqueue(_status_response("WEIRD_STATE"))
    adapter = _adapter(transport)

    with pytest.raises(MalformedImageResultError):
        adapter.generate(
            _provider(),
            "secret-key",
            "fal-ai/flux/schnell",
            "prompt",
            Deadline.after(30),
        )


# --- transient / infrastructure failures ------------------------------------


def test_generate_retries_transport_errors_then_succeeds():
    transport = FakeHTTPTransport()
    transport.enqueue_error(requests.ConnectionError("connection reset"))
    transport.enqueue(_submit_response())
    transport.enqueue(_status_response("COMPLETED"))
    transport.enqueue(_result_response())
    adapter = _adapter(transport)

    result = adapter.generate(
        _provider(), "secret-key", "fal-ai/flux/schnell", "prompt", Deadline.after(30)
    )
    assert result.image_url


def test_generate_raises_transient_error_after_exhausting_transport_retries():
    transport = FakeHTTPTransport()
    for _ in range(3):
        transport.enqueue_error(requests.ConnectionError("connection reset"))
    adapter = _adapter(transport)

    with pytest.raises(ImageProviderTransientError):
        adapter.generate(
            _provider(),
            "secret-key",
            "fal-ai/flux/schnell",
            "prompt",
            Deadline.after(30),
        )
    assert len(transport.calls) == 3


def test_generate_raises_transient_error_after_exhausting_5xx_retries():
    transport = FakeHTTPTransport()
    for _ in range(3):
        transport.enqueue(FakeHTTPResponse(503, {"detail": "runner unavailable"}))
    adapter = _adapter(transport)

    with pytest.raises(ImageProviderTransientError):
        adapter.generate(
            _provider(),
            "secret-key",
            "fal-ai/flux/schnell",
            "prompt",
            Deadline.after(30),
        )
    assert len(transport.calls) == 3


def test_generate_retries_503_then_succeeds():
    transport = FakeHTTPTransport()
    transport.enqueue(FakeHTTPResponse(503, {"detail": "runner unavailable"}))
    transport.enqueue(_submit_response())
    transport.enqueue(_status_response("COMPLETED"))
    transport.enqueue(_result_response())
    adapter = _adapter(transport)

    result = adapter.generate(
        _provider(), "secret-key", "fal-ai/flux/schnell", "prompt", Deadline.after(30)
    )
    assert result.image_url


# --- timeout / cancellation --------------------------------------------------


def test_generate_times_out_mid_poll_and_cancels_best_effort(monkeypatch):
    """A fake clock advanced only by the injected sleep() drives the deadline,
    so the poll loop deterministically expires without any real delay."""

    clock = {"now": 0.0}

    def fake_monotonic() -> float:
        return clock["now"]

    def fake_sleep(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    transport = FakeHTTPTransport()
    transport.enqueue(_submit_response())
    # The 5-second deadline at a 1.5s poll interval allows exactly 4 polls
    # before expiry (0 -> 1.5 -> 3.0 -> 4.5 -> 5.0), then the best-effort
    # cancel call.
    for _ in range(4):
        transport.enqueue(_status_response("IN_PROGRESS"))
    transport.enqueue(FakeHTTPResponse(202, {"status": "CANCELLATION_REQUESTED"}))

    adapter = FalAdapter(transport=transport, sleep=fake_sleep, poll_interval=1.5)
    deadline = Deadline.after(5)

    with pytest.raises(ImageTimeoutError):
        adapter.generate(
            _provider(), "secret-key", "fal-ai/flux/schnell", "prompt", deadline
        )

    cancel_calls = [call for call in transport.calls if call["method"] == "PUT"]
    assert len(cancel_calls) == 1
    assert cancel_calls[0]["url"] == (
        "https://queue.fal.run/fal-ai/flux/schnell/requests/req-1/cancel"
    )


def test_generate_swallows_cancel_failure_and_still_raises_timeout(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    def fake_sleep(seconds: float) -> None:
        clock["now"] += seconds

    transport = FakeHTTPTransport()
    transport.enqueue(_submit_response())
    for _ in range(4):
        transport.enqueue(_status_response("IN_PROGRESS"))
    transport.enqueue_error(requests.ConnectionError("cancel failed"))

    adapter = FalAdapter(transport=transport, sleep=fake_sleep, poll_interval=1.5)
    deadline = Deadline.after(5)

    with pytest.raises(ImageTimeoutError):
        adapter.generate(
            _provider(), "secret-key", "fal-ai/flux/schnell", "prompt", deadline
        )


def test_generate_raises_timeout_immediately_when_submit_exceeds_deadline(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    def fake_sleep(seconds: float) -> None:
        clock["now"] += seconds

    transport = FakeHTTPTransport()
    adapter = FalAdapter(transport=transport, sleep=fake_sleep)
    clock["now"] = 100.0  # already past the deadline created before "now" advanced
    deadline = Deadline(expires_at=1.0)

    with pytest.raises(ImageTimeoutError):
        adapter.generate(
            _provider(), "secret-key", "fal-ai/flux/schnell", "prompt", deadline
        )
    assert transport.calls == []


# --- catalog: search_models -------------------------------------------------


_COMPATIBLE_OPENAPI = {
    "components": {
        "schemas": {
            "Input": {"properties": {"prompt": {"type": "string"}}},
            "Output": {"properties": {"images": {"type": "array"}}},
        }
    }
}

_NO_PROMPT_OPENAPI = {
    "components": {
        "schemas": {
            "Input": {"properties": {"image_url": {"type": "string"}}},
            "Output": {"properties": {"images": {"type": "array"}}},
        }
    }
}

_NO_IMAGES_OPENAPI = {
    "components": {
        "schemas": {
            "Input": {"properties": {"prompt": {"type": "string"}}},
            "Output": {"properties": {"video": {"type": "string"}}},
        }
    }
}


def _catalog_entry(
    endpoint_id: str,
    *,
    status: str = "active",
    category: str = "text-to-image",
    openapi=_COMPATIBLE_OPENAPI,
    display_name: str | None = None,
) -> dict:
    entry = {
        "endpoint_id": endpoint_id,
        "metadata": {
            "display_name": display_name or endpoint_id,
            "category": category,
            "status": status,
            "description": "A model.",
            "tags": ["fast"],
            "thumbnail_url": "https://fal.media/thumb.png",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    }
    if openapi is not None:
        entry["openapi"] = openapi
    return entry


def test_search_models_filters_to_compatible_active_text_to_image_endpoints():
    transport = FakeHTTPTransport()
    transport.enqueue(
        FakeHTTPResponse(
            200,
            {
                "models": [
                    _catalog_entry("fal-ai/flux/schnell", display_name="Flux Schnell"),
                    _catalog_entry("fal-ai/no-prompt", openapi=_NO_PROMPT_OPENAPI),
                    _catalog_entry("fal-ai/no-images", openapi=_NO_IMAGES_OPENAPI),
                    _catalog_entry("fal-ai/deprecated", status="deprecated"),
                    _catalog_entry("fal-ai/video", category="text-to-video"),
                    _catalog_entry("fal-ai/no-schema", openapi=None),
                    {"endpoint_id": "fal-ai/not-a-dict-metadata", "metadata": "oops"},
                ],
                "next_cursor": "page-2",
            },
        )
    )
    adapter = _adapter(transport)

    result = adapter.search_models(_provider(), "secret-key", "flux", None)

    assert [option.model_id for option in result.options] == ["fal-ai/flux/schnell"]
    option = result.options[0]
    assert option.display_name == "Flux Schnell"
    assert option.category == "text-to-image"
    assert "openapi" not in option.metadata
    assert option.metadata == {
        "description": "A model.",
        "tags": ["fast"],
        "thumbnail_url": "https://fal.media/thumb.png",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    assert result.next_cursor == "page-2"

    call = transport.calls[0]
    assert call["params"]["category"] == "text-to-image"
    assert call["params"]["status"] == "active"
    assert call["params"]["expand"] == "openapi-3.0"
    assert call["params"]["q"] == "flux"
    assert call["headers"]["Authorization"] == "Key secret-key"


def test_search_models_passes_cursor_when_given():
    transport = FakeHTTPTransport()
    transport.enqueue(FakeHTTPResponse(200, {"models": [], "next_cursor": None}))
    adapter = _adapter(transport)

    adapter.search_models(_provider(), "secret-key", "", "page-2")

    assert transport.calls[0]["params"]["cursor"] == "page-2"
    assert "q" not in transport.calls[0]["params"]


def test_search_models_raises_auth_error_on_401():
    transport = FakeHTTPTransport()
    transport.enqueue(FakeHTTPResponse(401, {"detail": "invalid key"}))
    adapter = _adapter(transport)

    with pytest.raises(ImageAuthError):
        adapter.search_models(_provider(), "bad-key", "", None)


def test_search_models_raises_transient_error_after_exhausting_5xx_retries():
    transport = FakeHTTPTransport()
    for _ in range(3):
        transport.enqueue(FakeHTTPResponse(500, {"detail": "internal error"}))
    adapter = _adapter(transport)

    with pytest.raises(ImageProviderTransientError):
        adapter.search_models(_provider(), "secret-key", "", None)


def test_search_models_raises_malformed_result_when_models_key_missing():
    transport = FakeHTTPTransport()
    transport.enqueue(FakeHTTPResponse(200, {"oops": []}))
    adapter = _adapter(transport)

    with pytest.raises(MalformedImageResultError):
        adapter.search_models(_provider(), "secret-key", "", None)


# --- catalog: validate_model -------------------------------------------------


def test_validate_model_returns_compatible_true():
    transport = FakeHTTPTransport()
    transport.enqueue(
        FakeHTTPResponse(200, {"models": [_catalog_entry("fal-ai/flux/schnell")]})
    )
    adapter = _adapter(transport)

    result = adapter.validate_model(_provider(), "secret-key", "fal-ai/flux/schnell")

    assert result.compatible is True
    assert transport.calls[0]["params"]["endpoint_id"] == "fal-ai/flux/schnell"


def test_validate_model_returns_incompatible_when_not_found():
    transport = FakeHTTPTransport()
    transport.enqueue(FakeHTTPResponse(200, {"models": []}))
    adapter = _adapter(transport)

    result = adapter.validate_model(_provider(), "secret-key", "fal-ai/unknown")

    assert result.compatible is False
    assert result.reason


def test_validate_model_returns_incompatible_when_missing_prompt_input():
    transport = FakeHTTPTransport()
    transport.enqueue(
        FakeHTTPResponse(
            200,
            {"models": [_catalog_entry("fal-ai/img2img", openapi=_NO_PROMPT_OPENAPI)]},
        )
    )
    adapter = _adapter(transport)

    result = adapter.validate_model(_provider(), "secret-key", "fal-ai/img2img")

    assert result.compatible is False
    assert "prompt" in result.reason


def test_validate_model_returns_incompatible_when_missing_images_output():
    transport = FakeHTTPTransport()
    transport.enqueue(
        FakeHTTPResponse(
            200,
            {
                "models": [
                    _catalog_entry("fal-ai/text2video", openapi=_NO_IMAGES_OPENAPI)
                ]
            },
        )
    )
    adapter = _adapter(transport)

    result = adapter.validate_model(_provider(), "secret-key", "fal-ai/text2video")

    assert result.compatible is False
    assert "images" in result.reason


def test_validate_model_returns_incompatible_when_deprecated():
    transport = FakeHTTPTransport()
    transport.enqueue(
        FakeHTTPResponse(
            200, {"models": [_catalog_entry("fal-ai/old", status="deprecated")]}
        )
    )
    adapter = _adapter(transport)

    result = adapter.validate_model(_provider(), "secret-key", "fal-ai/old")

    assert result.compatible is False
