"""Unit tests for deaddit/dynamics/ranking.py (platform-dynamics.md §3)."""

from __future__ import annotations

import calendar
import math
from datetime import UTC, datetime, timedelta

import pytest

from deaddit.dynamics.ranking import (
    HOT_EPOCH,
    HOT_GRAVITY,
    RISING_WINDOW_HOURS,
    WILSON_Z,
    controversy,
    normalize_comment_sort,
    normalize_post_sort,
    post_order_by,
    post_rank_key,
    rising_filter,
    up_down_split,
    wilson_lower_bound,
)
from deaddit.models import Post, Subdeaddit, User

EPOCH_DT = datetime(1970, 1, 1) + timedelta(seconds=HOT_EPOCH)
NOW = datetime(2026, 8, 1, 12, 0, 0)


def _unix(dt: datetime) -> int:
    return calendar.timegm(dt.utctimetuple())


# --- hot: golden values ------------------------------------------------------


def test_hot_golden_values():
    # At the hot epoch the time term vanishes; only the signed log term remains.
    assert post_rank_key("hot", score=10, created_at=EPOCH_DT, now=NOW) == 1.0
    assert post_rank_key("hot", score=1, created_at=EPOCH_DT, now=NOW) == 0.0
    assert post_rank_key("hot", score=-10, created_at=EPOCH_DT, now=NOW) == -1.0
    # One gravity period later, score 1 gains exactly 1.0 -> ties the classic case.
    ts = _unix(EPOCH_DT) + HOT_GRAVITY
    dt = datetime(1970, 1, 1) + timedelta(seconds=ts)
    assert post_rank_key("hot", score=1, created_at=dt, now=NOW) == 1.0


def test_hot_gravity_tie_exact():
    """Classic time-decay edge: score 100 one gravity older ties score 10 now.

    log10(100) - log10(10) == 1.0 == one HOT_GRAVITY period, and because both
    timestamps are exact multiples of HOT_GRAVITY past the epoch the floating
    point keys are bit-identical (3.0), so only id DESC can break the tie.
    """
    key_older_high = post_rank_key(
        "hot",
        score=100,
        created_at=EPOCH_DT + timedelta(seconds=HOT_GRAVITY),
        now=NOW,
    )
    key_fresh_low = post_rank_key(
        "hot",
        score=10,
        created_at=EPOCH_DT + timedelta(seconds=2 * HOT_GRAVITY),
        now=NOW,
    )
    assert key_older_high == key_fresh_low == 3.0


def test_hot_sign_and_floor_edges():
    # At equal age, negative scores rank below positive ones.
    neg = post_rank_key("hot", score=-10, created_at=EPOCH_DT, now=NOW)
    zero = post_rank_key("hot", score=0, created_at=EPOCH_DT, now=NOW)
    pos = post_rank_key("hot", score=10, created_at=EPOCH_DT, now=NOW)
    assert neg < zero < pos
    # max(|s|,1) floor: score 0 and score 1 share the log term (both log10(1)).
    assert (
        post_rank_key("hot", score=0, created_at=EPOCH_DT, now=NOW)
        == post_rank_key("hot", score=1, created_at=EPOCH_DT, now=NOW)
        == 0.0
    )


def test_post_rank_key_truncates_to_whole_seconds():
    """Microseconds are dropped on BOTH created_at and now (strftime parity)."""
    base = post_rank_key("hot", score=5, created_at=EPOCH_DT, now=NOW)
    with_micro_created = post_rank_key(
        "hot", score=5, created_at=EPOCH_DT + timedelta(microseconds=999999), now=NOW
    )
    with_micro_now = post_rank_key(
        "new", score=5, created_at=NOW, now=NOW + timedelta(microseconds=999999)
    )
    plain_new = post_rank_key("new", score=5, created_at=NOW, now=NOW)
    assert with_micro_created == base
    assert with_micro_now == plain_new == float(_unix(NOW))


# --- rising -------------------------------------------------------------------


def test_rising_denominator_growth():
    """Documented inputs: fresh score 50 beats 24h-old score 500.

    fresh:   50 / pow(0 + 2, 1.8) ~= 14.36
    24h old: 500 / pow(24 + 2, 1.8) ~= 1.42
    """
    fresh = NOW
    old = NOW - timedelta(hours=RISING_WINDOW_HOURS)
    k_fresh = post_rank_key("rising", score=50, created_at=fresh, now=NOW)
    k_old = post_rank_key("rising", score=500, created_at=old, now=NOW)
    assert k_fresh == pytest.approx(50 / math.pow(2, 1.8))
    assert k_old == pytest.approx(500 / math.pow(26, 1.8))
    assert k_fresh > k_old


def test_rising_negative_hours_clamped():
    """Future-dated posts (clock skew) clamp hours to 0 instead of boosting."""
    future = NOW + timedelta(hours=1)
    assert post_rank_key(
        "rising", score=8, created_at=future, now=NOW
    ) == pytest.approx(8 / math.pow(2, 1.8))


