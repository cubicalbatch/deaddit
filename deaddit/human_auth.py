"""Human authentication and session management for Deaddit.

Provides helpers for human user registration, credential verification, session
resolution, route protection, and runtime feature gating.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from functools import wraps

from flask import abort, g, has_request_context, redirect, request, session, url_for
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from deaddit.config import Config
from deaddit.extensions import db
from deaddit.models import User
from deaddit.utils import safe_local_next

logger = logging.getLogger(__name__)

TRUTHY_VALUES = frozenset({"true", "1", "yes", "on"})
FALSY_VALUES = frozenset({"false", "0", "no", "off"})

# Constant dummy hash computed once at import time to prevent user enumeration
# via timing side channels.
_DUMMY_PASSWORD_HASH = generate_password_hash("deaddit-timing-defense-dummy-password")


def human_accounts_enabled() -> bool:
    """Check if human accounts and participation are enabled.

    Environment variable HUMAN_ACCOUNTS_ENABLED takes precedence over database
    settings. A malformed environment variable logs a warning and fails closed
    (returns False).
    """
    if "HUMAN_ACCOUNTS_ENABLED" in os.environ:
        raw = os.environ.get("HUMAN_ACCOUNTS_ENABLED", "").strip().lower()
        if raw in TRUTHY_VALUES:
            return True
        if raw in FALSY_VALUES:
            return False
        logger.warning(
            "Malformed HUMAN_ACCOUNTS_ENABLED environment variable: %r. Failing closed.",
            os.environ.get("HUMAN_ACCOUNTS_ENABLED"),
        )
        return False

    raw = Config.get("HUMAN_ACCOUNTS_ENABLED", "true")
    if isinstance(raw, str):
        raw = raw.strip().lower()
    if raw in TRUTHY_VALUES:
        return True
    if raw in FALSY_VALUES:
        return False
    return False


def current_human() -> User | None:
    """Resolve and request-cache the currently authenticated human user.

    Uses flask.g._current_human as request-level cache. Requires user.is_human
    (non-null password hash) and human_accounts_enabled(). Clears stale session
    identity if the user no longer exists or is not human.
    """
    if not has_request_context():
        return None

    username = session.get("human_username")
    if not username:
        g._current_human = None
        return None

    if getattr(g, "_current_human", None) is not None and g._current_human.username == username:
        return g._current_human

    user = User.query.filter_by(username=username).first()
    if user is not None and user.is_human and human_accounts_enabled():
        g._current_human = user
        return user

    session.pop("human_username", None)
    g._current_human = None
    return None


def human_login_required(f):
    """Decorator to require an authenticated human user for a view.

    Aborts with 404 if human accounts are disabled. Redirects anonymous requests
    to login with a safe local return path.
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not human_accounts_enabled():
            abort(404)
        if not current_human():
            target = request.full_path if request.query_string else request.path
            safe_next = safe_local_next(target)
            login_url = (
                url_for("web.login", next=safe_next)
                if safe_next
                else url_for("web.login")
            )
            return redirect(login_url)
        return f(*args, **kwargs)

    return decorated_function


def register_human(
    username: str, password: str, password_confirm: str
) -> tuple[User | None, str | None]:
    """Register a new human account.

    Validates username and password, performs case-insensitive collision check,
    persists User row, and signs into the session.

    Returns:
        (user, None) on success, or (None, error_message) on failure.
    """
    if not human_accounts_enabled():
        return None, "Human accounts are disabled"

    canonical = (username or "").strip().lower()

    if (
        len(canonical) < 3
        or len(canonical) > 30
        or not re.match(r"^[a-z0-9_-]+$", canonical)
    ):
        return (
            None,
            "Username must be between 3 and 30 characters and contain only letters, numbers, underscores, and hyphens.",
        )

    if not password or len(password) < 8 or len(password) > 256:
        return None, "Password must be between 8 and 256 characters."

    if password != password_confirm:
        return None, "Passwords do not match."

    # Case-insensitive collision check
    existing = User.query.filter(func.lower(User.username) == canonical).first()
    if existing is not None:
        return None, "Username is already taken"

    user = User(
        username=canonical,
        password_hash=generate_password_hash(password),
        model="human",
        created_at=datetime.utcnow(),
        agent_state={},
        bio="",
        occupation="",
        education="",
        writing_style="",
        interests="[]",
        personality_traits="[]",
    )
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return None, "Username is already taken"

    session["human_username"] = user.username
    if has_request_context():
        g._current_human = user
    return user, None


def authenticate_human(
    username: str, password: str
) -> tuple[User | None, str | None]:
    """Authenticate human credentials and establish session identity.

    Uses a dummy password check on non-existent or synthetic accounts to maintain
    constant-time response and prevent username enumeration.

    Returns:
        (user, None) on success, or (None, error_message) on failure.
    """
    if not human_accounts_enabled():
        return None, "Human accounts are disabled"

    canonical = (username or "").strip().lower()
    user = User.query.filter(func.lower(User.username) == canonical).first()

    if user is None or not user.is_human or not user.password_hash:
        check_password_hash(_DUMMY_PASSWORD_HASH, password or "")
        return None, "Invalid username or password"

    if not check_password_hash(user.password_hash, password or ""):
        return None, "Invalid username or password"

    session["human_username"] = user.username
    if has_request_context():
        g._current_human = user
    return user, None


def logout_human() -> None:
    """Clear human identity from session and request cache.

    Does not clear admin authentication keys.
    """
    session.pop("human_username", None)
    if has_request_context():
        g._current_human = None
