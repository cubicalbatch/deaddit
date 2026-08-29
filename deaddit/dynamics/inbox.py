"""Inbox notification service (Phase D3, slice B2).

Shared contract per the roadmap: notifications land in ``notification`` (see
:class:`deaddit.models.Notification`) and this module is the only reader.
The AgenticCore consumes it in two places:

- the ``view_inbox`` agent tool (:func:`deaddit.agents.tools_read._view_inbox`)
  fetches a page and marks what it saw as read;
- context assembly (:func:`deaddit.agents.prompts.prepare_agent_visit`) asks
  :func:`unread_count` to decide whether to inject an inbox notice.

Pagination is keyset-stable under inserts: the cursor encodes
``(created_at isoformat, id)`` of the last item of the previous page and the
tuple comparison ``(created_at, id) < (cursor_ts, cursor_id)`` keeps paging
deterministic even when newer rows are inserted mid-iteration.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from deaddit.extensions import db
from deaddit.models import Notification


def _clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), 100))


def _encode_cursor(item: Notification) -> str:
    ts = item.created_at or datetime.utcnow()
    return f"{ts.isoformat()}|{item.id}"


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        raw_ts, raw_id = cursor.rsplit("|", 1)
        return datetime.fromisoformat(raw_ts), int(raw_id)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"invalid inbox cursor: {cursor!r}") from exc


def get_inbox(
    username: str,
    unread_only: bool = True,
    limit: int = 25,
    cursor: str | None = None,
) -> dict:
    """Return one newest-first page of ``username``'s inbox items.

    Each item is ``{id, kind, actor, post_id, comment_id, snippet,
    created_at (iso), read_at (iso | None)}``. The result also carries
    ``unread`` (total unread count for the user, independent of the page)
    and ``next_cursor`` (opaque keyset token, ``None`` when exhausted).
    """
    query = Notification.query.filter_by(recipient=username)
    if cursor is not None:
        cursor_ts, cursor_id = _decode_cursor(cursor)
        query = query.filter(
            db.or_(
                Notification.created_at < cursor_ts,
                db.and_(
                    Notification.created_at == cursor_ts,
                    Notification.id < cursor_id,
                ),
            )
        )
    if unread_only:
        query = query.filter(Notification.read_at.is_(None))

    limit = _clamp_limit(limit)
    rows = (
        query.order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit + 1)  # peek one past the page to know if more remain
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]

    items = [
        {
            "id": row.id,
            "kind": row.kind,
            "actor": row.actor,
            "post_id": row.post_id,
            "comment_id": row.comment_id,
            "snippet": row.snippet,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "read_at": row.read_at.isoformat() if row.read_at else None,
        }
        for row in rows
    ]
    next_cursor = _encode_cursor(rows[-1]) if has_more and rows else None
    return {
        "items": items,
        "unread": unread_count(username),
        "next_cursor": next_cursor,
    }


def mark_inbox_read(username: str, ids: list[int] | str | None = None) -> dict:
    """Mark some or all of ``username``'s unread notifications as read.

    ``ids`` restricts the flip to those ids (ownership-filtered); ``"all"``
    flips every unread row. Returns ``{"count": n}`` where ``n`` is the
    number of rows flipped this call — a repeated call returns ``0``.
    Commits.
    """
    query = Notification.query.filter(
        Notification.recipient == username, Notification.read_at.is_(None)
    )
    if ids != "all":
        query = query.filter(Notification.id.in_(ids or []))
    count = query.update({"read_at": datetime.utcnow()}, synchronize_session=False)
    db.session.commit()
    return {"count": count}


def unread_count(username: str) -> int:
    """Number of ``username``'s notifications with ``read_at IS NULL``."""
    return int(
        Notification.query.filter(
            Notification.recipient == username, Notification.read_at.is_(None)
        ).count()
    )


def purge_read_notifications(max_age_days: int = 90) -> dict[str, int]:
    """Delete read notifications older than ``max_age_days`` days.

    One short DELETE statement; returns ``{"purged": n}``. Unread and recent
    rows are never touched.
    """
    cutoff = datetime.utcnow() - timedelta(days=max_age_days)
    purged = Notification.query.filter(
        Notification.read_at.isnot(None), Notification.read_at < cutoff
    ).delete(synchronize_session=False)
    db.session.commit()
    return {"purged": purged}
