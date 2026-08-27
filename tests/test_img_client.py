"""The provider-neutral dispatch layer: routing, and failing closed.

No test here contacts a live service: register_adapter/reset_adapters is the
seam every adapter (real or fake) goes through, and get_adapter() raises rather
than falling back to any HTTP transport when nothing is registered.
"""

from __future__ import annotations

import time

import pytest

from deaddit.images.client import (
    credential_is_configured,
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


def _result(request_id="req-1", url="https://provider.example/image.png"):
    return ImageGenerationResult(
        request_id=request_id,
        image_url=url,
        image_bytes=None,
        mime_type="image/png",
        width=512,
        height=512,
    )


def test_stored_api_key_wins_and_blank_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("FALAI_API_KEY", "env-secret")
    adapter = FakeImageAdapter()
    register_adapter("fal", adapter)

    def search(provider):
        adapter.enqueue_search(ModelSearchResult(options=[], next_cursor=None))
        return search_models(provider, query="cats")

    # A stored key (even whitespace-padded) beats the environment variable.
    stored = _provider(api_key="  stored-secret  ")
    assert credential_is_configured(stored) is True
    search(stored)
    assert adapter.search_calls[-1]["credential"] == "stored-secret"

    # A blank stored key falls back to the environment variable.
    blank = _provider(api_key="   ")
    assert credential_is_configured(blank) is True
    search(blank)
    assert adapter.search_calls[-1]["credential"] == "env-secret"

    # Neither source available: fail closed before any adapter dispatch.
    neither = _provider(api_key=None)
    monkeypatch.delenv("FALAI_API_KEY", raising=False)
    assert credential_is_configured(neither) is False
    with pytest.raises(ImageCredentialError, match="admin UI"):
        search_models(neither, query="cats")


def test_dispatch_routes_to_each_adapter_and_otherwise_fails_closed(monkeypatch):
    monkeypatch.setenv("FALAI_API_KEY", "fal-secret")
    monkeypatch.setenv("RUNWARE_API_KEY", "runware-secret")

    # An unregistered type has no transport to fall back on.
    with pytest.raises(UnknownImageProviderTypeError):
        get_adapter("fal")

    fal_adapter, runware_adapter = FakeImageAdapter(), FakeImageAdapter()
    register_adapter("fal", fal_adapter)
    register_adapter("runware", runware_adapter)
    assert get_adapter("fal") is fal_adapter

    fal_provider = _provider()
    runware_provider = _provider(
        name="Runware", provider_type="runware", credential_env="RUNWARE_API_KEY"
    )

    search = ModelSearchResult(
        options=[ModelOption(model_id="fal-ai/flux/schnell", display_name="Flux")],
        next_cursor="page-2",
    )
    validation = ModelValidation(compatible=True)
    fal_result, runware_result = _result("fal-req"), _result("runware-req")
    fal_adapter.enqueue_search(search)
    fal_adapter.enqueue_validate(validation)
    fal_adapter.enqueue_generate(fal_result)
    runware_adapter.enqueue_generate(runware_result)

    deadline = Deadline.after(30)
    assert search_models(fal_provider, query="flux", cursor=None) is search
    assert validate_model(fal_provider, "fal-ai/flux/schnell") is validation
    assert (
        generate(fal_provider, "flux/schnell", "a cat in a hat", deadline) is fal_result
    )
    assert (
        generate(runware_provider, "civitai:1@1", "prompt", Deadline.after(30))
        is runware_result
    )

    # Each adapter sees its own credential, resolved from the provider's env name.
    assert fal_adapter.search_calls == [
        {
            "provider": fal_provider,
            "credential": "fal-secret",
            "query": "flux",
            "cursor": None,
        }
    ]
    assert fal_adapter.validate_calls[0]["model_id"] == "fal-ai/flux/schnell"
    fal_call = fal_adapter.generate_calls[0]
    assert (fal_call["prompt"], fal_call["deadline"]) == ("a cat in a hat", deadline)
    assert runware_adapter.generate_calls[0]["credential"] == "runware-secret"
    assert runware_adapter.search_calls == []

    # Adapter failures surface unchanged rather than being swallowed.
    fal_adapter.enqueue_error(RuntimeError("provider exploded"), method="generate")
    with pytest.raises(RuntimeError, match="provider exploded"):
        generate(fal_provider, "example/model", "a cat", Deadline.after(30))

    unregister_adapter("fal")
    unregister_adapter("fal")  # idempotent
    reset_adapters()
    with pytest.raises(UnknownImageProviderTypeError):
        get_adapter("runware")

    # Everything below is refused before any adapter is consulted at all.
    fal_adapter = FakeImageAdapter()
    register_adapter("fal", fal_adapter)

    def refuses(provider, deadline=None, error=None):
        with pytest.raises(error):
            generate(provider, "example/model", "a cat", deadline or Deadline.after(30))

    # A disabled provider is rejected on every entry point.
    disabled = _provider(is_enabled=False)
    for call in (
        lambda: search_models(disabled, query="cats"),
        lambda: validate_model(disabled, "example/model"),
        lambda: generate(disabled, "example/model", "a cat", Deadline.after(30)),
    ):
        with pytest.raises(ImageProviderDisabledError):
            call()

    refuses(_provider(credential_env=""), error=ImageCredentialError)

    monkeypatch.delenv("FALAI_API_KEY", raising=False)
    refuses(_provider(), error=ImageCredentialError)
    # No adapter registered at all: still impossible to reach a network call.
    reset_adapters()
    refuses(_provider(), error=(UnknownImageProviderTypeError, ImageCredentialError))

    register_adapter("fal", fal_adapter)
    monkeypatch.setenv("FALAI_API_KEY", "secret-key")
    refuses(
        _provider(),
        deadline=Deadline(expires_at=time.monotonic() - 1),
        error=ImageTimeoutError,
    )

    assert fal_adapter.search_calls == []
    assert fal_adapter.validate_calls == []
    assert fal_adapter.generate_calls == []

    # Deadlines and results validate their own inputs up front.
    for seconds in (0, -1):
        with pytest.raises(ValueError):
            Deadline.after(seconds)
    live = Deadline.after(60)
    assert 0 < live.remaining() <= 60 and not live.expired()
    past = Deadline(expires_at=time.monotonic() - 1)
    assert past.remaining() == 0.0 and past.expired()
    with pytest.raises(MalformedImageResultError):
        ImageGenerationResult(
            request_id="req-1",
            image_url=None,
            image_bytes=None,
            mime_type="image/png",
            width=512,
            height=512,
        )
    _result()
    ImageGenerationResult(
        request_id="req-1",
        image_url=None,
        image_bytes=b"\x89PNG",
        mime_type="image/png",
        width=512,
        height=512,
    )