def test_rising_boundary_exactly_window_is_included(app, db_session):
    """rising_filter uses >= : a post created exactly 24h ago is INCLUDED;
    only strictly older posts drop out of the window."""
    user = User(username="riser")
    sub = Subdeaddit(name="risesub", description="d")
    db_session.add_all([user, sub])
    db_session.commit()

    def mk(title: str, age: timedelta, score: int) -> Post:
        p = Post(
            title=title,
            content="c",
            user="riser",
            subdeaddit_name="risesub",
            model="m",
            score=score,
            created_at=NOW - age,
        )
        db_session.add(p)
        return p

    p_edge = mk("edge", timedelta(hours=RISING_WINDOW_HOURS), 10)
    p_out = mk("out", timedelta(hours=RISING_WINDOW_HOURS, seconds=1), 10)
    db_session.commit()

    ids = {p.id for p in db_session.query(Post).filter(rising_filter(NOW)).all()}
    assert p_edge.id in ids
    assert p_out.id not in ids


def test_rising_sql_order_by_with_bound_now(app, db_session):
    """Exercises the :now-bound TextClause inside order_by empirically."""
    user = User(username="riser2")
    sub = Subdeaddit(name="risesub2", description="d")
    db_session.add_all([user, sub])
    db_session.commit()

    def mk(title: str, age: timedelta, score: int) -> Post:
        p = Post(
            title=title,
            content="c",
            user="riser2",
            subdeaddit_name="risesub2",
            model="m",
            score=score,
            created_at=NOW - age,
        )
        db_session.add(p)
        return p

    p_fresh_high = mk("fresh-high", timedelta(hours=1), 50)
    p_fresh_low = mk("fresh-low", timedelta(minutes=30), 5)
    p_old_huge = mk("old-huge", timedelta(hours=RISING_WINDOW_HOURS + 1), 100000)
    db_session.commit()

    rows = (
        db_session.query(Post)
        .filter(rising_filter(NOW))
        .order_by(*post_order_by("rising", now=NOW))
        .all()
    )
    assert [r.id for r in rows] == [p_fresh_high.id, p_fresh_low.id]
    assert p_old_huge.id not in {r.id for r in rows}


# --- SQL vs python equivalence (hot) -----------------------------------------


def test_hot_sql_matches_python_mirror(app, db_session):
    """20 posts days apart, distinct scores: SQL ORDER BY == sorted(post_rank_key)."""
    user = User(username="smoker")
    sub = Subdeaddit(name="smokesub", description="d")
    db_session.add_all([user, sub])
    db_session.commit()

    posts = []
    for i in range(20):
        # Whole-day age steps keep keys well separated (>= ~1 per step) so
        # fp jitter can never reorder either side.
        posts.append(
            Post(
                title=f"p{i}",
                content="c",
                user="smoker",
                subdeaddit_name="smokesub",
                model="m",
                score=-30 + 13 * i,  # distinct scores, mixed signs
                created_at=NOW - timedelta(days=i),
            )
        )
    db_session.add_all(posts)
    db_session.commit()

    sql_ids = [
        p.id for p in db_session.query(Post).order_by(*post_order_by("hot")).all()
    ]
    by_key = sorted(
        posts,
        key=lambda p: post_rank_key(
            "hot", score=p.score, created_at=p.created_at, now=NOW
        ),
        reverse=True,
    )
    assert sql_ids == [p.id for p in by_key]


def test_hot_sql_real_division_regression(app, db_session):
    """Regression: the SQL fragment must divide by 45000.0 (float), not 45000.

    With integer division SQLite buckets the recency term to whole gravity
    periods, and within one bucket the higher score always wins — silently
    flattening hot's time-decay gradient. Oracle is hardcoded arithmetic,
    independent of ranking.py: A (score 10, fresher by 20000 s) earns a recency
    edge of 20000/45000 ~= 0.4444 which beats B's log10(11)-log10(10) ~=
    0.0414, so A must rank first. Under integer division both share a bucket
    and B would wrongly win. A is inserted first (lower id), so an accidental
    tie cannot be masked by the id DESC tiebreak either.
    """
    user = User(username="divreg")
    sub = Subdeaddit(name="divsub", description="d")
    db_session.add_all([user, sub])
    db_session.commit()
    base = datetime(2026, 8, 20, 12, 0, 0)
    post_a = Post(
        title="a",
        content="c",
        user="divreg",
        subdeaddit_name="divsub",
        model="m",
        score=10,
        created_at=base,
    )
    post_b = Post(
        title="b",
        content="c",
        user="divreg",
        subdeaddit_name="divsub",
        model="m",
        score=11,
        created_at=base - timedelta(seconds=20000),
    )
    db_session.add_all([post_a, post_b])
    db_session.commit()

    ids = [p.id for p in db_session.query(Post).order_by(*post_order_by("hot")).all()]
    assert ids == [post_a.id, post_b.id]


