"""Single persistence path for user-generated content (Phase A4, Resolution 1).

Every creator validates its input, then ``db.session.add`` + ``commit`` inside
itself and returns the ORM object. Validation failures raise
:class:`ContentValidationError`. On :class:`sqlalchemy.exc.SQLAlchemyError`
the session is rolled back and the exception re-raised unchanged (including
``IntegrityError`` for PK collisions).

Each function requires an active Flask application context; none of them
creates one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cache

from sqlalchemy.exc import SQLAlchemyError

from deaddit.dynamics import activity, degeneracy, moderation, notifications
from deaddit.extensions import cache as flask_cache
from deaddit.extensions import db
from deaddit.models import Comment, Post, PostImage, Setting, Subdeaddit, User

__all__ = [
    "ContentValidationError",
    "PendingPostImage",
    "get_available_models",
    "preflight_image_post",
    "create_image_post",
]


class ContentValidationError(ValueError):
    """Raised when creation input fails validation."""


@cache
def get_available_models() -> list[str]:
    # Query unique models from both Post and Comment tables
    post_models = db.session.query(Post.model).distinct().all()
    comment_models = db.session.query(Comment.model).distinct().all()

    # Combine and deduplicate the models
    all_models = {model[0] for model in post_models + comment_models if model[0]}
    return list(all_models)


# Per-user hourly creation caps (plan §7): 5 posts, 30 comments per hour by
# default; each cap is Setting-tunable and a negative value disables it.
_RATE_LIMITS: dict[str, tuple[str, int]] = {
    "post": ("rate_limit_posts_per_hour", 5),
    "comment": ("rate_limit_comments_per_hour", 30),
}


def _check_rate_limit(user: str, kind: str) -> None:
    """Reject creation overflow with the machine-readable reason rate_limited."""
    key, default = _RATE_LIMITS[kind]
    raw = Setting.get_value(key, str(default))
    try:
        limit = int(str(raw))
    except (TypeError, ValueError):
        limit = default
    if limit < 0:
        return
    model = Post if kind == "post" else Comment
    cutoff = datetime.utcnow() - timedelta(hours=1)
    recent = model.query.filter(model.user == user, model.created_at >= cutoff).count()
    if recent >= limit:
        raise ContentValidationError("rate_limited")


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


def _validate_post_fields(
    title: str, content: str | None, *, require_content: bool
) -> None:
    """Reject a missing title, or missing content when content is required.

    ``require_content=True`` reproduces the original text-post contract
    exactly (empty title or content -> ``"Invalid post data"``).
    ``require_content=False`` is the image-post contract: content may be
    blank or ``None`` because the image itself carries the post.
    """
    if not title or (require_content and not content):
        raise ContentValidationError("Invalid post data")


def _validate_post_preflight(
    *,
    title: str,
    content: str | None,
    user: str,
    subdeaddit: str,
    require_content: bool,
) -> None:
    """Every check a post must pass before it may be written.

    Shared by text posts (:func:`create_post`) and image posts
    (:func:`preflight_image_post` / :func:`create_image_post`) so both
    contracts reject unknown users/communities, active bans, and an
    exceeded post rate limit identically. Order matters: field validation
    first (cheap, no queries), then existence, then ban, then rate limit —
    matching the original ``create_post`` behavior byte-for-byte.
    """
    _validate_post_fields(title, content, require_content=require_content)
    if not User.query.filter_by(username=user).first():
        raise ContentValidationError(f"User '{user}' does not exist")
    if not Subdeaddit.query.filter_by(name=subdeaddit).first():
        raise ContentValidationError(f"Subdeaddit '{subdeaddit}' does not exist")
    if moderation.active_ban_for(user, subdeaddit) is not None:
        raise ContentValidationError(f"User '{user}' is banned")
    _check_rate_limit(user, "post")


def _run_post_hooks(post: Post) -> None:
    """Cache/notification/activity/degeneracy side effects, run exactly once.

    Callers must invoke this exactly once, and only after the post (and,
    for image posts, its ``PostImage``) has been committed. Extracted so
    every post-creation path — text or image — shares one hook-once
    implementation instead of duplicating the call sequence.
    """
    _clear_read_caches()
    notifications.notify_post_created(post)
    activity.record_event(event_type="post", username=post.user, post_id=post.id)
    degeneracy.detect_repetition_for_post(post)


def create_post(
    *,
    title: str,
    content: str,
    user: str,
    subdeaddit: str,
    score: int = 0,
    model: str = "unknown",
    post_type: str | None = None,
    created_at: datetime | None = None,
) -> Post:
    """Create and persist a text :class:`~deaddit.models.Post`.

    Raises:
        ContentValidationError: on an empty title/content, unknown author,
            unknown subdeaddit, an active ban, or an exceeded post rate
            limit.
    """
    _validate_post_preflight(
        title=title,
        content=content,
        user=user,
        subdeaddit=subdeaddit,
        require_content=True,
    )

    post = Post(
        title=title,
        content=content,
        score=score,
        user=user,
        subdeaddit_name=subdeaddit,
        model=model,
        post_type=post_type,
    )
    if created_at is not None:
        post.created_at = created_at
    db.session.add(post)
    _commit()
    _run_post_hooks(post)
    return post


@dataclass(frozen=True)
class PendingPostImage:
    """A validated, already-stored image ready to attach to a new post.

    Every field mirrors a :class:`~deaddit.models.PostImage` column. The
    caller — the image-publication orchestration layer (plan phase 4B),
    not this service — is responsible for generating, downloading,
    decoding, and atomically storing the original/thumbnail files
    (:mod:`deaddit.images.storage`) *before* constructing this dataclass
    and calling :func:`create_image_post`. This service performs no file
    I/O: it only writes the database rows that reference the given paths.

    On any failure inside :func:`create_image_post` (validation or
    database), no Post/PostImage row is created and the files on disk are
    left untouched — removing them is the orchestration layer's job.
    """

    original_path: str
    thumbnail_path: str
    mime_type: str
    byte_size: int
    width: int
    height: int
    alt_text: str
    source_prompt: str
    provider_snapshot: str
    model_snapshot: str
    provider_id: int | None = None
    request_snapshot: str | None = None


def preflight_image_post(*, user: str, subdeaddit: str, title: str) -> None:
    """Validate everything that must hold before an image is generated.

    Call this first, before spending any provider cost on image
    generation. It runs the same checks as :func:`create_post` minus the
    content requirement: a non-empty title, a known user and subdeaddit,
    no active ban, and an unexceeded post rate limit.

    This check is advisory for cost avoidance only — it does not reserve
    a rate-limit slot or lock anything. Generation and storage can take
    long enough for state to change (a new ban lands, the rate-limit
    window fills, the community is deleted), so :func:`create_image_post`
    independently re-runs every one of these checks immediately before it
    commits.

    Raises:
        ContentValidationError: on an empty title, unknown author, unknown
            subdeaddit, an active ban, or an exceeded post rate limit.
    """
    _validate_post_preflight(
        title=title,
        content=None,
        user=user,
        subdeaddit=subdeaddit,
        require_content=False,
    )


def create_image_post(
    *,
    title: str,
    content: str | None,
    user: str,
    subdeaddit: str,
    image: PendingPostImage,
    score: int = 0,
    model: str = "unknown",
    post_type: str | None = None,
    created_at: datetime | None = None,
) -> Post:
    """Atomically create a Post and its PostImage; run hooks exactly once.

    Call this only after :func:`preflight_image_post` has succeeded and
    the image has already been generated and stored on disk as a
    :class:`PendingPostImage`. This function re-validates every
    preflight condition itself immediately before commit — see
    :func:`preflight_image_post` for why that recheck matters — so it
    never trusts a preflight result that may be stale.

    ``content`` may be blank or ``None``: image posts are not required to
    carry body text, unlike :func:`create_post`. Passing an ``image`` is
    what makes blank content acceptable; there is no way to call this
    function without one.

    Both rows are written in the same database transaction: a failure
    committing either one leaves neither behind. On any failure, this
    function does not touch the filesystem — removing the original and
    thumbnail files referenced by ``image`` is the caller's
    responsibility (the image-publication orchestration layer owns
    filesystem rollback, not the content service).

    Raises:
        ContentValidationError: same conditions as
            :func:`preflight_image_post`, re-checked at commit time.
    """
    _validate_post_preflight(
        title=title,
        content=None,
        user=user,
        subdeaddit=subdeaddit,
        require_content=False,
    )

    post = Post(
        title=title,
        content=content or None,
        score=score,
        user=user,
        subdeaddit_name=subdeaddit,
        model=model,
        post_type=post_type,
    )
    if created_at is not None:
        post.created_at = created_at
    db.session.add(post)
    db.session.flush()  # assign post.id for the PostImage FK below

    db.session.add(
        PostImage(
            post_id=post.id,
            original_path=image.original_path,
            thumbnail_path=image.thumbnail_path,
            mime_type=image.mime_type,
            byte_size=image.byte_size,
            width=image.width,
            height=image.height,
            alt_text=image.alt_text,
            source_prompt=image.source_prompt,
            provider_id=image.provider_id,
            provider_snapshot=image.provider_snapshot,
            model_snapshot=image.model_snapshot,
            request_snapshot=image.request_snapshot,
        )
    )
    _commit()
    _run_post_hooks(post)
    return post


def create_comment(
    *,
    post_id: int,
    content: str,
    user: str,
    parent_id: int | None = None,
    score: int = 0,
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
    _check_rate_limit(user, "comment")

    comment = Comment(
        post_id=post_id,
        parent_id=parent_id,
        content=content,
        score=score,
        user=user,
        model=model,
    )
    if created_at is not None:
        comment.created_at = created_at
    db.session.add(comment)
    _commit()
    _clear_read_caches()
    notifications.notify_comment_created(comment)
    activity.record_event(
        event_type="comment", username=user, post_id=post_id, comment_id=comment.id
    )
    degeneracy.detect_repetition_for_comment(comment)
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
