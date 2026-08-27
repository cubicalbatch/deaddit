"""Deterministic tests for the fal.ai adapter.

Every response is a hand-written fixture matching fal's documented queue and
catalog shapes - no test contacts fal.ai, reads FALAI_API_KEY, or spends money.
HTTP is replaced end-to-end via FakeHTTPTransport/FakeHTTPResponse.
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

MODEL = "fal-ai/flux/schnell"


def _provider(**overrides) -> ImageProvider:
    fields = {
        "name": "fal",
        "provider_type": "fal",
        "credential_env": "FALAI_API_KEY",
        "default_model": MODEL,
        "is_enabled": True,
    }
    fields.update(overrides)
    return ImageProvider(**fields)


def _adapter(transport: FakeHTTPTransport, *, poll_interval: float = 0.0) -> FalAdapter:
    return FalAdapter(
        transport=transport, sleep=lambda _seconds: None, poll_interval=poll_interval
    )


def _submit_response(request_id: str = "req-1") -> FakeHTTPResponse:
    base = f"https://queue.fal.run/fal-ai/flux/schnell/requests/{request_id}"
    return FakeHTTPResponse(
        202,
        {
            "request_id": request_id,
            "response_url": base,
            "status_url": f"{base}/status",
            "cancel_url": f"{base}/cancel",
            "queue_position": 0,
        },
    )


def _status_response(status: str, status_code: int = 200) -> FakeHTTPResponse:
    return FakeHTTPResponse(status_code, {"status": status, "request_id": "req-1"})


def _result_response(*, has_nsfw_concepts=None, **overrides) -> FakeHTTPResponse:
    image = {
        "url": "https://v3.fal.media/files/rabbit/abc123.png",
        "width": 1024,
        "height": 1024,
        "content_type": "image/png",
    }
    image.update(overrides)
    body = {"images": [image], "seed": 42}
    if has_nsfw_concepts is not None:
        body["has_nsfw_concepts"] = has_nsfw_concepts
    return FakeHTTPResponse(200, body)


def _generate(adapter, deadline_seconds: int = 30):
    return adapter.generate(
        _provider(),
        "secret-key",
        MODEL,
        "a cat in a hat",
        Deadline.after(deadline_seconds),
    )


def test_generate_polls_the_queue_to_completion_including_http_202_statuses():
    transport = FakeHTTPTransport()
    transport.enqueue(_submit_response())
    # fal answers the status endpoint with 202 while the request is still
    # queued or running, and only 200 once it has completed. Treating 202 as an
    # error broke every real generation that was not instantly done.
    transport.enqueue(_status_response("IN_QUEUE", status_code=202))
    transport.enqueue(_status_response("IN_PROGRESS", status_code=202))
    transport.enqueue(_status_response("COMPLETED"))
    transport.enqueue(_result_response(has_nsfw_concepts=[False]))

    result = _generate(_adapter(transport))

    assert result.request_id == "req-1"
    assert result.image_url == "https://v3.fal.media/files/rabbit/abc123.png"
    assert result.image_bytes is None
    assert result.mime_type == "image/png"
    assert (result.width, result.height) == (1024, 1024)
    assert result.seed == 42
    assert result.safety_verdict == "passed"

    submit_call = transport.calls[0]
    assert submit_call["method"] == "POST"
    assert submit_call["url"] == "https://queue.fal.run/fal-ai/flux/schnell"
    assert submit_call["headers"]["Authorization"] == "Key secret-key"
    assert submit_call["json"] == {"prompt": "a cat in a hat"}
    assert [call["method"] for call in transport.calls[1:]] == ["GET"] * 4

    # A result with no safety metadata at all is reported as unknown, not passed.
    plain = FakeHTTPTransport()
    plain.enqueue(_submit_response())
    plain.enqueue(_status_response("COMPLETED"))
    plain.enqueue(_result_response())
    assert _generate(_adapter(plain)).safety_verdict == "unknown"


def test_generate_maps_provider_failures_to_typed_errors():
    content_policy_422 = FakeHTTPResponse(
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
    generic_422 = FakeHTTPResponse(
        422,
        {
            "detail": [
                {"loc": ["body", "prompt"], "msg": "field required", "type": "missing"}
            ]
        },
    )

    cases = [
        # Authentication is rejected at submit, before any polling.
        (ImageAuthError, [FakeHTTPResponse(401, {"detail": "invalid key"})]),
        (
            ImageContentPolicyError,
            [_submit_response(), _status_response("COMPLETED"), content_policy_422],
        ),
        # A flagged result is a policy failure even though the call succeeded.
        (
            ImageContentPolicyError,
            [
                _submit_response(),
                _status_response("COMPLETED"),
                _result_response(has_nsfw_concepts=[True]),
            ],
        ),
        (
            ImageValidationError,
            [_submit_response(), _status_response("COMPLETED"), generic_422],
        ),
        # Result bodies that cannot yield an image are malformed, not transient.
        (
            MalformedImageResultError,
            [
                _submit_response(),
                _status_response("COMPLETED"),
                FakeHTTPResponse(200, {"seed": 1}),
            ],
        ),
        (
            MalformedImageResultError,
            [
                _submit_response(),
                _status_response("COMPLETED"),
                FakeHTTPResponse(200, {"images": []}),
            ],
        ),
        (
            MalformedImageResultError,
            [
                _submit_response(),
                _status_response("COMPLETED"),
                FakeHTTPResponse(200, {"images": [{"width": 512, "height": 512}]}),
            ],
        ),
        (
            MalformedImageResultError,
            [
                _submit_response(),
                _status_response("COMPLETED"),
                FakeHTTPResponse(200, malformed_json=True, text="not json"),
            ],
        ),
        # An unrecognized queue status must not be polled forever.
        (
            MalformedImageResultError,
            [_submit_response(), _status_response("WEIRD_STATE")],
        ),
    ]

    for error, responses in cases:
        transport = FakeHTTPTransport()
        for response in responses:
            transport.enqueue(response)
        with pytest.raises(error):
            _generate(_adapter(transport))


def test_generate_retries_infrastructure_failures_then_times_out_and_cancels(
    monkeypatch,
):
    # Transport errors and 5xx are retried, and a later attempt can still win.
    for failure in (
        requests.ConnectionError("connection reset"),
        FakeHTTPResponse(503, {"detail": "runner unavailable"}),
    ):
        transport = FakeHTTPTransport()
        if isinstance(failure, FakeHTTPResponse):
            transport.enqueue(failure)
        else:
            transport.enqueue_error(failure)
        transport.enqueue(_submit_response())
        transport.enqueue(_status_response("COMPLETED"))
        transport.enqueue(_result_response())
        assert _generate(_adapter(transport)).image_url

    # Retries are bounded: three attempts, then a typed transient error.
    for failure in (
        requests.ConnectionError("connection reset"),
        FakeHTTPResponse(503, {"detail": "runner unavailable"}),
    ):
        transport = FakeHTTPTransport()
        for _ in range(3):
            if isinstance(failure, FakeHTTPResponse):
                transport.enqueue(
                    FakeHTTPResponse(503, {"detail": "runner unavailable"})
                )
            else:
                transport.enqueue_error(failure)
        with pytest.raises(ImageProviderTransientError):
            _generate(_adapter(transport))
        assert len(transport.calls) == 3

    # A fake clock advanced only by the injected sleep() drives the deadline, so
    # the poll loop deterministically expires without any real delay.
    clock = {"now": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    def fake_sleep(seconds: float) -> None:
        clock["now"] += seconds

    def timing_out(final):
        transport = FakeHTTPTransport()
        transport.enqueue(_submit_response())
        # A 5s deadline at a 1.5s interval allows exactly 4 polls
        # (0 -> 1.5 -> 3.0 -> 4.5 -> 5.0) before the best-effort cancel.
        for _ in range(4):
            transport.enqueue(_status_response("IN_PROGRESS"))
        if isinstance(final, FakeHTTPResponse):
            transport.enqueue(final)
        else:
            transport.enqueue_error(final)
        adapter = FalAdapter(transport=transport, sleep=fake_sleep, poll_interval=1.5)
        with pytest.raises(ImageTimeoutError):
            adapter.generate(
                _provider(), "secret-key", MODEL, "prompt", Deadline.after(5)
            )
        return transport

    transport = timing_out(FakeHTTPResponse(202, {"status": "CANCELLATION_REQUESTED"}))
    cancel_calls = [call for call in transport.calls if call["method"] == "PUT"]
    assert len(cancel_calls) == 1
    assert cancel_calls[0]["url"] == (
        "https://queue.fal.run/fal-ai/flux/schnell/requests/req-1/cancel"
    )
    # A failing cancel is swallowed; the timeout is still what the caller sees.
    timing_out(requests.ConnectionError("cancel failed"))

    # An already-expired deadline never reaches the network at all.
    empty = FakeHTTPTransport()
    clock["now"] = 100.0
    with pytest.raises(ImageTimeoutError):
        FalAdapter(transport=empty, sleep=fake_sleep).generate(
            _provider(), "secret-key", MODEL, "prompt", Deadline(expires_at=1.0)
        )
    assert empty.calls == []


_COMPATIBLE = {
    "components": {
        "schemas": {
            "Input": {"properties": {"prompt": {"type": "string"}}},
            "Output": {"properties": {"images": {"type": "array"}}},
        }
    }
}
_NO_PROMPT = {
    "components": {
        "schemas": {
            "Input": {"properties": {"image_url": {"type": "string"}}},
            "Output": {"properties": {"images": {"type": "array"}}},
        }
    }
}
_NO_IMAGES = {
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
    openapi=_COMPATIBLE,
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


def test_catalog_search_and_validation_only_accept_usable_models():
    transport = FakeHTTPTransport()
    transport.enqueue(
        FakeHTTPResponse(
            200,
            {
                "models": [
                    _catalog_entry(MODEL, display_name="Flux Schnell"),
                    _catalog_entry("fal-ai/no-prompt", openapi=_NO_PROMPT),
                    _catalog_entry("fal-ai/no-images", openapi=_NO_IMAGES),
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

    assert [option.model_id for option in result.options] == [MODEL]
    option = result.options[0]
    assert option.display_name == "Flux Schnell"
    assert option.category == "text-to-image"
    # The raw schema is an implementation detail and never reaches callers.
    assert option.metadata == {
        "description": "A model.",
        "tags": ["fast"],
        "thumbnail_url": "https://fal.media/thumb.png",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    assert result.next_cursor == "page-2"
    params = transport.calls[0]["params"]
    assert params["category"] == "text-to-image"
    assert params["status"] == "active"
    assert params["expand"] == "openapi-3.0"
    assert params["q"] == "flux"
    assert transport.calls[0]["headers"]["Authorization"] == "Key secret-key"

    # Paging passes the cursor through and omits an empty query.
    paged = FakeHTTPTransport()
    paged.enqueue(FakeHTTPResponse(200, {"models": [], "next_cursor": None}))
    _adapter(paged).search_models(_provider(), "secret-key", "", "page-2")
    assert paged.calls[0]["params"]["cursor"] == "page-2"
    assert "q" not in paged.calls[0]["params"]

    # Catalog failures map to the same typed errors as generation.
    for error, responses in (
        (ImageAuthError, [FakeHTTPResponse(401, {"detail": "invalid key"})]),
        (
            ImageProviderTransientError,
            [FakeHTTPResponse(500, {"detail": "internal"})] * 3,
        ),
        (MalformedImageResultError, [FakeHTTPResponse(200, {"oops": []})]),
    ):
        failing = FakeHTTPTransport()
        for response in responses:
            failing.enqueue(response)
        with pytest.raises(error):
            _adapter(failing).search_models(_provider(), "secret-key", "", None)

    # validate_model answers with a reason instead of raising.
    ok = FakeHTTPTransport()
    ok.enqueue(FakeHTTPResponse(200, {"models": [_catalog_entry(MODEL)]}))
    validation = _adapter(ok).validate_model(_provider(), "secret-key", MODEL)
    assert validation.compatible is True
    assert ok.calls[0]["params"]["endpoint_id"] == MODEL

    for model_id, entries, expected_reason in (
        ("fal-ai/unknown", [], None),
        (
            "fal-ai/img2img",
            [_catalog_entry("fal-ai/img2img", openapi=_NO_PROMPT)],
            "prompt",
        ),
        (
            "fal-ai/text2video",
            [_catalog_entry("fal-ai/text2video", openapi=_NO_IMAGES)],
            "images",
        ),
        ("fal-ai/old", [_catalog_entry("fal-ai/old", status="deprecated")], None),
    ):
        failing = FakeHTTPTransport()
        failing.enqueue(FakeHTTPResponse(200, {"models": entries}))
        outcome = _adapter(failing).validate_model(_provider(), "secret-key", model_id)
        assert outcome.compatible is False
        assert outcome.reason
        if expected_reason:
            assert expected_reason in outcome.reason
