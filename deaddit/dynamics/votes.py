"""Vote casting service (Phase D1, Wave B slice S2).

Implements the frozen D1 contract: transactional vote upsert with score /
vote_count bookkeeping and author karma adjustments.
Rejection reasons are BYTE-FROZEN — agents match on them verbatim.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from deaddit.dynamics import activity
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


def _success(score: int, changed: bool, change_kind: str) -> dict[str, Any]:
    return {
        "status": "ok",
        "score": score,
        "changed": changed,
        "change_kind": change_kind,
    }


def _find_vote(
    voter: str | None, target: str, target_id: int, visitor_hash: str | None = None
) -> Vote | None:
    filters: dict[str, Any] = (
        {"visitor_hash": visitor_hash} if visitor_hash else {"voter": voter}
    )
    if target == "post":
        filters["post_id"] = target_id
    else:
        filters["comment_id"] = target_id
    return db.session.query(Vote).filter_by(**filters).one_or_none()


def cast_vote(
    voter: str | None,
    target: str,
    target_id: int,
    value: int,
    _retried: bool = False,
    *,
    source: str = "simulated",
    allow_recast: bool = True,
    visitor_hash: str | None = None,
) -> dict[str, Any]:
    """Cast a vote while keeping score, vote count, and karma canonical.

    ``source`` defaults to ``"simulated"`` and ``allow_recast`` defaults to ``True``.
    Returns a standardized dictionary containing ``status``, ``score``, ``changed``,
    and ``change_kind`` metadata.

    Identity: either a platform ``voter`` username, or ``visitor_hash`` (the
    keyed hash of an anonymous browser's voter cookie) with ``voter=None`` —
    the visitor path skips user/self checks, since an anonymous browser is
    never a user and never authors content.

    ``value`` is 1, -1, or 0. Zero means "clear my existing vote" (delete the
    row and reverse its bookkeeping); with no existing row it is a no-op.
    """
    requested_source = source
    recast = allow_recast
    is_visitor = visitor_hash is not None

    model = _MODELS.get(target)
    if model is None:
        return _reject(f"{target} {target_id} does not exist", 0)

    # UPDATE takes a row lock on PostgreSQL and the SQLite writer lock.  It
    # serializes all vote decisions for this target before any vote read.
    locked = db.session.execute(
        update(model)
        .where(model.id == target_id)
        .values(score=model.score)
        .execution_options(synchronize_session=False)
    ).rowcount
    if locked != 1:
        db.session.commit()
        return _reject(f"{target} {target_id} does not exist", 0)

    item = db.session.get(model, target_id)
    if item is None:
        db.session.commit()
        return _reject(f"{target} {target_id} does not exist", 0)
    db.session.refresh(item)
    score = int(item.score or 0)

    if value not in (1, -1, 0):
        db.session.commit()
        return _reject("value must be 1 or -1", score)

    if not is_visitor and db.session.get(User, voter) is None:
        db.session.commit()
        return _reject(f"user '{voter}' does not exist", score)

    if item.user == voter:
        db.session.commit()
        return _reject(f"you cannot vote on your own {target}", score)

    if value == -1 and not _downvotes_allowed():
        db.session.commit()
        return _reject("downvotes are disabled", score)

    is_post = target == "post"
    delta = 0
    count_delta = 0
    changed = False
    change_kind = "same_value_noop"
    try:
        vote = _find_vote(voter, target, target_id, visitor_hash)
        if value == 0:
            # Clear: delete the row and reverse its bookkeeping. The
            # simulator is insert-only and never requests this.
            if vote is not None and recast:
                delta = -int(vote.value)
                count_delta = -1
                db.session.delete(vote)
                changed = True
                change_kind = "remove"
        elif vote is None:
            vote = Vote(
                voter=voter,
                visitor_hash=visitor_hash,
                value=value,
                source=requested_source,
                post_id=target_id if is_post else None,
                comment_id=None if is_post else target_id,
            )
            db.session.add(vote)
            delta = value
            count_delta = 1
            changed = True
            change_kind = "insert"
        elif not recast:
            # The simulator is insert-only. This branch intentionally wins
            # even for a same-value request: the pair was already claimed.
            change_kind = "insert_only_collision"
        elif int(vote.value) != value:
            delta = value - int(vote.value)
            vote.value = value
            changed = True
            change_kind = "direction_switch"
        # Same-value re-vote: pure no-op.

        if delta:
            db.session.execute(
                update(model)
                .where(model.id == target_id)
                .values(
                    score=model.score + delta,
                    vote_count=model.vote_count + count_delta,
                )
                .execution_options(synchronize_session=False)
            )
            karma_attr = "post_karma" if is_post else "comment_karma"
            karma_column = getattr(User, karma_attr)
            db.session.execute(
                update(User)
                .where(User.username == item.user)
                .values(**{karma_attr: karma_column + delta})
                .execution_options(synchronize_session=False)
            )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        if _retried:
            raise
        # Lost an insert race against a concurrent identical vote; the
        # re-read resolves it deterministically (no-op, collision, or switch).
        return cast_vote(
            voter,
            target,
            target_id,
            value,
            _retried=True,
            source=source,
            allow_recast=allow_recast,
            visitor_hash=visitor_hash,
        )

    if changed:
        activity.record_event(
            event_type="vote",
            username=None if is_visitor else voter,
            post_id=target_id if is_post else None,
            comment_id=None if is_post else target_id,
        )
    return _success(int(item.score), changed, change_kind)
