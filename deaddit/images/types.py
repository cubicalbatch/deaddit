"""Provider-neutral contracts for image generation.

These dataclasses and errors are the normalized shape every image-provider
adapter speaks, so the agent tool and content service depend on this module
rather than on any single provider's request/response shapes. Nothing here
performs I/O.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


class ImageProviderError(Exception):
    """Base class for image-provider and dispatch failures."""


class ImageProviderDisabledError(ImageProviderError):
    """The provider is disabled and must not be contacted."""


class ImageCredentialError(ImageProviderError):
    """The provider's credential environment variable is unset or blank."""


class UnknownImageProviderTypeError(ImageProviderError):
    """No adapter is registered for the provider's ``provider_type``."""


class ImageValidationError(ImageProviderError):
    """The request was rejected as invalid (bad prompt, model, or parameters)."""


class ImageAuthError(ImageProviderError):
    """The provider rejected the resolved credential."""


class ImageContentPolicyError(ImageProviderError):
    """The provider refused the request or result on safety grounds."""


class ImageProviderTransientError(ImageProviderError):
    """A retryable infrastructure failure at the provider; retry budget spent."""


class ImageTimeoutError(ImageProviderError):
    """The call did not complete before its deadline."""


class MalformedImageResultError(ImageProviderError):
    """The provider returned a result that does not match the normalized contract."""


@dataclass(frozen=True)
class Deadline:
    """A monotonic point in time by which an adapter call must complete."""

    expires_at: float

    @classmethod
    def after(cls, seconds: float) -> Deadline:
        """Build a deadline *seconds* from now."""
        if seconds <= 0:
            raise ValueError("seconds must be positive")
        return cls(time.monotonic() + seconds)

    def remaining(self) -> float:
        """Seconds left, floored at zero."""
        return max(0.0, self.expires_at - time.monotonic())

    def expired(self) -> bool:
        return self.remaining() <= 0.0


@dataclass
class ModelOption:
    """One normalized, cache-worthy catalog entry returned by ``search_models``."""

    model_id: str
    display_name: str
    category: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelSearchResult:
    """A page of :class:`ModelOption` plus an opaque cursor for the next page."""

    options: list[ModelOption]
    next_cursor: str | None = None


@dataclass
class ModelValidation:
    """The verdict of ``validate_model`` for one provider model identifier."""

    compatible: bool
    reason: str | None = None


@dataclass
class ImageGenerationResult:
    """One normalized text-to-image result.

    ``image_url`` and/or ``image_bytes`` locate the generated image; callers
    download/decode through ``deaddit.images.storage`` rather than trusting
    reported dimensions or MIME type for anything but a hint.
    """

    request_id: str
    image_url: str | None
    image_bytes: bytes | None
    mime_type: str | None
    width: int | None
    height: int | None
    seed: int | None = None
    cost: float | None = None
    safety_verdict: str = "unknown"

    def __post_init__(self) -> None:
        if not self.image_url and not self.image_bytes:
            raise MalformedImageResultError(
                "image result has neither image_url nor image_bytes"
            )


__all__ = [
    "Deadline",
    "ImageAuthError",
    "ImageContentPolicyError",
    "ImageCredentialError",
    "ImageGenerationResult",
    "ImageProviderDisabledError",
    "ImageProviderError",
    "ImageProviderTransientError",
    "ImageTimeoutError",
    "ImageValidationError",
    "MalformedImageResultError",
    "ModelOption",
    "ModelSearchResult",
    "ModelValidation",
    "UnknownImageProviderTypeError",
]
