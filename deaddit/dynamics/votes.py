"""Vote casting service (Phase D1, Wave B slice S2).

Implements the frozen D1 contract: transactional vote upsert with score /
vote_count / upvote_count bookkeeping and author karma adjustments.
Rejection reasons are BYTE-FROZEN — agents match on them verbatim.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError

from deaddit.extensions import db
from deaddit.models import Comment, Post, Setting, User, Vote

_TRUTHY = frozenset({"true", "1", "on", "yes"})

_MODELS: dict[str, type] = {"post": Post, "comment": Comment}


def _downvotes_allowed() -> bool:
    """Read the ``allow_downvotes`` setting; only known truthy values allow them."""
    return (
        Setting.get_value("allow_downvotes", "true") or ""
    ).strip().lower() in _TRUTHY


def _reject(reason: str, score: int) -> dict[str, Any]:
    return {"status": "rejected", "reason": reason, "score": score}


def _find_vote(voter: str, target: str, target_id: int) -> Vote | None:
    filters: dict[str, Any] = {"voter": voter}
    if target == "post":
        filters["post_id"] = target_id
    else:
        filters["comment_id"] = target_id
    return db.session.query(Vote).filter_by(**filters).one_or_none()


def cast_vote(
    voter: str, target: str, target_id: int, value: int, _retried: bool = False
) -> dict[str, Any]:
    """Cast (or recast) ``voter``'s vote of ``value`` on a post or comment.

    One transaction: upsert the :class:`Vote` row, adjust the target's
    ``score``/``vote_count``/``upvote_count``, and adjust the author's
    ``post_karma``/``comment_karma``.

    Returns ``{"status": "ok", "score": <int>}`` on success (including an
    idempotent same-value re-vote) or ``{"status": "rejected", "reason":
    <str>, "score": <int>}`` otherwise. A concurrent duplicate insert loses
    to a unique-constraint IntegrityError, rolls back, and retries once so
    the outcome matches serialized execution.
    """
    model = _MODELS.get(target)
    item = db.session.get(model, target_id) if model else None
    if item is None:
        return _reject(f"{target} {target_id} does not exist", 0)
    score = int(item.score or 0)

    if value not in (1, -1):
        return _reject("value must be 1 or -1", score)

    if db.session.get(User, voter) is None:
        return _reject(f"user '{voter}' does not exist", score)

    if item.user == voter:
        return _reject(f"you cannot vote on your own {target}", score)

    if value == -1 and not _downvotes_allowed():
        return _reject("downvotes are disabled", score)

    is_post = target == "post"
    delta = 0
    try:
        vote = _find_vote(voter, target, target_id)
        if vote is None:
            vote = Vote(
                voter=voter,
                value=value,
                post_id=target_id if is_post else None,
                comment_id=None if is_post else target_id,
            )
            db.session.add(vote)
            delta = value
            item.vote_count += 1
        elif vote.value != value:
            delta = value - vote.value
            vote.value = value
        # Same-value re-vote: pure no-op.

        if delta:
            item.score += delta
            item.upvote_count = item.score
            author = db.session.get(User, item.user)
            if is_post:
                author.post_karma += delta
            else:
                author.comment_karma += delta
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        if _retried:
            raise
        # Lost an insert race against a concurrent identical vote; the
        # re-read resolves it deterministically (no-op or switch).
        return cast_vote(voter, target, target_id, value, _retried=True)

    return {"status": "ok", "score": int(item.score)}
