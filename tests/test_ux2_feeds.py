"""UX-2 feed routes slice: sort whitelist, paging, rails, community context.

Tests capture the actual ``render_template`` context via the
``template_rendered`` signal so they assert the route contract (context vars,
ordering) independently of template markup, which is being redesigned
concurrently.
"""

import math

import pytest
from flask import template_rendered

from deaddit.models import Comment, Post, Subdeaddit, User

TOTAL_POSTS = 50
PER_PAGE = 20


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
    """Deterministic feed: 2 subs, 3 users, TOTAL_POSTS posts.

    Post i (i = 0..N-1): created_at ascending with i, upvote_count = i % 7,
    so default (newest first) order is reversed id order while 'top' sorts by
    upvote count -- visibly different orders.
    """
    from datetime import datetime, timedelta

    base = datetime(2026, 8, 24, 12, 0, 0)
    users = [
        User(username=f"u{i}", bio="b", interests="[]") for i in range(3)
    ]
    subs = [
        Subdeaddit(name="alpha", description="Alpha sub"),
        Subdeaddit(name="beta", description="Beta sub"),
    ]
    posts = []
    for i in range(TOTAL_POSTS):
        # 40 posts in alpha, 10 in beta; alpha gets most comments too.
        sub_name = "alpha" if i < 40 else "beta"
        posts.append(
            Post(
                title=f"Post {i:03d}",
                content=f"body {i}",
                user=f"u{i % 3}",
                subdeaddit_name=sub_name,
                model=f"model-{i % 2}",
                upvote_count=i % 7,
                created_at=base + timedelta(minutes=i),
            )
        )

    from deaddit import db as _db

    _db.session.add_all(users + subs + posts)
    _db.session.flush()

    # Comments spread over the first 20 alpha posts so sub_comment_count != 0.
    comments = [
        Comment(post_id=posts[i].id, content=f"c{i}", user="u0", model="m")
        for i in range(20)
    ]
    _db.session.add_all(comments)
    _db.session.commit()

    return {"posts": posts}

def _index_ctx(ctx):
    matches = [c for c in ctx if c["name"] == "index.html"]
    assert matches, f"index.html never rendered; got {[c['name'] for c in ctx]}"
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
        # 50 posts / 20 per page => 20 + 20 + 10, no duplicates anywhere.
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
    def test_default_order_is_new(self, app, ctx, feed_db):
        client = app.test_client()
        client.get("/")
        context = _index_ctx(ctx)
        assert context["sort"] == ""
        ids = [p.id for p in context["posts"]]
        assert ids == sorted(ids, reverse=True)

    def test_sort_new_explicitly_same_as_default(self, app, ctx, feed_db):
        client = app.test_client()
        client.get("/?sort=new")
        context = _index_ctx(ctx)
        assert context["sort"] == "new"
        ids = [p.id for p in context["posts"]]
        assert ids == sorted(ids, reverse=True)

    def test_sort_top_differs_from_default(self, app, ctx, feed_db):
        client = app.test_client()
        client.get("/?sort=top")
        context = _index_ctx(ctx)
        assert context["sort"] == "top"
        ids = [p.id for p in context["posts"]]
        scores = {p.id: p.upvote_count for p in feed_db["posts"]}
        page_scores = [scores[i] for i in ids]
        # Non-increasing score sequence, and not merely reversed-id order.
        assert page_scores == sorted(page_scores, reverse=True)
        assert ids != sorted(ids, reverse=True)

    @pytest.mark.parametrize("garbage", ["hot", "TOP", "'; DROP TABLE", "top%00"])
    def test_unknown_sort_falls_back_to_default(self, app, ctx, feed_db, garbage):
        client = app.test_client()
        client.get(f"/?sort={garbage}")
        context = _index_ctx(ctx)
        assert context["sort"] == ""
        ids = [p.id for p in context["posts"]]
        assert ids == sorted(ids, reverse=True)


class TestRails:
    def test_rail_subs_top6_by_post_count(self, app, ctx, feed_db):
        # Give beta more posts than alpha so ordering must flip.
        extra = [
            Post(
                title=f"Beta filler {i}",
                content="x",
                user="u0",
                subdeaddit_name="beta",
                model="m",
                upvote_count=0,
            )
            for i in range(45)
        ]
        gamma = Subdeaddit(name="gamma", description="Empty sub")
        # Pad with empty subs so the top-6 cap is actually exercised.
        filler_subs = [
            Subdeaddit(name=f"fill{i}", description=f"filler {i}")
            for i in range(5)
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
        # Zero-post communities still listed via outerjoin, filling the tail
        # (name-ordered tie-break); with 8 zero-post subs only some fit.
        zero_rows = [row for row in rail_subs if row["post_count"] == 0]
        assert len(zero_rows) >= 1
        assert all(row["post_count"] == 0 for row in rail_subs[2:])
        assert len({row["name"] for row in rail_subs}) == 6

    def test_rail_users_top6_by_post_count(self, app, ctx, feed_db):
        # u0 authored ~1/3 of posts; add many posts for u2 to make u2 top.
        extra = [
            Post(
                title=f"U2 spam {i}",
                content="x",
                user="u2",
                subdeaddit_name="alpha",
                model="m",
                upvote_count=0,
            )
            for i in range(30)
        ]
        lonely = User(username="lonely", bio="", interests="[]")
        # Pad with zero-post users so the top-6 cap is actually exercised.
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
        for row in rail_users:
            assert set(row) == {"username", "post_count"}
        assert any(row["username"] == "lonely" and row["post_count"] == 0 for row in rail_users)


class TestSubdeadditContext:
    def test_community_context_vars(self, app, ctx, feed_db):
        client = app.test_client()
        client.get("/d/alpha")
        matches = [c for c in ctx if c["name"] == "subdeaddit.html"]
        assert matches, "subdeaddit.html never rendered"
        context = matches[0]["context"]
        assert context["community"].name == "alpha"
        assert context["sub_post_count"] == 40
        # 20 comments seeded on alpha posts (post_id < 20).
        assert context["sub_comment_count"] == 20
        assert context["total_pages"] == math.ceil(40 / 10)
        assert context["sort"] == ""

    def test_subdeaddit_sort_top_reorders(self, app, ctx, feed_db):
        client = app.test_client()
        client.get("/d/alpha?sort=top")
        context = [c for c in ctx if c["name"] == "subdeaddit.html"][0]["context"]
        assert context["sort"] == "top"
        scores = [p.upvote_count for p in context["posts"]]
        assert scores == sorted(scores, reverse=True)

    def test_subdeaddit_garbage_sort_defaults(self, app, ctx, feed_db):
        client = app.test_client()
        client.get("/d/alpha?sort=nonsense")
        context = [c for c in ctx if c["name"] == "subdeaddit.html"][0]["context"]
        assert context["sort"] == ""
