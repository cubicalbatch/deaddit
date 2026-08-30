"""Anti-degeneracy detectors, demotion, and watchlist (Phase D6, plan §7).

Detectors (cheap, local):

- **Repetition** — char-trigram Jaccard of new content against the author's
  last ``HISTORY_K=10`` contents and against up to ``THREAD_SAMPLE=50``
  recent thread contents. Overlap ``> REPETITION_THRESHOLD (0.6)`` inserts a
  ``DegeneracyFlag(kind='repetition')`` row. Hooked into the content service
  AFTER commit; detection failure can never fail content creation.
- **Echo chambers** — nightly per-subdeaddit Gini of participation over a
  trailing window; high-Gini subs are flagged (detection + watchlist).
- **Brigading** — nightly voter-overlap between co-voting pairs; detection
  only (plan: manual bans are the remedy).

Levers:

- **Ranking demotion** — posts whose author carries a repetition flag inside
  the demotion window are ordered at half hot weight (``×0.5``). The formula
  module :mod:`deaddit.dynamics.ranking` stays byte-frozen; this module wraps
  its output at query-composition time (see :func:`with_repetition_demotion`).
  With no active flags the ORDER BY text is IDENTICAL to the plain hot
  fragment, so EXPLAIN QUERY PLAN keeps using ``ix_post_hot_expr``. While
  flags are active the CASE wrapper defeats that index — accepted at this
  scale (≤2k posts), documented here for the EQP house convention.
- **Per-user rate limits** live in the creation service itself
  (:mod:`deaddit.services.content`) because they must gate persistence.

Raw-SQL spot-checks the dashboards must agree with (acceptance criterion)::

    -- Watchlist rows equal:
    SELECT kind, username, subdeaddit_name, metric, created_at
      FROM degeneracy_flag ORDER BY created_at DESC LIMIT 50;

    -- The hot-demotion author set equals (window W =
    -- Setting degeneracy_demotion_window_days, default 7):
    SELECT DISTINCT username FROM degeneracy_flag
     WHERE kind = 'repetition' AND username IS NOT NULL
       AND created_at >= datetime('now', '-' || :W || ' days');

    -- Echo-chamber subs equal (Gini over per-user post+comment counts in
    -- the trailing scan window, per sub with >= 3 participants):
    SELECT s.name FROM subdeaddit s WHERE <gini of
        SELECT user, COUNT(*) FROM (
            SELECT user FROM post WHERE subdeaddit_name = s.name
              AND created_at >= :cutoff
            UNION ALL
            SELECT user FROM comment c JOIN post p ON c.post_id = p.id
             WHERE p.subdeaddit_name = s.name AND c.created_at >= :cutoff)
        GROUP BY user >= 0.7>;

All thresholds are plan-fixed constants except the demotion window, which is
Setting-tunable. Flag writes go through their own short transactions and are
idempotent per target (a re-detection of the same item does not duplicate).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import TextClause, case, func, literal_column

from deaddit.dynamics.metrics import gini_coefficient
from deaddit.dynamics.ranking import HOT_SQL_FRAGMENT
from deaddit.extensions import db
from deaddit.models import Comment, DegeneracyFlag, Post, Setting, Vote

logger = logging.getLogger(__name__)

#: Plan §7: ">0.6 overlap flags".
REPETITION_THRESHOLD = 0.6
#: Plan §7: "vs author's last K=10 contents".
HISTORY_K = 10
#: Upper bound on thread contents compared against (scale guard).
THREAD_SAMPLE = 50
#: How long a repetition flag keeps demoting its author's posts in hot feeds.
DEMOTION_WINDOW_DAYS_DEFAULT = 7
_SETTING_DEMOTION_WINDOW = "degeneracy_demotion_window_days"

_ECHO_WINDOW_DAYS = 7
_ECHO_GINI_THRESHOLD = 0.7
_ECHO_MIN_PARTICIPANTS = 3

_BRIGADE_WINDOW_DAYS = 7
_BRIGADE_OVERLAP_THRESHOLD = 0.8
_BRIGADE_MIN_SHARED = 10
_BRIGADE_MAX_VOTERS = 200

_WS_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------
def normalize_text(value: str | None) -> str:
    """Lowercase, whitespace-collapsed text used for trigram extraction."""
    return _WS_RE.sub(" ", (value or "").strip().lower())


def trigrams(value: str | None) -> set[str]:
    """Character trigrams of normalized text; empty for len < 3."""
    text = normalize_text(value)
    if len(text) < 3:
        return set()
    return {text[i : i + 3] for i in range(len(text) - 2)}


def trigram_jaccard(a: str | None, b: str | None) -> float:
    """Jaccard overlap of the two texts' trigram sets; 0.0 when either empty."""
    set_a, set_b = trigrams(a), trigrams(b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


# --------------------------------------------------------------------------
# Repetition detector (hooked from services/content after commit)
# --------------------------------------------------------------------------
def _recent_contents(query, exclude_id: int | None, limit: int) -> list[str]:
    q = query
    if exclude_id is not None:
        q = q.filter(Comment.id != exclude_id)
    rows = q.order_by(Comment.created_at.desc(), Comment.id.desc()).limit(limit)
    return [(row.content or "") for row in rows]


def _subdeaddit_of_post(post_id: int | None) -> str | None:
    if post_id is None:
        return None
    row = db.session.get(Post, post_id)
    return row.subdeaddit_name if row else None


def detect_repetition_for_comment(comment: Comment) -> DegeneracyFlag | None:
    """Compare a committed comment vs author history and its thread.

    Inserts and commits one ``DegeneracyFlag`` when any comparison exceeds
    the threshold. Idempotent per comment (an existing flag for the same
    comment short-circuits). Never raises (isolation contract).
    """
    try:
        existing = (
            db.session.query(DegeneracyFlag.id)
            .filter(
                DegeneracyFlag.kind == "repetition",
                DegeneracyFlag.comment_id == comment.id,
            )
            .first()
        )
        if existing is not None:
            return None

        author_history = _recent_contents(
            Comment.query.filter(Comment.user == comment.user),
            exclude_id=comment.id,
            limit=HISTORY_K,
        )
        thread_contents = _recent_contents(
            Comment.query.filter(Comment.post_id == comment.post_id),
            exclude_id=comment.id,
            limit=THREAD_SAMPLE,
        )
        best, best_against = 0.0, ""
        for other in [*author_history, *thread_contents]:
            score = trigram_jaccard(comment.content, other)
            if score > best:
                best, best_against = score, other[:200]
        if best <= REPETITION_THRESHOLD:
            return None

        flag = DegeneracyFlag(
            kind="repetition",
            username=comment.user,
            subdeaddit_name=_subdeaddit_of_post(comment.post_id),
            post_id=comment.post_id,
            comment_id=comment.id,
            metric=best,
            detail=json.dumps(
                {
                    "threshold": REPETITION_THRESHOLD,
                    "history_size": len(author_history),
                    "thread_size": len(thread_contents),
                    "matched": best_against,
                }
            ),
        )
        db.session.add(flag)
        db.session.commit()
        logger.info(
            "degeneracy: repetition flag on comment %s by %s (jaccard %.2f)",
            comment.id,
            comment.user,
            best,
        )
        return flag
    except Exception:  # noqa: BLE001 - isolation contract, see module docstring
        db.session.rollback()
        logger.warning(
            "repetition detection failed for comment %s", comment.id, exc_info=True
        )
        return None


def detect_repetition_for_post(post: Post) -> DegeneracyFlag | None:
    """Compare a committed post vs the author's last K post bodies."""
    try:
        existing = (
            db.session.query(DegeneracyFlag.id)
            .filter(
                DegeneracyFlag.kind == "repetition",
                DegeneracyFlag.post_id == post.id,
            )
            .first()
        )
        if existing is not None:
            return None

        mine = post.title and (post.title + "\n" + (post.content or ""))
        rows = (
            Post.query.filter(Post.user == post.user, Post.id != post.id)
            .order_by(Post.created_at.desc(), Post.id.desc())
            .limit(HISTORY_K)
            .all()
        )
        best, best_against = 0.0, ""
        for other in rows:
            score = trigram_jaccard(
                mine, (other.title or "") + "\n" + (other.content or "")
            )
            if score > best:
                best, best_against = score, (other.title or "")[:200]
        if best <= REPETITION_THRESHOLD:
            return None

        flag = DegeneracyFlag(
            kind="repetition",
            username=post.user,
            subdeaddit_name=post.subdeaddit_name,
            post_id=post.id,
            comment_id=None,
            metric=best,
            detail=json.dumps(
                {"threshold": REPETITION_THRESHOLD, "matched": best_against}
            ),
        )
        db.session.add(flag)
        db.session.commit()
        logger.info(
            "degeneracy: repetition flag on post %s by %s (jaccard %.2f)",
            post.id,
            post.user,
            best,
        )
        return flag
    except Exception:  # noqa: BLE001 - isolation contract
        db.session.rollback()
        logger.warning(
            "repetition detection failed for post %s", post.id, exc_info=True
        )
        return None


# --------------------------------------------------------------------------
# Demotion lever (query-composition seam; ranking.py stays frozen)
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
def demotion_window_cutoff(now: datetime | None = None) -> datetime:
    """Start of the active demotion window (Setting-tunable, default 7d)."""
    raw = Setting.get_value(_SETTING_DEMOTION_WINDOW, str(DEMOTION_WINDOW_DAYS_DEFAULT))
    try:
        days = max(int(str(raw)), 0)
    except (TypeError, ValueError):
        days = DEMOTION_WINDOW_DAYS_DEFAULT
    return (now or datetime.utcnow()) - timedelta(days=days)


def flagged_hot_authors(cutoff: datetime | None = None) -> list[str]:
    """Distinct authors with an active repetition flag (the demotion set)."""
    if cutoff is None:
        cutoff = demotion_window_cutoff()
    rows = (
        db.session.query(DegeneracyFlag.username)
        .filter(
            DegeneracyFlag.kind == "repetition",
            DegeneracyFlag.username.isnot(None),
            DegeneracyFlag.created_at >= cutoff,
        )
        .distinct()
        .all()
    )
    return sorted({row[0] for row in rows})


def with_repetition_demotion(order_clauses: list) -> list:
    """Wrap a ``ranking.post_order_by`` result with the ×0.5 hot demotion.

    Fast path: no active flags → the input clauses are returned UNCHANGED
    (byte-identical SQL, so the D2 expression index keeps serving EXPLAIN).
    Only the first clause (hot) is ever wrapped; tiebreaks pass through.
    """
    # Only the exact hot fragment is ever wrapped; new/top/rising (rising is
    # itself a bound TextClause) pass through untouched while demotions are on.
    if not order_clauses or not (
        isinstance(order_clauses[0], TextClause)
        and order_clauses[0].text == HOT_SQL_FRAGMENT + " DESC"
    ):
        return order_clauses
    authors = flagged_hot_authors()
    if not authors:
        return order_clauses
    factor = case((Post.user.in_(authors), 0.5), else_=1.0)
    demoted_hot = (factor * literal_column(HOT_SQL_FRAGMENT)).desc()
    return [demoted_hot, *order_clauses[1:]]


def hot_rank_key_demoted(
    *, score: int, created_at: datetime, now: datetime, demoted: bool
) -> float:
    """Test-only mirror of the demoted hot rank key: ``0.5 ×`` the frozen mirror.

    Used by tests to assert numerical parity against SQL with_repetition_demotion.
    """
    from deaddit.dynamics.ranking import post_rank_key

    key = post_rank_key("hot", score=score, created_at=created_at, now=now)
    return key * 0.5 if demoted else key


# --------------------------------------------------------------------------
# Nightly community scans (echo chambers, brigading)
# --------------------------------------------------------------------------
def _participation_by_user(sub: str, cutoff: datetime) -> dict[str, int]:
    post_rows = (
        db.session.query(Post.user, func.count(Post.id))
        .filter(Post.subdeaddit_name == sub, Post.created_at >= cutoff)
        .group_by(Post.user)
    )
    comment_rows = (
        db.session.query(Post.user, func.count(Comment.id))
        .join(Comment, Comment.post_id == Post.id)
        .filter(Post.subdeaddit_name == sub, Comment.created_at >= cutoff)
        .group_by(Post.user)
    )
    counts: dict[str, int] = {}
    for user, n in post_rows:
        counts[user] = counts.get(user, 0) + int(n)
    for user, n in comment_rows:
        counts[user] = counts.get(user, 0) + int(n)
    return counts


def _has_recent_flag(kind: str, *, username: str | None, sub: str | None) -> bool:
    cutoff = datetime.utcnow() - timedelta(hours=24)
    q = db.session.query(DegeneracyFlag.id).filter(
        DegeneracyFlag.kind == kind, DegeneracyFlag.created_at >= cutoff
    )
    if username is not None:
        q = q.filter(DegeneracyFlag.username == username)
    else:
        q = q.filter(DegeneracyFlag.username.is_(None))
    if sub is not None:
        q = q.filter(DegeneracyFlag.subdeaddit_name == sub)
    else:
        q = q.filter(DegeneracyFlag.subdeaddit_name.is_(None))
    return q.first() is not None


def scan_echo_chambers(now: datetime | None = None) -> int:
    """Nightly: flag subs whose trailing-window participation Gini is extreme."""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=_ECHO_WINDOW_DAYS)
    subs = [row[0] for row in db.session.query(Post.subdeaddit_name).distinct()]
    flagged = 0
    for sub in subs:
        counts = _participation_by_user(sub, cutoff)
        if len(counts) < _ECHO_MIN_PARTICIPANTS:
            continue
        value = gini_coefficient(list(counts.values()))
        if value < _ECHO_GINI_THRESHOLD:
            continue
        if _has_recent_flag("echo_chamber", username=None, sub=sub):
            continue
        db.session.add(
            DegeneracyFlag(
                kind="echo_chamber",
                username=None,
                subdeaddit_name=sub,
                metric=value,
                detail=json.dumps(
                    {
                        "participants": len(counts),
                        "actions": sum(counts.values()),
                        "window_days": _ECHO_WINDOW_DAYS,
                        "threshold": _ECHO_GINI_THRESHOLD,
                    }
                ),
                created_at=now,
            )
        )
        flagged += 1
    db.session.commit()
    return flagged


