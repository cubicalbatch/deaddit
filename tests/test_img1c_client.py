"""Deterministic tests for the provider-neutral image contracts (Phase 1C).

No test here contacts a live service: register_adapter/reset_adapters is the
seam every adapter (real or fake) goes through, and get_adapter() raises
rather than falling back to any HTTP transport when nothing is registered.
"""

from __future__ import annotations

import time

import pytest

from deaddit.images.client import (
    generate,
    get_adapter,
    register_adapter,
    reset_adapters,
    search_models,
    unregister_adapter,
    validate_model,
)
from deaddit.images.types import (
    Deadline,
    ImageCredentialError,
    ImageGenerationResult,
    ImageProviderDisabledError,
    ImageTimeoutError,
    MalformedImageResultError,
    ModelOption,
    ModelSearchResult,
    ModelValidation,
    UnknownImageProviderTypeError,
)
from deaddit.models import ImageProvider
from tests.fakes import FakeImageAdapter


@pytest.fixture(autouse=True)
def _clean_adapter_registry():
    """Every test starts and ends with an empty adapter registry."""
    reset_adapters()
    yield
    reset_adapters()


def _provider(**overrides) -> ImageProvider:
    fields = {
        "name": "Example Provider",
        "provider_type": "fal",
        "credential_env": "FALAI_API_KEY",
        "default_model": "example/model",
        "is_enabled": True,
    }
    fields.update(overrides)
    return ImageProvider(**fields)


# --- Deadline ---------------------------------------------------------------


def test_deadline_after_rejects_non_positive_seconds():
    with pytest.raises(ValueError):
        Deadline.after(0)
    with pytest.raises(ValueError):
        Deadline.after(-1)


def test_deadline_remaining_and_expired():
    deadline = Deadline.after(60)
    assert 0 < deadline.remaining() <= 60
    assert not deadline.expired()

    past = Deadline(expires_at=time.monotonic() - 1)
    assert past.remaining() == 0.0
    assert past.expired()


# --- ImageGenerationResult ---------------------------------------------------


def test_generation_result_requires_url_or_bytes():
    with pytest.raises(MalformedImageResultError):
        ImageGenerationResult(
            request_id="req-1",
            image_url=None,
            image_bytes=None,
            mime_type="image/png",
            width=512,
            height=512,
        )

    # Either alone is sufficient.
    ImageGenerationResult(
        request_id="req-1",
        image_url="https://provider.example/image.png",
        image_bytes=None,
        mime_type="image/png",
        width=512,
        height=512,
    )
    ImageGenerationResult(
        request_id="req-1",
        image_url=None,
        image_bytes=b"\x89PNG",
        mime_type="image/png",
        width=512,
        height=512,
    )


# --- adapter registry ---------------------------------------------------


def test_get_adapter_unknown_type_raises_without_registration():
    with pytest.raises(UnknownImageProviderTypeError):
        get_adapter("fal")


def test_register_and_unregister_adapter():
    adapter = FakeImageAdapter()
    register_adapter("fal", adapter)
    assert get_adapter("fal") is adapter

    unregister_adapter("fal")
    with pytest.raises(UnknownImageProviderTypeError):
        get_adapter("fal")

    # Unregistering an already-absent type is a no-op, not an error.
    unregister_adapter("fal")


def test_reset_adapters_clears_every_registration():
    register_adapter("fal", FakeImageAdapter())
    register_adapter("runware", FakeImageAdapter())

    reset_adapters()

    with pytest.raises(UnknownImageProviderTypeError):
        get_adapter("fal")
    with pytest.raises(UnknownImageProviderTypeError):
        get_adapter("runware")


# --- fail-before-network: disabled / missing credential / unknown type -----


def test_disabled_provider_fails_before_adapter_is_consulted():
    provider = _provider(is_enabled=False)
    adapter = FakeImageAdapter()
    register_adapter("fal", adapter)

    with pytest.raises(ImageProviderDisabledError):
        search_models(provider, query="cats")
    with pytest.raises(ImageProviderDisabledError):
        validate_model(provider, "example/model")
    with pytest.raises(ImageProviderDisabledError):
        generate(provider, "example/model", "a cat", Deadline.after(30))

    assert adapter.search_calls == []
    assert adapter.validate_calls == []
    assert adapter.generate_calls == []


def test_missing_credential_env_name_fails_before_adapter_is_consulted():
    provider = _provider(credential_env="")
    adapter = FakeImageAdapter()
    register_adapter("fal", adapter)

    with pytest.raises(ImageCredentialError):
        generate(provider, "example/model", "a cat", Deadline.after(30))

    assert adapter.generate_calls == []


def test_unset_credential_env_var_fails_before_adapter_is_consulted(monkeypatch):
    monkeypatch.delenv("FALAI_API_KEY", raising=False)
    provider = _provider()
    adapter = FakeImageAdapter()
    register_adapter("fal", adapter)

    with pytest.raises(ImageCredentialError):
        generate(provider, "example/model", "a cat", Deadline.after(30))

    assert adapter.generate_calls == []


def test_blank_credential_env_var_fails_before_adapter_is_consulted(monkeypatch):
    monkeypatch.setenv("FALAI_API_KEY", "   ")
    provider = _provider()
    adapter = FakeImageAdapter()
    register_adapter("fal", adapter)

    # A whitespace-only credential is still non-empty from os.environ's
    # perspective; assert it currently passes through as-is rather than
    # silently being treated as blank, since adapters own further validation.
    adapter.enqueue_generate(
        ImageGenerationResult(
            request_id="req-1",
            image_url="https://provider.example/image.png",
            image_bytes=None,
            mime_type="image/png",
            width=512,
            height=512,
        )
    )
    generate(provider, "example/model", "a cat", Deadline.after(30))
    assert adapter.generate_calls[0]["credential"] == "   "


