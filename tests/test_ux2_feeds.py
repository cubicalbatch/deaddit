"""Feed routes, sorting, comment sorts, rails, and query plan tests.

Tests capture the actual ``render_template`` context via the
``template_rendered`` signal so they assert the route contract (context vars,
ordering) independently of template markup.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest
from flask import template_rendered
from sqlalchemy import text as sa_text

from deaddit.dynamics.ranking import (
    HOT_SQL_FRAGMENT,
    post_order_by,
    rising_filter,
    up_down_split,
    wilson_lower_bound,
)
from deaddit.models import Comment, Post, Subdeaddit, User

TOTAL_POSTS = 50
PER_PAGE = 20
BASE_DT = datetime(2026, 8, 24, 12, 0, 0)

POST_SORTS = ("hot", "new", "top", "rising")

COMMENT_SPEC = [
    ("steady", 10, 12),
    ("divided", 0, 10),
    ("grindy", 2, 20),
    ("downbad", -6, 6),
]


@pytest.fixture()
def ctx(app):
    """Record template contexts rendered during a request."""
    recorded: list[dict] = []

    def _record(sender, template, context, **extra):
        recorded.append({"name": template.name, "context": dict(context)})

    template_rendered.connect(_record)
    yield recorded
    template_rendered.disconnect(_record)


@pytest.fixture()
def feed_db(app):
    """Deterministic feed: 2 subs, 3 users, TOTAL_POSTS posts with distinct timestamps."""
    from deaddit import db as _db

    users = [User(username=f"u{i}", bio="b", interests="[]") for i in range(3)]
    subs = [
        Subdeaddit(name="alpha", description="Alpha sub"),
        Subdeaddit(name="beta", description="Beta sub"),
    ]
    posts = []
    now = datetime.utcnow()
    for i in range(TOTAL_POSTS):
        sub_name = "alpha" if i < 40 else "beta"
        created_at = (
            now - timedelta(hours=i) if i < 8 else BASE_DT + timedelta(minutes=i)
        )
        posts.append(
            Post(
                title=f"Post {i:03d}",
                content=f"body {i}",
                user=f"u{i % 3}",
                subdeaddit_name=sub_name,
                model=f"model-{i % 2}",
                score=i % 7,
                vote_count=(i % 7) + 1,
                created_at=created_at,
            )
        )

    _db.session.add_all(users + subs + posts)
    _db.session.flush()

    comments = [
        Comment(post_id=posts[i].id, content=f"c{i}", user="u0", model="m")
        for i in range(20)
    ]
    _db.session.add_all(comments)
    _db.session.commit()

    return {"posts": posts}


@pytest.fixture()
def comment_thread(app):
    """Comment-thread fixture with hand-picked vote splits."""
    from deaddit import db as _db

    user = User.query.filter_by(username="u0").first()
    if not user:
        user = User(username="u0", bio="b", interests="[]")
        _db.session.add(user)
    sub = Subdeaddit.query.filter_by(name="alpha").first()
    if not sub:
        sub = Subdeaddit(name="alpha", description="A")
        _db.session.add(sub)

    post = Post(
        title="Thread",
        content="b",
        user="u0",
        subdeaddit_name="alpha",
        model="m",
        score=5,
        vote_count=6,
        created_at=BASE_DT,
    )
    _db.session.add(post)
    _db.session.flush()

    comments = []
    for i, (title, score, votes) in enumerate(COMMENT_SPEC):
        comments.append(
            Comment(
                post_id=post.id,
                content=title,
                user="u0",
                model="m",
                score=score,
                vote_count=votes,
                created_at=BASE_DT + timedelta(hours=i),
            )
        )
    _db.session.add_all(comments)
    _db.session.commit()
    return {"post_id": post.id}


@pytest.fixture()
def ranking_indexes(app):
    """Create feed indexes for EXPLAIN QUERY PLAN testing."""
    from deaddit.extensions import db

    db.session.execute(
        sa_text("CREATE INDEX IF NOT EXISTS ix_post_score ON post (score)")
    )
    db.session.execute(
        sa_text(
            f"CREATE INDEX IF NOT EXISTS ix_post_hot_expr ON post (({HOT_SQL_FRAGMENT}))"
        )
    )
    db.session.commit()
    yield


def _index_ctx(ctx):
    matches = [c for c in ctx if c["name"] == "index.html"]
    assert matches, f"index.html never rendered; got {[c['name'] for c in ctx]}"
    return matches[-1]["context"]


def _sub_ctx(ctx):
    matches = [c for c in ctx if c["name"] == "subdeaddit.html"]
    assert matches, f"subdeaddit.html never rendered; got {[c['name'] for c in ctx]}"
    return matches[-1]["context"]


class TestIndexPaging:
    def test_pages_partition_posts_with_zero_repeats(self, app, ctx, feed_db):
        client = app.test_client()
        seen: list[int] = []
        for page in (1, 2, 3):
            assert client.get(f"/?page={page}").status_code == 200
            ids = [p.id for p in _index_ctx(ctx)["posts"]]
            seen.extend(ids)
            assert len(ids) <= PER_PAGE
        assert len(seen) == TOTAL_POSTS
        assert len(set(seen)) == TOTAL_POSTS

    def test_total_pages_math(self, app, ctx, feed_db):
        client = app.test_client()
        client.get("/")
        expected = math.ceil(TOTAL_POSTS / PER_PAGE)
        assert _index_ctx(ctx)["total_pages"] == expected

        last_page = math.ceil(TOTAL_POSTS / PER_PAGE)
        client.get(f"/?page={last_page}")
        assert len(_index_ctx(ctx)["posts"]) == TOTAL_POSTS % PER_PAGE


class TestIndexSort:
    @staticmethod
    def _hot_bucket_ids(posts):
        order = sorted(
            posts,
            key=lambda p: (
                -(math.log10(p.score) if p.score > 0 else 0.0),
                -p.id,
            ),
        )
        return [p.id for p in order]

    def test_default_order_is_hot(self, app, ctx, feed_db):
        client = app.test_client()
        client.get("/")
        context = _index_ctx(ctx)
        assert context["sort"] == "hot"
        ids = [p.id for p in context["posts"]]
        assert len(ids) == PER_PAGE

    def test_sort_new_is_plain_recency(self, app, ctx, feed_db):
        client = app.test_client()
        client.get("/?sort=new")
        context = _index_ctx(ctx)
        assert context["sort"] == "new"
        posts = context["posts"]
        created_ats = [p.created_at for p in posts]
        assert created_ats == sorted(created_ats, reverse=True)

    def test_sort_top_orders_by_score(self, app, ctx, feed_db):
        from deaddit import db as _db

        client = app.test_client()
        for i, post in enumerate(feed_db["posts"]):
            post.score = (i * 13) % 50
        _db.session.commit()

        client.get("/?sort=top")
        context = _index_ctx(ctx)
        assert context["sort"] == "top"
        scores = [p.score for p in context["posts"]]
        assert scores == sorted(scores, reverse=True)

    def test_sort_rising_filters_to_recent_window(self, app, ctx, feed_db):
        client = app.test_client()
        client.get("/?sort=rising")
        context = _index_ctx(ctx)
        assert context["sort"] == "rising"
        cutoff = datetime.utcnow() - timedelta(hours=24)
        for post in context["posts"]:
            assert post.created_at >= cutoff

    @pytest.mark.parametrize("garbage", ["TOP", "'; DROP TABLE", "top%00", "best"])
    def test_unknown_sort_falls_back_to_hot(self, app, ctx, feed_db, garbage):
        client = app.test_client()
        client.get(f"/?sort={garbage}")
        context = _index_ctx(ctx)
        assert context["sort"] == "hot"


class TestInsertStability:
    def test_new_posts_do_not_displace_seen_items(self, app, ctx, feed_db):
        from deaddit import db as _db

        client = app.test_client()
        client.get("/?page=1&sort=new")

        now = datetime.utcnow() + timedelta(hours=10)
        fresh = [
            Post(
                title=f"Fresh {i}",
                content="x",
                user="u0",
                subdeaddit_name="alpha",
                model="m",
                score=100 + i,
                vote_count=100 + i,
                created_at=now + timedelta(minutes=i),
            )
            for i in range(3)
        ]
        _db.session.add_all(fresh)
        _db.session.commit()

        client.get("/?page=1&sort=new")
        new_p1_ids = {p.id for p in _index_ctx(ctx)["posts"]}
        fresh_ids = {p.id for p in fresh}
        assert fresh_ids <= new_p1_ids


class TestRails:
    def test_rail_subs_top6_by_post_count(self, app, ctx, feed_db):
        extra = [
            Post(
                title=f"Beta filler {i}",
                content="x",
                user="u0",
                subdeaddit_name="beta",
                model="m",
                score=0,
            )
            for i in range(45)
        ]
        gamma = Subdeaddit(name="gamma", description="Empty sub")
        filler_subs = [
            Subdeaddit(name=f"fill{i}", description=f"filler {i}") for i in range(5)
        ]
        from deaddit import db as _db

        _db.session.add_all(extra + [gamma] + filler_subs)
        _db.session.commit()

        client = app.test_client()
        client.get("/")
        context = _index_ctx(ctx)
        rail_subs = context["rail_subs"]
        assert len(rail_subs) == 6
        counts = [row["post_count"] for row in rail_subs]
        assert counts == sorted(counts, reverse=True)
        assert rail_subs[0]["name"] == "beta"
        assert rail_subs[0]["post_count"] == 55
        assert rail_subs[1]["name"] == "alpha"
        assert rail_subs[1]["post_count"] == 40

    def test_rail_users_top6_by_post_count(self, app, ctx, feed_db):
        extra = [
            Post(
                title=f"U2 spam {i}",
                content="x",
                user="u2",
                subdeaddit_name="alpha",
                model="m",
                score=0,
            )
            for i in range(30)
        ]
        lonely = User(username="lonely", bio="", interests="[]")
        filler_users = [
            User(username=f"quiet{i}", bio="", interests="[]") for i in range(5)
        ]
        from deaddit import db as _db

        _db.session.add_all(extra + [lonely] + filler_users)
        _db.session.commit()

        client = app.test_client()
        client.get("/")
        context = _index_ctx(ctx)
        rail_users = context["rail_users"]
        assert len(rail_users) == 6
        counts = [row["post_count"] for row in rail_users]
        assert counts == sorted(counts, reverse=True)
        assert rail_users[0]["username"] == "u2"


class TestSubdeadditContext:
    def test_community_context_vars(self, app, ctx, feed_db):
        client = app.test_client()
        client.get("/d/alpha")
        context = _sub_ctx(ctx)
        assert context["community"].name == "alpha"
        assert context["sub_post_count"] == 40
        assert context["sub_comment_count"] == 20
        assert context["total_pages"] == math.ceil(40 / 10)
        assert context["sort"] == "hot"

    def test_subdeaddit_sort_top_reorders(self, app, ctx, feed_db):
        from deaddit import db as _db

        client = app.test_client()
        for i, post in enumerate(feed_db["posts"]):
            post.score = (i * 13) % 50
        _db.session.commit()

        client.get("/d/alpha?sort=top")
        context = _sub_ctx(ctx)
        assert context["sort"] == "top"
        scores = [p.score for p in context["posts"]]
        assert scores == sorted(scores, reverse=True)

    def test_subdeaddit_garbage_sort_defaults_to_hot(self, app, ctx, feed_db):
        client = app.test_client()
        client.get("/d/alpha?sort=nonsense")
        context = _sub_ctx(ctx)
        assert context["sort"] == "hot"


class TestCommentSorts:
    def _tree_ids(self, app, ctx, thread, query=""):
        client = app.test_client()
        resp = client.get(f"/d/alpha/{thread['post_id']}{query}")
        assert resp.status_code == 200
        matches = [c for c in ctx if c["name"] == "post.html"]
        assert matches
        tree = matches[-1]["context"]["comment_tree"]
        return [node["content"] for node in tree]

    def test_top_order(self, app, ctx, comment_thread):
        assert self._tree_ids(app, ctx, comment_thread, "?sort=top") == [
            "steady",
            "grindy",
            "divided",
            "downbad",
        ]

    def test_default_is_top(self, app, ctx, comment_thread):
        assert self._tree_ids(app, ctx, comment_thread) == [
            "steady",
            "grindy",
            "divided",
            "downbad",
        ]

    def test_garbage_comment_sort_falls_back_to_top(self, app, ctx, comment_thread):
        assert self._tree_ids(app, ctx, comment_thread, "?sort=bogus") == [
            "steady",
            "grindy",
            "divided",
            "downbad",
        ]

    def test_new_order(self, app, ctx, comment_thread):
        assert self._tree_ids(app, ctx, comment_thread, "?sort=new") == [
            "downbad",
            "grindy",
            "divided",
            "steady",
        ]

    def test_best_orders_by_wilson(self, app, ctx, comment_thread):
        splits = {
            title: up_down_split(score, votes) for title, score, votes in COMMENT_SPEC
        }
        wilsons = {t: wilson_lower_bound(up, down) for t, (up, down) in splits.items()}
        assert (
            wilsons["steady"]
            > wilsons["grindy"]
            > wilsons["divided"]
            > wilsons["downbad"]
        )
        assert self._tree_ids(app, ctx, comment_thread, "?sort=best") == [
            "steady",
            "grindy",
            "divided",
            "downbad",
        ]

    def test_controversial_orders_by_min_up_down(self, app, ctx, comment_thread):
        assert self._tree_ids(app, ctx, comment_thread, "?sort=controversial") == [
            "grindy",
            "divided",
            "steady",
            "downbad",
        ]


class TestQueryPlans:
    @pytest.mark.parametrize("sort", POST_SORTS)
    def test_no_bare_table_scan(self, app, feed_db, ranking_indexes, sort):
        from deaddit.extensions import db

        q = db.session.query(Post)
        if sort == "rising":
            q = q.filter(rising_filter())
        q = q.order_by(*post_order_by(sort))

        sql = str(
            q.statement.compile(
                dialect=db.engine.dialect,
                compile_kwargs={"literal_binds": True},
            )
        )
        rows = db.session.execute(sa_text(f"EXPLAIN QUERY PLAN {sql}")).fetchall()
        lines = [row[3] for row in rows]
        for line in lines:
            ok = (
                "USING INDEX" in line
                or line.startswith("SEARCH")
                or "TEMP B-TREE" in line
            )
            assert ok, f"bare scan in plan:\n{chr(10).join(lines)}"
