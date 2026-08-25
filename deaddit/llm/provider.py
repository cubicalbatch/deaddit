"""Deterministic transport seam for the LLM client.

This module is THE seam for tests/evals: register a fake provider with
set_provider() and every LLMClient call is served deterministically.
Production never registers a provider — get_provider() then falls back to
the real HTTP transport (transport.post_chat).
"""

from __future__ import annotations

import deaddit.llm.transport as _transport

_provider = None


def set_provider(p) -> None:
    """Register a provider (a post_chat-compatible callable or object)."""
    global _provider
    _provider = p


def reset_provider() -> None:
    """Unregister the provider; get_provider() falls back to HTTP transport."""
    global _provider
    _provider = None


def get_provider():
    """Return the registered provider, else the real HTTP transport."""
    if _provider is None:
        return _transport.post_chat
    if callable(_provider):
        return _provider
    return _provider.post_chat


def get_stream_provider():
    """Return the registered provider's streaming transport, else HTTP SSE."""
    if _provider is None:
        return _transport.stream_chat
    if callable(_provider):
        return _provider
    return _provider.stream_chat