def scan_brigading(now: datetime | None = None) -> int:
    """Nightly: detection-only flags for suspiciously overlapping voter pairs."""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=_BRIGADE_WINDOW_DAYS)
    voter_col = Vote.voter
    top_voters = [
        row[0]
        for row in db.session.query(voter_col, func.count(Vote.id))
        .filter(Vote.created_at >= cutoff)
        .group_by(voter_col)
        .order_by(func.count(Vote.id).desc())
        .limit(_BRIGADE_MAX_VOTERS)
        .all()
    ]
    if len(top_voters) < 2:
        return 0

    def _target_key(post_id: int | None, comment_id: int | None) -> str:
        if post_id is not None:
            return f"post:{post_id}"
        return f"comment:{comment_id}"

    sets: dict[str, set[str]] = {}
    rows = db.session.query(Vote.voter, Vote.post_id, Vote.comment_id).filter(
        Vote.voter.in_(top_voters), Vote.created_at >= cutoff
    )
    for voter, post_id, comment_id in rows:
        sets.setdefault(voter, set()).add(_target_key(post_id, comment_id))

    flagged = 0
    ordered = sorted(sets)
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            shared = sets[a] & sets[b]
            union = sets[a] | sets[b]
            if len(shared) < _BRIGADE_MIN_SHARED or not union:
                continue
            overlap = len(shared) / len(union)
            if overlap < _BRIGADE_OVERLAP_THRESHOLD:
                continue
            pair_key = "|".join(sorted((a, b)))
            if _has_recent_flag("brigading", username=pair_key, sub=None):
                continue
            db.session.add(
                DegeneracyFlag(
                    kind="brigading",
                    username=pair_key,
                    metric=overlap,
                    detail=json.dumps(
                        {
                            "pair": sorted((a, b)),
                            "shared_targets": len(shared),
                            "window_days": _BRIGADE_WINDOW_DAYS,
                        }
                    ),
                    created_at=now,
                )
            )
            flagged += 1
    db.session.commit()
    return flagged


def run_nightly_scans() -> dict[str, int]:
    """Registered nightly entry point: both community scans, counts returned."""
    echo = scan_echo_chambers()
    brigade = scan_brigading()
    return {"echo_chamber_flags": echo, "brigading_flags": brigade}
