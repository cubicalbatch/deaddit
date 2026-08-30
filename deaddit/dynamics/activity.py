"""ActivityEvent emission for platform metrics (Phase D6, plan §8).

``ActivityEvent`` is the raw truth behind the ``PlatformDaily`` rollup. This
module is the ONLY writer. It is hooked into the single persistence path
(``deaddit.services.content``, Resolution 1) and into the vote and report
services, always strictly AFTER their transactions commit.

Isolation contract (mirrors :mod:`deaddit.dynamics.notifications`): a platform
action must NEVER fail because event emission failed. Every public entry
point wraps its whole body in ``try/except Exception``, logs at warning
level, rolls back any dirty state it left on the shared session, and returns
normally. Emission is provably non-raising.

Event types: 'post' | 'comment' | 'vote' | 'report'.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from deaddit.extensions import db
from deaddit.models import ActivityEvent

logger = logging.getLogger(__name__)

EVENT_TYPES = ("post", "comment", "vote", "report")


def record_event(
    *,
    event_type: str,
    username: str | None = None,
    post_id: int | None = None,
    comment_id: int | None = None,
    meta: dict | None = None,
) -> None:
    """Insert one ActivityEvent in its own short transaction; never raises.

    ``meta`` is a plain dict WE assemble from request context — it is JSON we
    build ourselves, never parsed model output (Resolution 11).
    """
    try:
        db.session.add(
            ActivityEvent(
                occurred_at=datetime.utcnow(),
                event_type=event_type,
                username=username,
                post_id=post_id,
                comment_id=comment_id,
                meta=json.dumps(meta) if meta else None,
            )
        )
        db.session.commit()
    except Exception:  # noqa: BLE001 - isolation contract, see module docstring
        db.session.rollback()
        logger.warning(
            "activity event emission failed (type=%s user=%s post=%s comment=%s)",
            event_type,
            username,
            post_id,
            comment_id,
            exc_info=True,
        )
