"""Public live-activity ticker (Phase UX-6, Slice A).

Read-only merge of the four activity sources (posts, comments, votes, jobs)
into a uniform newest-first list rendered by ``templates/live.html`` (full
page) or ``partials/_live_items.html`` (fragment mode).

Pagination is keyset-based: each item carries an opaque cursor encoding
``(created_at, kind, id)``; ``?before=`` returns strictly older items,
``?since=`` strictly newer ones. The socket pump in
``deaddit/runtime/live_pump.py`` reuses the source predicates/count helpers
below -- do not duplicate their SQL.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import re
from datetime import datetime

from flask import Blueprint, jsonify, render_template, request, url_for
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from deaddit.extensions import db
from deaddit.models import Comment, Job, Post, Vote
from deaddit.utils import process_post_title

logger = logging.getLogger(__name__)

bp = Blueprint("live", __name__)

PAGE_SIZE = 30
FETCH_PER_SOURCE = 300
NEWER_LIMIT = 50

_COMMENT_SNIPPET_LEN = 140

KINDS = ("comment", "job", "post", "vote")

# Job event timestamp: first non-null of completed/started/created.
_JOB_TS = func.coalesce(Job.completed_at, Job.started_at, Job.created_at)


# ---------------------------------------------------------------------------
# Cursor encoding: JSON {ts, kind, id} -> unpadded urlsafe base64.
# ---------------------------------------------------------------------------


def encode_cursor(ts: datetime | None, kind: str, id_: int) -> str:
    payload = json.dumps(
        {"ts": ts.isoformat() if ts else None, "kind": kind, "id": int(id_)}
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(raw: str | None) -> tuple[datetime, str, int] | None:
    """Decode tolerantly; any garbage (or unknown kind) -> None, never raise."""
    if not raw:
        return None
    try:
        padded = raw + "=" * (-len(raw) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        ts = datetime.fromisoformat(data["ts"])
        kind = data["kind"]
        id_ = int(data["id"])
    except (KeyError, ValueError, TypeError):
        return None
    if kind not in KINDS or ts.tzinfo is not None:
        return None
    return ts, kind, id_


# ---------------------------------------------------------------------------
# Source queries (shared shape with runtime/live_pump.py count helpers).
# ---------------------------------------------------------------------------


def _keyset_predicate(
    ts_col, id_col, ts: datetime, id_: int, newer: bool, kind: str, cursor_kind: str | None
):
    """Keyset comparison for one source against the global (ts, kind, id) cursor.

    Within the same kind the strict per-source (ts, id) comparison applies.
    Across kinds at an identical timestamp a lower-kind source owns its WHOLE
    tie group on an older page (and a higher-kind source on a newer one), so
    the tie boundary is ``ts`` alone -- comparing ids across different tables
    would silently drop rows whenever a page boundary splits a tie group.
    """
    if newer:
        if cursor_kind is not None and kind > cursor_kind:
            return ts_col >= ts
        if cursor_kind is not None and kind < cursor_kind:
            return ts_col > ts
        return or_(ts_col > ts, and_(ts_col == ts, id_col > id_))
    if cursor_kind is not None and kind < cursor_kind:
        return ts_col <= ts
    if cursor_kind is not None and kind > cursor_kind:
        return ts_col < ts
    return or_(ts_col < ts, and_(ts_col == ts, id_col < id_))


def _fetch_rows(
    columns: tuple,
    decorate,
    ts_col,
    id_col,
    base_filters: tuple,
    newer: bool,
    cursor: tuple[datetime, str, int] | None,
    kind: str,
) -> list:
    """Run one source query ordered by (ts, id), keyset-filtered by cursor.

    ``decorate`` applies any joins to the fresh Select (e.g.
    ``lambda s: s.join(Post, Comment.post_id == Post.id)``).

    Over-fetches FETCH_PER_SOURCE per source so the dropped tail at identical
    cross-kind timestamps cannot lose rows at realistic volumes; the exact
    global order across kinds is resolved Python-side by the sort key below.
    """
    stmt = decorate(select(*columns))
    if base_filters:
        stmt = stmt.where(*base_filters)
    if cursor is not None:
        ts, ck, cid = cursor
        stmt = stmt.where(_keyset_predicate(ts_col, id_col, ts, cid, newer, kind, ck))
    if newer:
        stmt = stmt.order_by(ts_col.asc(), id_col.asc())
    else:
        stmt = stmt.order_by(ts_col.desc(), id_col.desc())
    return db.session.execute(stmt.limit(FETCH_PER_SOURCE)).all()


# ---------------------------------------------------------------------------
# Raw row fetchers -> [(dt, kind, id, fields-dict-without-cursor)]
# ---------------------------------------------------------------------------

_EventRow = tuple  # (datetime | None, str, int, dict)


def _plain_snippet(text: str | None) -> str:
    """First ~140 chars of comment content as plain text (no HTML)."""
    if not text:
        return ""
    plain = html.unescape(re.sub(r"<[^>]+>", " ", text))
    plain = re.sub(r"\s+", " ", plain).strip()
    if len(plain) <= _COMMENT_SNIPPET_LEN:
        return plain
    return plain[:_COMMENT_SNIPPET_LEN].rstrip() + "…"


def _post_events(newer: bool, cursor) -> list[_EventRow]:
    rows = _fetch_rows(
        (Post.id, Post.created_at, Post.title, Post.user, Post.subdeaddit_name),
        lambda s: s,
        Post.created_at,
        Post.id,
        (Post.removed.isnot(True),),
        newer,
        cursor,
        "post",
    )
    return [
        (
            r.created_at,
            "post",
            r.id,
            {
                "actor": r.user,
                "title": process_post_title(r.title),
                "community": r.subdeaddit_name,
                "href": url_for(
                    "web.post",
                    subdeaddit_name=r.subdeaddit_name,
                    post_id=r.id,
                ),
            },
        )
        for r in rows
    ]


def _comment_events(newer: bool, cursor) -> list[_EventRow]:
    # A comment is excluded when it OR its parent post is removed; its
    # community comes from the parent post.
    rows = _fetch_rows(
        (
            Comment.id,
            Comment.created_at,
            Comment.content,
            Comment.user,
            Post.subdeaddit_name,
            Post.id.label("post_id"),
        ),
        lambda s: s.join(Post, Comment.post_id == Post.id),
        Comment.created_at,
        Comment.id,
        (Comment.removed.isnot(True), Post.removed.isnot(True)),
        newer,
        cursor,
        "comment",
    )
    return [
        (
            r.created_at,
            "comment",
            r.id,
            {
                "actor": r.user,
                "title": _plain_snippet(r.content),
                "community": r.subdeaddit_name,
                "href": url_for(
                    "web.post",
                    subdeaddit_name=r.subdeaddit_name,
                    post_id=r.post_id,
                )
                + f"#comment-{r.id}",
            },
        )
        for r in rows
    ]


def _vote_events(newer: bool, cursor) -> list[_EventRow]:
    """Votes on posts ('voted on "<title>"') and on comments of a post."""
    vp = aliased(Post)
    vc = aliased(Comment)
    vpc = aliased(Post)
    columns = (
        Vote.id,
        Vote.created_at,
        Vote.voter,
        Vote.value,
        vp.title.label("post_title"),
        vp.subdeaddit_name.label("post_sub"),
        vp.id.label("post_pk"),
        vpc.title.label("c_post_title"),
        vpc.subdeaddit_name.label("c_post_sub"),
        vpc.id.label("c_post_pk"),
    )
    rows = _fetch_rows(
        columns,
        lambda s: s.outerjoin(vp, Vote.post_id == vp.id).outerjoin(
            vc, Vote.comment_id == vc.id
        ).outerjoin(vpc, vc.post_id == vpc.id),
        Vote.created_at,
        Vote.id,
        (),
        newer,
        cursor,
        "vote",
    )
    events: list[_EventRow] = []
    for r in rows:
        title = r.post_title if r.post_title is not None else r.c_post_title
        community = r.post_sub if r.post_sub is not None else r.c_post_sub
        post_pk = r.post_pk if r.post_pk is not None else r.c_post_pk
        if title is None or community is None or post_pk is None:
            # Vote whose target (and its post) was hard-deleted: unrenderable.
            continue
        phrase = (
            f'voted on "{process_post_title(title)}"'
            if r.post_title is not None
            else f'voted on a comment on "{process_post_title(title)}"'
        )
        value = "+1" if r.value > 0 else "-1"
        events.append(
            (
                r.created_at,
                "vote",
                r.id,
                {
                    "actor": r.voter,
                    "title": f"{phrase} ({value})",
                    "community": community,
                    "href": url_for(
                        "web.post", subdeaddit_name=community, post_id=post_pk
                    ),
                },
            )
        )
    return events


def _job_events(newer: bool, cursor) -> list[_EventRow]:
    rows = _fetch_rows(
        (Job.id, _JOB_TS.label("created_at"), Job.type, Job.status),
        lambda s: s,
        _JOB_TS,
        Job.id,
        (),
        newer,
        cursor,
        "job",
    )
    return [
        (
            r.created_at,
            "job",
            r.id,
            {
                "actor": None,
                "title": f"{r.type.value} job {r.status.value}",
                "community": None,
                "href": url_for("admin.job_detail", job_id=r.id),
            },
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Merge + route
# ---------------------------------------------------------------------------


def _collect_events(newer: bool, cursor) -> list[dict]:
    """Merge all four sources into uniform template-context dicts."""
    ts_c, kind_c, id_c = cursor if cursor is not None else (None, None, None)
    rows: list[_EventRow] = []
    rows.extend(_post_events(newer, cursor))
    rows.extend(_comment_events(newer, cursor))
    rows.extend(_vote_events(newer, cursor))
    rows.extend(_job_events(newer, cursor))
    if not newer and cursor is not None:
        # The per-source SQL prefilter only sees each source's own (ts, id);
        # cross-kind ties at identical timestamps are decided by the global
        # (ts, kind, id) key here so page boundaries never duplicate or skip.
        rows = [r for r in rows if (r[0], r[1], r[2]) < (ts_c, kind_c, id_c)]
    elif newer and cursor is not None:
        rows = [r for r in rows if (r[0], r[1], r[2]) > (ts_c, kind_c, id_c)]
    # Deterministic global order: createdat DESC, kind DESC, id DESC --
    # reverse=True on the (dt, kind, id) tuple sorts each component descending.
    rows.sort(key=lambda r: (r[0], r[1], r[2]), reverse=not newer)

    limit = NEWER_LIMIT if newer else PAGE_SIZE
    selected = rows[:limit]
    if newer:
        # since-merge walks ascending to take the 50 closest new items, but
        # every rendered list is newest-first.
        selected.reverse()
    items = []
    for dt, kind, id_, fields in selected:
        item = dict(fields)
        item["kind"] = kind
        item["created_at"] = dt
        item["ts_iso"] = dt.isoformat() if dt else ""
        item["cursor"] = encode_cursor(dt, kind, id_)
        items.append(item)
    return items


@bp.route("/live")
def recent():
    before_raw = request.args.get("before")
    since_raw = request.args.get("since")
    is_fragment = (
        request.args.get("fragment") == "1"
        or request.headers.get("HX-Request") == "true"
    )

    if before_raw and since_raw:
        error = "Use either 'before' or 'since', not both."
        if is_fragment:
            return (
                render_template("partials/_live_items.html", items=[], error=error),
                400,
            )
        return jsonify({"error": error}), 400

    # Garbage cursors decode to None and fall through to the first page;
    # this route never 500s on bad query strings.
    since = decode_cursor(since_raw)
    before = decode_cursor(before_raw)

    if since is not None:
        items = _collect_events(newer=True, cursor=since)
    elif before is not None:
        items = _collect_events(newer=False, cursor=before)
    else:
        items = _collect_events(newer=False, cursor=None)

    if is_fragment:
        return render_template("partials/_live_items.html", items=items)
    return render_template("live.html", items=items)


# ---------------------------------------------------------------------------
# Watermark / count helpers consumed by runtime/live_pump.py.
# ---------------------------------------------------------------------------


def max_event_ts() -> datetime | None:
    """Max event timestamp across the four sources (None on empty DB).

    Removed posts/comments are excluded to match the ticker's visible set.
    """
    candidates = []
    for expr, filters in (
        (Post.created_at, (Post.removed.isnot(True),)),
        (Comment.created_at, (Comment.removed.isnot(True),)),
        (Vote.created_at, ()),
        (_JOB_TS, ()),
    ):
        stmt = select(func.max(expr))
        if filters:
            stmt = stmt.where(*filters)
        value = db.session.execute(stmt).scalar()
        if value is not None:
            candidates.append(value)
    return max(candidates) if candidates else None


def count_events_after(ts: datetime | None) -> int:
    """COUNT of visible rows strictly past ``ts`` across the four sources.

    Mirrors the per-source keyset "newer" predicate (ts-only form); votes on
    hard-deleted targets are rare enough that they still tick the counter.
    """

    def _count(decorate, ts_col, filters: tuple = ()) -> int:
        stmt = decorate(select(func.count()))
        if filters:
            stmt = stmt.where(*filters)
        if ts is not None:
            stmt = stmt.where(ts_col > ts)
        return db.session.execute(stmt).scalar() or 0

    vp = aliased(Post)
    vc = aliased(Comment)
    vpc = aliased(Post)

    def _vote_joins(stmt):
        return (
            stmt.select_from(Vote)
            .outerjoin(vp, Vote.post_id == vp.id)
            .outerjoin(vc, Vote.comment_id == vc.id)
            .outerjoin(vpc, vc.post_id == vpc.id)
        )

    total = _count(lambda s: s, Post.created_at, (Post.removed.isnot(True),))
    total += _count(
        lambda s: s.select_from(Comment).join(Post, Comment.post_id == Post.id),
        Comment.created_at,
        (Comment.removed.isnot(True), Post.removed.isnot(True)),
    )
    total += _count(_vote_joins, Vote.created_at)
    total += _count(lambda s: s, _JOB_TS)
    return total
