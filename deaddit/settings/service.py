"""Process-local TTL cache for resolved configuration values.

The cache is per-process: each process (web app, worker) keeps its own view of
the database. The short TTL bounds cross-process staleness, so a setting
changed through the admin UI becomes visible to other processes within
``DEADDIT_SETTINGS_TTL_SECONDS`` (default 10s; bad values fall back to the
default). Environment-variable changes are picked up on restart only —
documented and normal.

Writes invalidate eagerly: an ``after_flush`` event on the ORM Session class
invalidates cached keys whenever ``Setting`` rows change, so same-process
readers never observe stale values across a flush.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

DEFAULT_TTL_SECONDS = 10.0


class SecretNotPersistable(ValueError):
    """Raised when attempting to store a secret setting in the database."""


class DeployFlagNotPersistable(ValueError):
    """Raised when attempting to store a deploy-time flag in the database.

    Deploy flags belong to whoever starts the process, not to the admin UI, and
    are resolved from the environment only.
    """


class _Missing:
    """Private sentinel marking a cached negative lookup (distinct from None)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<missing>"


MISSING = _Missing()

_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}
_hook_registered = False


def ttl_seconds() -> float:
    """Effective TTL in seconds, from DEADDIT_SETTINGS_TTL_SECONDS if valid."""
    raw = os.environ.get("DEADDIT_SETTINGS_TTL_SECONDS", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEFAULT_TTL_SECONDS


def cached(key: str, resolver: Callable[[], str | None]) -> str | None:
    """Return the cached resolution for ``key``, or resolve via ``resolver``.

    Negative results (``None``) are cached as missing so repeated lookups of
    absent settings do not hit the database either.
    """
    now = time.monotonic()
    with _lock:
        entry = _cache.get(key)
        if entry is not None and entry[0] > now:
            value = entry[1]
            return None if value is MISSING else value

    resolved = resolver()
    expiry = time.monotonic() + ttl_seconds()
    with _lock:
        _cache[key] = (expiry, MISSING if resolved is None else resolved)
    return resolved


def invalidate(key: str) -> None:
    """Drop the cached entry for ``key``, if any."""
    with _lock:
        _cache.pop(key, None)


def clear() -> None:
    """Drop every cached entry."""
    with _lock:
        _cache.clear()


def register_invalidation_hook() -> None:
    """Invalidate cached keys whenever a session flush touches Setting rows.

    Idempotent: safe to call repeatedly (multiple create_app calls must not
    stack listeners).
    """
    global _hook_registered
    if _hook_registered:
        return
    _hook_registered = True

    from sqlalchemy import event
    from sqlalchemy.orm import Session

    from deaddit.models import Setting

    def _after_flush(session, flush_context):
        touched = {
            obj.key
            for obj in list(session.new) + list(session.dirty) + list(session.deleted)
            if isinstance(obj, Setting)
        }
        for key in touched:
            invalidate(key)

    event.listen(Session, "after_flush", _after_flush)


register_invalidation_hook()
