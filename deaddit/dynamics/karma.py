"""Vote-authoritative score repair and karma rebuild (Phase D1, Wave B S2).

Nightly recompute per the D1 contract:

- All items are vote-authoritative: ``score`` and ``vote_count`` are repaired
  from the Vote rows (drift is logged). Items without votes have score 0 and
  vote_count 0.
- Karma = sum of effective scores over a user's posts/comments, where
  effective_score = item.score. Only users who own at least one post or
  comment are updated.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func

from deaddit.extensions import db
from deaddit.models import Comment, Post, User, Vote

logger = logging.getLogger(__name__)


_PLANS: tuple[tuple[str, type, Any], ...] = (
    ("post", Post, Vote.post_id),
    ("comment", Comment, Vote.comment_id),
)


def _vote_aggregates(vote_column: Any) -> dict[int, tuple[int, int]]:
    """Per-target ``(sum(value), count)`` over existing Vote rows."""
    rows = (
        db.session.query(vote_column, func.sum(Vote.value), func.count(Vote.id))
        .filter(vote_column.isnot(None))
        .group_by(vote_column)
        .all()
    )
    return {tid: (int(total), int(count)) for tid, total, count in rows}


def recompute_scores_and_karma() -> dict[str, int]:
    """Repair vote-authoritative aggregates and rebuild user karma.

    Returns the summary ``{"repaired", "drift_votes", "karma_updates"}``:
    items whose aggregates were fixed, individual column mismatches found
    against Vote truth, and karma columns changed.
    """
    repaired = drift_votes = 0
    effective: dict[tuple[str, int], int] = {}

    for name, model, vote_column in _PLANS:
        aggregates = _vote_aggregates(vote_column)
        for item in db.session.query(model).all():
            agg = aggregates.get(item.id)
            total, count = agg if agg is not None else (0, 0)
            changed = False
            if item.score != total:
                drift_votes += 1
                item.score = total
                changed = True
            if item.vote_count != count:
                drift_votes += 1
                item.vote_count = count
                changed = True
            if changed:
                logger.info(
                    "%s %s drifted: repaired to score=%d vote_count=%d",
                    name,
                    item.id,
                    total,
                    count,
                )
                repaired += 1
            effective[(name, item.id)] = total

    karma_updates = 0
    for name, model, _ in _PLANS:
        attr = "post_karma" if name == "post" else "comment_karma"
        totals: dict[str, int] = {}
        for item in db.session.query(model).all():
            eff = effective.get((name, item.id))
            if eff is None:
                continue
            totals[item.user] = totals.get(item.user, 0) + eff
        for username, total in totals.items():
            user = db.session.get(User, username)
            if user is not None and getattr(user, attr) != total:
                setattr(user, attr, total)
                karma_updates += 1

    db.session.commit()
    summary = {
        "repaired": repaired,
        "drift_votes": drift_votes,
        "karma_updates": karma_updates,
    }
    logger.info("dynamics recompute summary: %s", summary)
    return summary
