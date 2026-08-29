"""Notification emission for platform dynamics (plan §5).

Emits ``Notification`` rows for replies and mentions when new content lands.
Per platform-dynamics.md §5, this module is hooked into the single content
persistence path (:mod:`deaddit.services.content`) and runs strictly AFTER the
content transaction has committed.

Isolation contract: content creation must NEVER fail because emission failed.
Every public entry point wraps its entire body in ``try/except Exception``,
logs at warning level, rolls back any dirty state it left on the shared
session, and returns normally. Emission is provably non-raising.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from deaddit.dynamics.threads import exchange_cap, exchange_tail_for_reply
from deaddit.extensions import db
from deaddit.models import Comment, Notification, Post, User

logger = logging.getLogger(__name__)

_MENTION_TOKEN_RE = re.compile(r"@([A-Za-z0-9_-]+)")
_SNIPPET_LENGTH = 200
_DEDUPE_WINDOW = timedelta(hours=1)


def _emit(
    *,
    recipient: str | None,
    kind: str,
    actor: str | None,
    post_id: int | None,
    comment_id: int | None,
    snippet: str | None,
) -> None:
    """Insert one notification row in its own short transaction.

    Suppresses self-notifications and duplicate emissions within the rolling
    dedupe window. Never raises by itself, but callers still guard it.
    """
    if not recipient or recipient == actor:
        return
    cutoff = datetime.utcnow() - _DEDUPE_WINDOW
    duplicate = (
        db.session.query(Notification.id)
        .filter(
            Notification.recipient == recipient,
            Notification.kind == kind,
            Notification.post_id == post_id,
            Notification.actor == actor,
            Notification.created_at > cutoff,
        )
        .first()
    )
    if duplicate is not None:
        return
    db.session.add(
        Notification(
            recipient=recipient,
            kind=kind,
            actor=actor,
            post_id=post_id,
            comment_id=comment_id,
            snippet=snippet,
        )
    )
    db.session.commit()


def _mentioned_usernames(content: str | None) -> list[str]:
    """Return distinct existing usernames mentioned via ``@token`` (case-sensitive)."""
    if not content:
        return []
    usernames = {row[0] for row in db.session.query(User.username)}
    mentioned: list[str] = []
    for token in _MENTION_TOKEN_RE.findall(content):
        if token in usernames and token not in mentioned:
            mentioned.append(token)
    return mentioned


def notify_comment_created(comment: Comment) -> None:
    """Emit reply/mention notifications for a freshly committed comment."""
    comment_id = comment.id  # snapshot: ORM access is unsafe once a flush fails
    try:
        snippet = (comment.content or "")[:_SNIPPET_LENGTH]
        # Reply: parent comment's author for replies, post author for top-levels.
        if comment.parent_id is not None:
            recipient = (
                db.session.query(Comment.user)
                .filter(Comment.id == comment.parent_id)
                .scalar()
            )
            # Reply-chain fatigue: when this reply completes the pairwise
            # exchange cap, the counterpart is not invited back - the
            # exchange ends here, unanswered, the way real ones do. The
            # agent tool enforces the same cap (tail > cap is rejected),
            # so the two sides can never disagree.
            if (
                recipient is not None
                and recipient != comment.user
                and exchange_tail_for_reply(comment.parent_id, comment.user)
                >= exchange_cap(comment.post_id, recipient, comment.user)
            ):
                recipient = None
        else:
            recipient = (
                db.session.query(Post.user).filter(Post.id == comment.post_id).scalar()
            )
        _emit(
            recipient=recipient,
            kind="reply",
            actor=comment.user,
            post_id=comment.post_id,
            comment_id=comment.id,
            snippet=snippet,
        )
        # Mentions: every distinct known username tagged in the content.
        for username in _mentioned_usernames(comment.content):
            _emit(
                recipient=username,
                kind="mention",
                actor=comment.user,
                post_id=comment.post_id,
                comment_id=comment.id,
                snippet=snippet,
            )
    except Exception:
        # Roll back FIRST: any ORM attribute access against a failed flush
        # raises PendingRollbackError. Log only plain snapshotted values.
        db.session.rollback()
        logger.warning(
            "notification emission failed for comment %s", comment_id, exc_info=True
        )


def notify_post_created(post: Post) -> None:
    """Emit mention notifications for a freshly committed post."""
    post_id = post.id  # snapshot: ORM access is unsafe once a flush fails
    try:
        snippet = (post.content or "")[:_SNIPPET_LENGTH]
        for username in _mentioned_usernames(post.content):
            _emit(
                recipient=username,
                kind="mention",
                actor=post.user,
                post_id=post.id,
                comment_id=None,
                snippet=snippet,
            )
    except Exception:
        # Roll back BEFORE any attribute access; log plain snapshotted values.
        db.session.rollback()
        logger.warning(
            "notification emission failed for post %s", post_id, exc_info=True
        )


def notify_mod_action(
    *,
    recipient: str | None,
    actor: str | None,
    post_id: int | None = None,
    comment_id: int | None = None,
    snippet: str | None = None,
) -> None:
    """Emit a ``mod_action`` notification (the emitter owed from D3).

    Called by :mod:`deaddit.dynamics.moderation` strictly AFTER the
    moderation transaction has committed. Same isolation contract as every
    other public entry point: whole body guarded, rollback before any ORM
    access on a failed flush, log warning, never raise.
    """
    try:
        _emit(
            recipient=recipient,
            kind="mod_action",
            actor=actor,
            post_id=post_id,
            comment_id=comment_id,
            snippet=snippet,
        )
    except Exception:
        # Roll back BEFORE any attribute access; log plain snapshotted values.
        db.session.rollback()
        logger.warning(
            "notification emission failed for mod action to %s",
            recipient,
            exc_info=True,
        )
