"""Shared admin-session authentication helpers."""

import hashlib
import hmac

from flask import session

from .config import Config

_SESSION_FINGERPRINT = "admin_token_fingerprint"


def token_fingerprint(token: str) -> str:
    """Return a non-secret fingerprint suitable for a signed session cookie."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_admin_authenticated() -> bool:
    """Validate the session binding against the currently configured token."""
    api_token = Config.get("API_TOKEN")
    fingerprint = session.get(_SESSION_FINGERPRINT)
    return bool(
        api_token
        and isinstance(fingerprint, str)
        and hmac.compare_digest(fingerprint, token_fingerprint(api_token))
    )
