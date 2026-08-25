"""Single persistence path for user-generated content (Phase A4, Resolution 1).

Every creator validates its input, then ``db.session.add`` + ``commit`` inside
itself and returns the ORM object. Validation failures raise
:class:`ContentValidationError` with the exact legacy 400 body text from
``deaddit/api.py`` so the HTTP wrapper can map them verbatim. On
:class:`sqlalchemy.exc.SQLAlchemyError` the session is rolled back and the
exception re-raised unchanged (including ``IntegrityError`` for PK collisions).

Each function requires an active Flask application context; none of them
creates one.
"""

from __future__ import annotations

import json
from datetime import datetime
from functools import cache

from sqlalchemy.exc import SQLAlchemyError

from deaddit.dynamics import moderation, notifications
from deaddit.extensions import cache as flask_cache
from deaddit.extensions import db
from deaddit.models import Comment, Post, Subdeaddit, User

__all__ = ["ContentValidationError", "get_available_models"]


class ContentValidationError(ValueError):
    """Raised when creation input fails validation.

    The message maps verbatim to the legacy 400 error bodies of
    ``deaddit/api.py`` ``ingest()`` / ``ingest_user()``.
    """


@cache
def get_available_models() -> list[str]:
    # Query unique models from both Post and Comment tables
    post_models = db.session.query(Post.model).distinct().all()
    comment_models = db.session.query(Comment.model).distinct().all()

    # Combine and deduplicate the models
    all_models = {model[0] for model in post_models + comment_models if model[0]}

    return list(all_models)


def _clear_read_caches() -> None:
    """Invalidate read-side caches after a successful mutation.

    Lazy imports keep module load free of circular dependencies:
    ``deaddit.api`` re-exports :func:`get_available_models` from this module,
    and importing it at module scope would be circular.
    """
    from deaddit.api import get_available_models

    get_available_models.cache_clear()
    flask_cache.clear()  # Clear comment count caches


def _commit() -> None:
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        raise


def create_post(
    *,
    title: str,
    content: str,
    user: str,
    subdeaddit: str,
    upvote_count: int = 0,
    model: str = "unknown",
    post_type: str | None = None,
    created_at: datetime | None = None,
) -> Post:
    """Create and persist a :class:`~deaddit.models.Post`.

    Raises:
        ContentValidationError: on an empty title/content, unknown author, or
            unknown subdeaddit (legacy 400 messages).
    """
    if not title or not content:
        raise ContentValidationError("Invalid post data")
    if not User.query.filter_by(username=user).first():
        raise ContentValidationError(f"User '{user}' does not exist")
    if not Subdeaddit.query.filter_by(name=subdeaddit).first():
        raise ContentValidationError(f"Subdeaddit '{subdeaddit}' does not exist")
    if moderation.active_ban_for(user, subdeaddit) is not None:
        raise ContentValidationError(f"User '{user}' is banned")

    post = Post(
        title=title,
        content=content,
        upvote_count=upvote_count,
        user=user,
        subdeaddit_name=subdeaddit,
        model=model,
        post_type=post_type,
    )
    if created_at is not None:
        post.created_at = created_at
    db.session.add(post)
    _commit()
    _clear_read_caches()
    notifications.notify_post_created(post)
    return post


def create_comment(
    *,
    post_id: int,
    content: str,
    user: str,
    parent_id: int | None = None,
    upvote_count: int = 0,
    model: str = "unknown",
    created_at: datetime | None = None,
) -> Comment:
    """Create and persist a :class:`~deaddit.models.Comment`.

    Raises:
        ContentValidationError: on empty content, unknown author, unknown
            post, or unknown parent comment.
    """
    if not content:
        raise ContentValidationError("Comment missing required fields: content")
    if not User.query.filter_by(username=user).first():
        raise ContentValidationError(f"User '{user}' does not exist")
    post = db.session.get(Post, post_id)
    if post is None:
        raise ContentValidationError(f"Post '{post_id}' does not exist")
    if post.removed:
        raise ContentValidationError(f"Post '{post_id}' has been removed")
    if moderation.active_ban_for(user, post.subdeaddit_name) is not None:
        raise ContentValidationError(f"User '{user}' is banned")

    comment = Comment(
        post_id=post_id,
        parent_id=parent_id,
        content=content,
        upvote_count=upvote_count,
        user=user,
        model=model,
    )
    if created_at is not None:
        comment.created_at = created_at
    db.session.add(comment)
    _commit()
    _clear_read_caches()
    notifications.notify_comment_created(comment)
    return comment


def create_user(
    *,
    username: str,
    age: int,
    gender: str,
    bio: str,
    interests: list | str,
    occupation: str,
    education: str,
    writing_style: str,
    personality_traits: list | str,
    model: str = "unknown",
    created_at: datetime | None = None,
) -> User:
    """Create and persist a :class:`~deaddit.models.User`.

    ``gender`` is coerced to ``"Male"`` unless it is exactly ``"Male"``
    or ``"Female"``. ``interests`` / ``personality_traits`` accept either a
    list (JSON-serialized) or a pre-serialized JSON string (stored as-is).

    Duplicate usernames surface as ``IntegrityError`` after rollback — there
    is deliberately no second error type for PK collisions.

    Note:
        ``created_at`` is accepted for signature uniformity but only applied
        if the model has a ``created_at`` column (Phase D5: it does).

    Raises:
        IntegrityError: on duplicate username, after rollback.
    """
    gender = gender if gender in ("Male", "Female") else "Male"
    interests_json = interests if isinstance(interests, str) else json.dumps(interests)
    traits_json = (
        personality_traits
        if isinstance(personality_traits, str)
        else json.dumps(personality_traits)
    )

    user_obj = User(
        username=username,
        age=age,
        gender=gender,
        bio=bio,
        interests=interests_json,
        occupation=occupation,
        education=education,
        writing_style=writing_style,
        personality_traits=traits_json,
        model=model,
    )
    if created_at is not None and "created_at" in User.__table__.columns:
        user_obj.created_at = created_at
    db.session.add(user_obj)
    _commit()
    _clear_read_caches()
    return user_obj


def create_subdeaddit(
    *,
    name: str,
    description: str,
    post_types: list[str] | None = None,
    update_if_exists: bool = False,
    created_at: datetime | None = None,
) -> Subdeaddit:
    """Create a :class:`~deaddit.models.Subdeaddit`, or upsert it.

    Note:
        ``created_at`` is accepted for signature uniformity but only applied
        if the model has a ``created_at`` column (Phase D5: it does).

    Raises:
        ContentValidationError: missing name/description, or existing name
            with ``update_if_exists=False``.
    """
    missing_fields = []
    if not name:
        missing_fields.append("name")
    if not description:
        missing_fields.append("description")
    if missing_fields:
        raise ContentValidationError(
            f"Subdeaddit missing required fields: {', '.join(missing_fields)}"
        )

    types_list = post_types if post_types is not None else []

    existing = db.session.get(Subdeaddit, name)
    if existing:
        if not update_if_exists:
            raise ContentValidationError(f"Subdeaddit '{name}' already exists")
        existing.description = description
        existing.set_post_types(types_list)
        _commit()
        _clear_read_caches()
        return existing

    subdeaddit = Subdeaddit(name=name, description=description)
    subdeaddit.set_post_types(types_list)
    if created_at is not None and "created_at" in Subdeaddit.__table__.columns:
        subdeaddit.created_at = created_at
    db.session.add(subdeaddit)
    _commit()
    _clear_read_caches()
    return subdeaddit