# --- top / new order-by shape -------------------------------------------------


def test_post_order_by_shapes():
    clauses = {s: post_order_by(s, now=NOW) for s in ("hot", "new", "top", "rising")}
    for cs in clauses.values():
        assert isinstance(cs, list) and len(cs) == 2
    assert all(c is not None for c in clauses)


def test_top_and_new_tiebreak_id_desc(app, db_session):
    """Identical (score, created_at) pairs resolve by id DESC under top/new."""
    user = User(username="tiebrk")
    sub = Subdeaddit(name="tiesub", description="d")
    db_session.add_all([user, sub])
    db_session.commit()
    first = Post(
        title="first",
        content="c",
        user="tiebrk",
        subdeaddit_name="tiesub",
        model="m",
        score=7,
        created_at=NOW,
    )
    second = Post(
        title="second",
        content="c",
        user="tiebrk",
        subdeaddit_name="tiesub",
        model="m",
        score=7,
        created_at=NOW,
    )
    db_session.add_all([first, second])
    db_session.commit()

    for sort in ("top", "new"):
        ids = [
            p.id
            for p in db_session.query(Post)
            .filter(Post.subdeaddit_name == "tiesub")
            .order_by(*post_order_by(sort))
            .all()
        ]
        assert ids == [second.id, first.id]


# --- normalization ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "hot"),
        ("", "hot"),
        ("bogus", "hot"),
        ("TOP", "hot"),  # case-sensitive by design
        ("hot", "hot"),
        ("new", "new"),
        ("top", "top"),
        ("rising", "rising"),
    ],
)
def test_normalize_post_sort(raw, expected):
    assert normalize_post_sort(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "top"),
        ("", "top"),
        ("bogus", "top"),
        ("New", "top"),  # case-sensitive by design
        ("top", "top"),
        ("new", "new"),
        ("best", "best"),
        ("controversial", "controversial"),
    ],
)
def test_normalize_comment_sort(raw, expected):
    assert normalize_comment_sort(raw) == expected


# --- Wilson lower bound -------------------------------------------------------


def test_wilson_zero_votes_returns_prior():
    assert wilson_lower_bound(0, 0) == 0.5


def test_wilson_golden_cases():
    # (1,0) has closed form 1/(1+z^2) ~= 0.2066. Wilson's lower bound is
    # floored at 0 by construction: with phat=0 the margin term equals the
    # centre term, so (0,1) lands on exactly 0.0 — the lowest possible score.
    assert wilson_lower_bound(1, 0) == pytest.approx(1 / (1 + WILSON_Z**2))
    assert wilson_lower_bound(1, 0) == pytest.approx(0.2065, abs=1e-3)
    assert wilson_lower_bound(0, 1) == 0.0
    # (5,5) sits below the 0.5 prior — balanced votes earn no trust yet.
    w55 = wilson_lower_bound(5, 5)
    assert w55 == pytest.approx(0.2366, abs=1e-3)
    assert w55 < 0.5


def test_wilson_monotonic_in_upvotes():
    assert (
        wilson_lower_bound(7, 3) > wilson_lower_bound(5, 5) > wilson_lower_bound(3, 7)
    )
    # More evidence at the same ratio tightens toward phat.
    assert wilson_lower_bound(90, 10) > wilson_lower_bound(9, 1)


def test_wilson_custom_z():
    assert wilson_lower_bound(1, 0, z=0.0) == pytest.approx(1.0)


# --- up/down split and controversy -------------------------------------------


@pytest.mark.parametrize(
    ("score", "vote_count"),
    [(65, 70), (0, 0), (3, 5), (-3, 5), (10, 10), (-10, 10), (1, 1), (-1, 1)],
)
def test_up_down_split_round_trip(score, vote_count):
    up, down = up_down_split(score, vote_count)
    assert up + down == vote_count
    # Floor division absorbs odd (vote_count + score) parity: one vote lands
    # in `down`, so the recovered difference may sit one below `score`.
    assert up - down in (score, score - 1)
    assert (up > down) == (score > 0)
    assert controversy(up, down) == min(up, down)


def test_up_down_split_odd_parity_floor():
    # (vote_count + score) odd: floor division puts the spare vote on top.
    assert up_down_split(3, 4) == (3, 1)
    assert up_down_split(1, 2) == (1, 1)


def test_naive_datetimes_treated_as_utc():
    """Naive input must equal the equivalent UTC-aware input (strftime parity)."""
    aware = NOW.replace(tzinfo=UTC)
    assert post_rank_key("hot", score=5, created_at=NOW, now=aware) == post_rank_key(
        "hot", score=5, created_at=aware, now=aware
    )