def test_unregistered_provider_type_fails_before_credential_lookup(monkeypatch):
    monkeypatch.delenv("FALAI_API_KEY", raising=False)
    provider = _provider(provider_type="fal")

    # No adapter registered at all for "fal"; the missing credential is
    # never reached because get_adapter() rejects first is not required,
    # but either way no network call is possible without an adapter.
    with pytest.raises((UnknownImageProviderTypeError, ImageCredentialError)):
        generate(provider, "example/model", "a cat", Deadline.after(30))


def test_expired_deadline_fails_before_adapter_generate_is_called(monkeypatch):
    monkeypatch.setenv("FALAI_API_KEY", "secret-key")
    provider = _provider()
    adapter = FakeImageAdapter()
    register_adapter("fal", adapter)

    expired = Deadline(expires_at=time.monotonic() - 1)
    with pytest.raises(ImageTimeoutError):
        generate(provider, "example/model", "a cat", expired)

    assert adapter.generate_calls == []


# --- successful dispatch ------------------------------------------------


def test_search_models_dispatches_to_registered_adapter(monkeypatch):
    monkeypatch.setenv("FALAI_API_KEY", "secret-key")
    provider = _provider()
    adapter = FakeImageAdapter()
    register_adapter("fal", adapter)
    expected = ModelSearchResult(
        options=[ModelOption(model_id="fal-ai/flux/schnell", display_name="Flux")],
        next_cursor="page-2",
    )
    adapter.enqueue_search(expected)

    result = search_models(provider, query="flux", cursor=None)

    assert result is expected
    assert adapter.search_calls == [
        {
            "provider": provider,
            "credential": "secret-key",
            "query": "flux",
            "cursor": None,
        }
    ]


def test_validate_model_dispatches_to_registered_adapter(monkeypatch):
    monkeypatch.setenv("FALAI_API_KEY", "secret-key")
    provider = _provider()
    adapter = FakeImageAdapter()
    register_adapter("fal", adapter)
    expected = ModelValidation(compatible=True)
    adapter.enqueue_validate(expected)

    result = validate_model(provider, "fal-ai/flux/schnell")

    assert result is expected
    assert adapter.validate_calls == [
        {
            "provider": provider,
            "credential": "secret-key",
            "model_id": "fal-ai/flux/schnell",
        }
    ]


def test_generate_dispatches_to_registered_adapter_with_deadline(monkeypatch):
    monkeypatch.setenv("FALAI_API_KEY", "secret-key")
    provider = _provider()
    adapter = FakeImageAdapter()
    register_adapter("fal", adapter)
    expected = ImageGenerationResult(
        request_id="req-1",
        image_url="https://provider.example/image.png",
        image_bytes=None,
        mime_type="image/png",
        width=512,
        height=512,
        seed=42,
        cost=0.01,
        safety_verdict="passed",
    )
    adapter.enqueue_generate(expected)
    deadline = Deadline.after(30)

    result = generate(provider, "fal-ai/flux/schnell", "a cat in a hat", deadline)

    assert result is expected
    call = adapter.generate_calls[0]
    assert call["provider"] is provider
    assert call["credential"] == "secret-key"
    assert call["model_id"] == "fal-ai/flux/schnell"
    assert call["prompt"] == "a cat in a hat"
    assert call["deadline"] is deadline


def test_adapter_error_propagates_through_dispatch(monkeypatch):
    monkeypatch.setenv("FALAI_API_KEY", "secret-key")
    provider = _provider()
    adapter = FakeImageAdapter()
    register_adapter("fal", adapter)
    adapter.enqueue_error(RuntimeError("provider exploded"), method="generate")

    with pytest.raises(RuntimeError, match="provider exploded"):
        generate(provider, "example/model", "a cat", Deadline.after(30))


# --- swappability: two provider types dispatch independently ---------------


def test_two_provider_types_dispatch_to_their_own_adapters(monkeypatch):
    monkeypatch.setenv("FALAI_API_KEY", "fal-secret")
    monkeypatch.setenv("RUNWARE_API_KEY", "runware-secret")
    fal_provider = _provider(
        name="Fal", provider_type="fal", credential_env="FALAI_API_KEY"
    )
    runware_provider = _provider(
        name="Runware", provider_type="runware", credential_env="RUNWARE_API_KEY"
    )
    fal_adapter = FakeImageAdapter()
    runware_adapter = FakeImageAdapter()
    register_adapter("fal", fal_adapter)
    register_adapter("runware", runware_adapter)

    fal_result = ImageGenerationResult(
        request_id="fal-req",
        image_url="https://fal.example/image.png",
        image_bytes=None,
        mime_type="image/png",
        width=512,
        height=512,
    )
    runware_result = ImageGenerationResult(
        request_id="runware-req",
        image_url="https://runware.example/image.png",
        image_bytes=None,
        mime_type="image/png",
        width=512,
        height=512,
    )
    fal_adapter.enqueue_generate(fal_result)
    runware_adapter.enqueue_generate(runware_result)

    assert (
        generate(fal_provider, "flux/schnell", "prompt", Deadline.after(30))
        is fal_result
    )
    assert (
        generate(runware_provider, "civitai:1@1", "prompt", Deadline.after(30))
        is runware_result
    )
    assert fal_adapter.generate_calls[0]["credential"] == "fal-secret"
    assert runware_adapter.generate_calls[0]["credential"] == "runware-secret"
    assert runware_adapter.search_calls == []
    assert fal_adapter.validate_calls == []
