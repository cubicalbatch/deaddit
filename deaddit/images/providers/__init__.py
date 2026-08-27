"""Concrete image-provider adapters (fal.ai, Runware, ...).

Importing this package performs no I/O and registers nothing on its own;
register_default_adapters() is the explicit, side-effect-safe call the app
factory makes to wire the real adapters into deaddit.images.client's
registry. Tests never call it - they register fakes (or leave the registry
empty) via deaddit.images.client.register_adapter()/reset_adapters()
instead, so no test ever reaches a live transport.
"""

from __future__ import annotations

from deaddit.images.client import register_adapter
from deaddit.images.providers.fal import PROVIDER_TYPE as _FAL_PROVIDER_TYPE
from deaddit.images.providers.fal import FalAdapter
from deaddit.images.providers.runware import PROVIDER_TYPE as _RUNWARE_PROVIDER_TYPE
from deaddit.images.providers.runware import RunwareAdapter


def register_default_adapters() -> None:
    """Register the real fal.ai and Runware adapters for production use.

    Idempotent: each call constructs fresh adapter instances and replaces
    whatever was previously registered for "fal"/"runware". Constructing an
    adapter performs no I/O (its transport is only used once a dispatch call
    is actually made), so calling this from the app factory has no network
    side effect.
    """
    register_adapter(_FAL_PROVIDER_TYPE, FalAdapter())
    register_adapter(_RUNWARE_PROVIDER_TYPE, RunwareAdapter())


__all__ = ["register_default_adapters"]
