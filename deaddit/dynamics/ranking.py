"""Feed and comment ranking for platform dynamics.

Source of truth: refactor/platform-dynamics.md §3 (formulas verbatim):

- hot  = log10(max(|score|, 1)) * sign(score) + (unix_ts(created_at) - 1134028003) / 45000
- top  = score DESC (time windows are explicitly out of scope)
- rising = score / pow(hours_since_post + 2, 1.8), restricted to posts from the
  last RISING_WINDOW_HOURS hours (rising_filter)
- new  = created_at DESC

Every post ordering ends with a deterministic ``id DESC`` tiebreak.
``HOT_SQL_FRAGMENT`` is the canonical SQLite text of the hot expression; it is
byte-shared with the expression index created by the D2 migration and must not
be reformatted or re-spaced.

Second-resolution parity: SQLite's ``strftime('%s', ...)`` truncates timestamps
to whole seconds, while Python datetimes carry microseconds. All pure-python
mirrors here therefore truncate both ``created_at`` and ``now`` to whole unix
seconds (naive datetimes are treated as UTC, matching how the app stores them)
before any arithmetic, so Python keys agree exactly with SQL rankings.
"""

from __future__ import annotations

import calendar
import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, text
from sqlalchemy.sql.elements import TextClause

from deaddit.models import Post

POST_SORTS = ("hot", "new", "top", "rising")
COMMENT_SORTS = ("top", "new", "best", "controversial")
HOT_EPOCH = 1134028003
HOT_GRAVITY = 45000.0
RISING_WINDOW_HOURS = 24
WILSON_Z = 1.96

# Canonical SQLite text, byte-shared with the D2 migration's expression index.
# The divisor literal MUST stay float (45000.0): with the integer 45000 SQLite
# performs integer division and truncates the recency term to whole-gravity
# buckets, diverging from the spec formula and the Python mirror.
HOT_SQL_FRAGMENT = (
    "(log10(max(abs(score),1))*sign(score)"
    " + (strftime('%s',created_at)-1134028003)/45000.0)"
)

_RISING_SQL_FRAGMENT = (
    "(score/pow(max((strftime('%s',:now)-strftime('%s',created_at))/3600.0,0.0)+2,1.8))"
)


def _unix_seconds(dt: datetime) -> int:
    """Whole unix seconds, truncating sub-second precision (strftime parity).

    Naive datetimes are interpreted as UTC — the app stores naive UTC via
    ``datetime.utcnow``, and SQLite's ``strftime('%s')`` applies no timezone.
    """
    return int(calendar.timegm(dt.utctimetuple()))


def normalize_post_sort(value: str | None) -> str:
    """Map raw user input onto POST_SORTS; ''/None/garbage fall back to 'hot'."""
    if isinstance(value, str) and value in POST_SORTS:
        return value
    return "hot"


def normalize_comment_sort(value: str | None) -> str:
    """Map raw user input onto COMMENT_SORTS; ''/None/garbage fall back to 'top'."""
    if isinstance(value, str) and value in COMMENT_SORTS:
        return value
    return "top"


def post_order_by(sort: str, now: datetime | None = None) -> list:
    """SQLAlchemy order-by clauses for Post feeds, ending in id DESC tiebreak.

    Usage: ``Post.query.order_by(*post_order_by(sort))`` on a single-table,
    unfiltered query over Post. For ``sort == 'rising'`` the returned clauses
    contain a :now-bound TextClause; callers MUST also apply
    ``rising_filter(now)`` to restrict the feed to the last
    RISING_WINDOW_HOURS hours, otherwise old zero/negative-score items leak in.
    """
    sort = normalize_post_sort(sort)
    if sort == "hot":
        hot: TextClause = text(HOT_SQL_FRAGMENT + " DESC")
        return [hot, Post.id.desc()]
    if sort == "top":
        return [Post.score.desc(), Post.id.desc()]
    if sort == "new":
        return [Post.created_at.desc(), Post.id.desc()]
    # rising
    # Bind :now as a SQLite datetime string: strftime('%s', X) only parses
    # ISO-ish text (raw numbers are misread as Julian days).
    now = now or datetime.now(UTC)
    if now.tzinfo is not None:
        now = now.astimezone(UTC)
    rising: TextClause = text(_RISING_SQL_FRAGMENT + " DESC").bindparams(
        now=now.strftime("%Y-%m-%d %H:%M:%S")
    )
    return [rising, Post.id.desc()]


def post_rank_key(
    sort: str, *, score: int, created_at: datetime, now: datetime
) -> float:
    """Pure-python mirror of post_order_by for one row.

    Both ``created_at`` and ``now`` are truncated to whole unix seconds before
    conversion so results match ``strftime('%s')`` semantics exactly (see
    module docstring). Keys compare descending, like the SQL ORDER BY.
    """
    ts_created = _unix_seconds(created_at)
    if sort == "hot":
        sign = -1 if score < 0 else (1 if score > 0 else 0)
        return (
            math.log10(max(abs(score), 1)) * sign
            + (ts_created - HOT_EPOCH) / HOT_GRAVITY
        )
    if sort == "top":
        return float(score)
    if sort == "new":
        return float(ts_created)
    # rising
    ts_now = _unix_seconds(now)
    hours = max((ts_now - ts_created) / 3600.0, 0.0)
    return score / math.pow(hours + 2, 1.8)


def rising_filter(now: datetime | None = None) -> ColumnElement:
    """Inclusive lower bound for the rising window: created_at >= now - 24h.

    A post created exactly RISING_WINDOW_HOURS hours ago is INCLUDED (>=),
    matching ``hours_since_post <= RISING_WINDOW_HOURS`` at the window edge;
    only strictly older posts are excluded.
    """
    now = now or datetime.now(UTC)
    return Post.created_at >= now - timedelta(hours=RISING_WINDOW_HOURS)


def wilson_lower_bound(up: int, down: int, z: float = WILSON_Z) -> float:
    """Wilson score interval lower bound at z=1.96 over up/(up+down).

    Returns the 0.5 prior when n == 0 so brand-new comments cold-start at the
    neutral prior instead of ranking by absence of evidence.
    """
    n = up + down
    if n == 0:
        return 0.5
    phat = up / n
    z2 = z * z
    centre = phat + z2 / (2 * n)
    margin = z * math.sqrt(phat * (1 - phat) / n + z2 / (4 * n * n))
    return (centre - margin) / (1 + z2 / n)


def up_down_split(score: int, vote_count: int) -> tuple[int, int]:
    """Derive (up, down) from the denormalized pair (score, vote_count).

    ``up = (vote_count + score) // 2``; floor division absorbs odd parity
    (one extra vote rounds into the majority direction), keeping
    ``up + down == vote_count`` always.
    """
    up = (vote_count + score) // 2
    return up, vote_count - up


def controversy(up: int, down: int) -> int:
    """Controversy-lite: min(up, down). High engagement both ways floats up."""
    return min(up, down)
