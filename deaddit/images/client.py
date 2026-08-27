"""Provider dispatch for image generation.

This module is THE seam for tests: register a fake adapter with
register_adapter() and every call below is served deterministically. No
adapter is registered by default, so calling search_models/validate_model/
generate against an unregistered provider_type fails closed rather than
reaching for a real HTTP transport.

Credentials are resolved from ``ImageProvider.credential_env`` at call time,
never persisted or cached. A disabled provider, an unregistered provider
type, or a missing credential all fail here, before any adapter method (and
therefore before any network request) runs.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from deaddit.images.types import (
    Deadline,
    ImageCredentialError,
    ImageGenerationResult,
    ImageProviderDisabledError,
    ImageTimeoutError,
    ModelSearchResult,
    ModelValidation,
    UnknownImageProviderTypeError,
)

if TYPE_CHECKING:
    from deaddit.models import ImageProvider


@runtime_checkable
class ImageAdapter(Protocol):
    """The contract every provider adapter (real or fake) implements.

    Every method receives the already-resolved *credential* string; adapters
    never read environment variables themselves.
    """

    def search_models(
        self,
        provider: ImageProvider,
        credential: str,
        query: str,
        cursor: str | None,
    ) -> ModelSearchResult: ...

    def validate_model(
        self,
        provider: ImageProvider,
        credential: str,
        model_id: str,
    ) -> ModelValidation: ...

    def generate(
        self,
        provider: ImageProvider,
        credential: str,
        model_id: str,
        prompt: str,
        deadline: Deadline,
    ) -> ImageGenerationResult: ...


_ADAPTERS: dict[str, ImageAdapter] = {}


def register_adapter(provider_type: str, adapter: ImageAdapter) -> None:
    """Register *adapter* to serve every provider with this ``provider_type``."""
    _ADAPTERS[provider_type] = adapter


def unregister_adapter(provider_type: str) -> None:
    """Remove any adapter registered for *provider_type*, if present."""
    _ADAPTERS.pop(provider_type, None)


def reset_adapters() -> None:
    """Unregister every adapter; production and tests call this to isolate runs."""
    _ADAPTERS.clear()


def get_adapter(provider_type: str) -> ImageAdapter:
    """Return the adapter registered for *provider_type*.

    Raises :class:`UnknownImageProviderTypeError` rather than falling back to
    any live transport.
    """
    try:
        return _ADAPTERS[provider_type]
    except KeyError:
        raise UnknownImageProviderTypeError(
            f"no image adapter registered for provider_type={provider_type!r}"
        ) from None


def _resolve_credential(provider: ImageProvider) -> str:
    """Resolve the provider's credential, failing before any network request."""
    if not provider.is_enabled:
        raise ImageProviderDisabledError(
            f"image provider {provider.name!r} is disabled"
        )
    env_name = provider.credential_env
    if not env_name:
        raise ImageCredentialError(
            f"image provider {provider.name!r} has no credential_env configured"
        )
    credential = os.environ.get(env_name)
    if not credential:
        raise ImageCredentialError(
            f"environment variable {env_name!r} for image provider "
            f"{provider.name!r} is not set"
        )
    return credential


def search_models(
    provider: ImageProvider,
    query: str = "",
    cursor: str | None = None,
) -> ModelSearchResult:
    """Search *provider*'s catalog, resolving its adapter and credential first."""
    credential = _resolve_credential(provider)
    adapter = get_adapter(provider.provider_type)
    return adapter.search_models(provider, credential, query, cursor)


def validate_model(provider: ImageProvider, model_id: str) -> ModelValidation:
    """Confirm *model_id* accepts a normalized text-to-image request."""
    credential = _resolve_credential(provider)
    adapter = get_adapter(provider.provider_type)
    return adapter.validate_model(provider, credential, model_id)


def generate(
    provider: ImageProvider,
    model_id: str,
    prompt: str,
    deadline: Deadline,
) -> ImageGenerationResult:
    """Generate one image, resolving the adapter and credential first.

    Raises :class:`ImageTimeoutError` immediately if *deadline* has already
    elapsed, without invoking the adapter.
    """
    credential = _resolve_credential(provider)
    adapter = get_adapter(provider.provider_type)
    if deadline.expired():
        raise ImageTimeoutError("deadline elapsed before generation started")
    return adapter.generate(provider, credential, model_id, prompt, deadline)


__all__ = [
    "ImageAdapter",
    "get_adapter",
    "generate",
    "register_adapter",
    "reset_adapters",
    "search_models",
    "unregister_adapter",
    "validate_model",
]
