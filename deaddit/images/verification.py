"""Provider connection verification: authenticated catalog search, never generation.

test_connection() is the seam a future admin "Test connection" action calls:
it proves a provider row's credential and adapter wiring work by resolving
the credential and running one catalog search through the normal
deaddit.images.client dispatch path. It never calls generate(), so invoking
it never risks a paid provider call - only search_models() is exercised.

Every dispatch failure (disabled provider, missing/blank credential, no
adapter registered for the provider_type, rejected credential, malformed or
transient provider response) is caught here and reported as a failed
ConnectionTestResult rather than raised, so a caller (CLI or future admin
route) never needs its own try/except around this call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from deaddit.images.client import search_models
from deaddit.images.types import ImageProviderError

if TYPE_CHECKING:
    from deaddit.models import ImageProvider

_SAMPLE_SIZE = 5


@dataclass
class ConnectionTestResult:
    """The outcome of one authenticated catalog probe against a provider."""

    ok: bool
    message: str
    sample_model_ids: list[str] = field(default_factory=list)


def test_connection(
    provider: ImageProvider, *, query: str = ""
) -> ConnectionTestResult:
    """Confirm *provider* can authenticate and list its model catalog.

    Performs exactly one search_models() call - no generation, no payment -
    so this is safe to run as often as an admin likes. Returns
    ``ok=False`` with a human-readable message instead of raising when the
    provider is disabled, its credential is missing, its provider_type has
    no registered adapter, or the adapter call itself fails.
    """
    try:
        result = search_models(provider, query=query, cursor=None)
    except ImageProviderError as exc:
        return ConnectionTestResult(ok=False, message=str(exc))

    sample_ids = [option.model_id for option in result.options[:_SAMPLE_SIZE]]
    count = len(result.options)
    noun = "model" if count == 1 else "models"
    return ConnectionTestResult(
        ok=True,
        message=f"Connected: catalog search returned {count} {noun}.",
        sample_model_ids=sample_ids,
    )


__all__ = ["ConnectionTestResult", "test_connection"]
