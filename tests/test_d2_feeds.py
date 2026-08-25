"""D2 ranked feeds: hot/new/top/rising over the web routes, comment sorts,
page-walk determinism, insert stability, and EQP index-usage assertions.

Route-level tests on the in-memory conftest fixtures, mirroring the
test_ux2_feeds.py ctx/feed_db pattern. Seeded posts are spread DAYS apart
with distinct scores so second-level created_at jitter cannot reorder.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from flask import template_rendered
from sqlalchemy import text as sa_text

from deaddit.dynamics.ranking import (
    post_order_by,
    post_rank_key,
    rising_filter,
    up_down_split,
    wilson_lower_bound,
)
from deaddit.models import Comment, Post, Subdeaddit, User

INDEX_PER_PAGE = 20
SUB_PER_PAGE = 10
N_OLD = 22
N_RECENT = 8
BASE = datetime(2026, 8, 1, 12, 0, 0)

POST_SORTS = ("hot", "new", "top", "rising")


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
def d2_feed(app):
    """30 alpha posts: N_OLD spread a day apart (varied scores incl. <=0)
    plus N_RECENT inside the rising window, all with distinct scores."""
    from deaddit import db as _db

    _db.session.add_all(
        [
            User(username="u0", bio="b", interests="[]"),
            Subdeaddit(name="alpha", description="Alpha sub"),
        ]
    )
    spec = [
        (BASE + timedelta(days=i), ((i * 7) % 23) - 3) for i in range(N_OLD)
    ]
    now = datetime.utcnow()
    for j in range(N_RECENT):
        spec.append((now - timedelta(hours=2 * (j + 1)), 30 - 3 * j))
    posts = []
    for k, (created_at, score) in enumerate(spec):
        posts.append(
            Post(
                title=f"Post {k:02d}",
                content=f"body {k}",
                user="u0",
                subdeaddit_name="alpha",
                model="m",
                score=score,
                vote_count=abs(score) + 1,
                created_at=created_at,
            )
        )
    _db.session.add_all(posts)
    _db.session.flush()

    rows = [
        {"id": p.id, "score": p.score, "created_at": p.created_at}
        for p in posts
    ]
    _db.session.commit()
    return {"rows": rows}


# Comment-thread fixture: four root comments with hand-picked vote splits.
#   steady : score=10 votes=12 -> up=11 down=1  (wilson ~0.65, controv=1)
#   divided: score= 0 votes=10 -> up=5  down=5  (wilson ~0.24, controv=5)
#   grindy : score= 2 votes=20 -> up=11 down=9  (wilson ~0.34, controv=9)
#   downbad: score=-6 votes=6  -> up=0  down=6  (wilson 0.00, controv=0)
COMMENT_SPEC = [
    ("steady", 10, 12),
    ("divided", 0, 10),
    ("grindy", 2, 20),
    ("downbad", -6, 6),
]


@pytest.fixture()
def comment_thread(app):
    from deaddit import db as _db

    post = Post(
        title="Thread",
        content="b",
        user="u0",
        subdeaddit_name="alpha",
        model="m",
        score=5,
        vote_count=6,
        created_at=BASE,
    )
    _db.session.add_all([User(username="u0", bio="b", interests="[]"),
                         Subdeaddit(name="alpha", description="A"), post])
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
                upvote_count=max(score, 0),
                created_at=BASE + timedelta(hours=i),
            )
        )
    _db.session.add_all(comments)
    _db.session.commit()
    return {"post_id": post.id}


def _ctx_of(ctx, name):
    matches = [c for c in ctx if c["name"] == name]
    assert matches, f"{name} never rendered; got {[c['name'] for c in ctx]}"
    return matches[-1]["context"]


def _expected_ids(rows, sort):
    """Python-mirror expected order over the seeded rows (id DESC ties)."""
    now = datetime.utcnow()
    pool = rows
    if sort == "rising":
        cutoff = now - timedelta(hours=24)
        pool = [r for r in rows if r["created_at"] >= cutoff]

    def key(r):
        return (
            -post_rank_key(
                sort, score=r["score"], created_at=r["created_at"], now=now
            ),
            -r["id"],
        )

    return [r["id"] for r in sorted(pool, key=key)]


def _walk(client, ctx, path, template, per_page):
    seen: list[int] = []
    page = 1
    while page <= 20:
        resp = client.get(path + (f"&page={page}" if "?" in path else f"?page={page}"))
        assert resp.status_code == 200
        posts = _ctx_of(ctx, template)["posts"]
        if not posts:
            break
        ids = [p.id for p in posts]
        assert len(ids) == len(set(ids)), f"duplicate id on page {page}"
        seen.extend(ids)
        if len(posts) < per_page:
            break
        page += 1
    return seen


class TestFeedSorts:
    @pytest.mark.parametrize("sort", POST_SORTS)
    @pytest.mark.parametrize("path,template,per_page", [
        ("/", "index.html", INDEX_PER_PAGE),
        ("/d/alpha", "subdeaddit.html", SUB_PER_PAGE),
    ])
    def test_first_page_ordered_per_sort(self, app, ctx, d2_feed, sort,
                                         path, template, per_page):
        client = app.test_client()
        resp = client.get(f"{path}?sort={sort}")
        assert resp.status_code == 200
        ids = [p.id for p in _ctx_of(ctx, template)["posts"]]
        expected = _expected_ids(d2_feed["rows"], sort)
        assert ids == expected[:per_page]
        assert _ctx_of(ctx, template)["sort"] == sort

    @pytest.mark.parametrize("path,template", [
        ("/", "index.html"),
        ("/d/alpha", "subdeaddit.html"),
    ])
    def test_missing_param_is_hot(self, app, ctx, d2_feed, path, template):
        client = app.test_client()
        client.get(path)
        context = _ctx_of(ctx, template)
        assert context["sort"] == "hot"
        ids = [p.id for p in context["posts"]]
        assert ids == _expected_ids(d2_feed["rows"], "hot")[: len(ids)]

    @pytest.mark.parametrize("garbage", ["bogus", "", "TOP", "'; DROP TABLE"])
    @pytest.mark.parametrize("path,template", [
        ("/", "index.html"),
        ("/d/alpha", "subdeaddit.html"),
    ])
    def test_garbage_falls_back_to_hot(self, app, ctx, d2_feed, garbage,
                                       path, template):
        client = app.test_client()
        sep = "&" if "?" in path else "?"
        url = f"{path}{sep}sort={garbage}" if garbage else path
        client.get(url)
        context = _ctx_of(ctx, template)
        assert context["sort"] == "hot"
        ids = [p.id for p in context["posts"]]
        assert ids == _expected_ids(d2_feed["rows"], "hot")[: len(ids)]

    @pytest.mark.parametrize("sort", POST_SORTS)
    @pytest.mark.parametrize("path,template,per_page", [
        ("/", "index.html", INDEX_PER_PAGE),
        ("/d/alpha", "subdeaddit.html", SUB_PER_PAGE),
    ])
    def test_walk_all_pages_partitions_exactly_once(self, app, ctx, d2_feed,
                                                    sort, path, template,
                                                    per_page):
        client = app.test_client()
        seen = _walk(client, ctx, f"{path}?sort={sort}", template, per_page)
        expected = _expected_ids(d2_feed["rows"], sort)
        assert len(seen) == len(expected)
        assert sorted(seen) == sorted(expected)


class TestInsertStability:
    @pytest.mark.parametrize("sort", POST_SORTS)
    @pytest.mark.parametrize("path,template,per_page", [
        ("/", "index.html", INDEX_PER_PAGE),
        ("/d/alpha", "subdeaddit.html", SUB_PER_PAGE),
    ])
    def test_new_posts_do_not_displace_old_ones(self, app, ctx, d2_feed,
                                                sort, path, template,
                                                per_page):
        from deaddit import db as _db

        client = app.test_client()
        before = _walk(client, ctx, f"{path}?sort={sort}", template, per_page)

        # 5 brand-new, high-score posts.
        now = datetime.utcnow()
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
            for i in range(5)
        ]
        _db.session.add_all(fresh)
        _db.session.commit()
        fresh_ids = {p.id for p in fresh}

        after = _walk(client, ctx, f"{path}?sort={sort}", template, per_page)
        old_rows = [r for r in d2_feed["rows"] if r["id"] in set(before)]
        expected_now = _expected_ids(
            d2_feed["rows"]
            + [{"id": p.id, "score": p.score, "created_at": p.created_at}
               for p in fresh],
            sort,
        )
        # Every previously-seen id still appears exactly once...
        assert sorted(after) == sorted(expected_now)
        for oid in (r["id"] for r in old_rows):
            assert after.count(oid) == 1
        # ...and the new ones appear.
        assert fresh_ids <= set(after)


class TestCommentSorts:
    def _tree_ids(self, app, ctx, thread, query=""):
        client = app.test_client()
        resp = client.get(f"/d/alpha/{thread['post_id']}{query}")
        assert resp.status_code == 200
        tree = _ctx_of(ctx, "post.html")["comment_tree"]
        return [node["content"] for node in tree]

    def _splits(self):
        out = {}
        for title, score, votes in COMMENT_SPEC:
            out[title] = up_down_split(score, votes)
        return out

    def test_top_order(self, app, ctx, comment_thread):
        assert self._tree_ids(app, ctx, comment_thread, "?sort=top") == [
            "steady", "grindy", "divided", "downbad",
        ]

    def test_default_is_top(self, app, ctx, comment_thread):
        assert self._tree_ids(app, ctx, comment_thread) == [
            "steady", "grindy", "divided", "downbad",
        ]

    def test_garbage_comment_sort_falls_back_to_top(self, app, ctx,
                                                    comment_thread):
        assert self._tree_ids(app, ctx, comment_thread, "?sort=bogus") == [
            "steady", "grindy", "divided", "downbad",
        ]

    def test_new_order(self, app, ctx, comment_thread):
        assert self._tree_ids(app, ctx, comment_thread, "?sort=new") == [
            "downbad", "grindy", "divided", "steady",
        ]

    def test_best_orders_by_hand_computed_wilson(self, app, ctx,
                                                 comment_thread):
        splits = self._splits()
        wilsons = {
            t: wilson_lower_bound(up, down) for t, (up, down) in splits.items()
        }
        # Hand-computed expectations: steady .65 > grindy .34 >
        # divided .24 > downbad .00.
        assert (
            wilsons["steady"] > wilsons["grindy"]
            > wilsons["divided"] > wilsons["downbad"]
        )
        assert self._tree_ids(app, ctx, comment_thread, "?sort=best") == [
            "steady", "grindy", "divided", "downbad",
        ]


    def test_controversial_orders_by_min_up_down(self, app, ctx,
                                                 comment_thread):
        splits = self._splits()
        cont = {t: min(up, down) for t, (up, down) in splits.items()}
        assert cont == {
            "steady": 1, "divided": 5, "grindy": 9, "downbad": 0,
        }
        assert self._tree_ids(app, ctx, comment_thread,
                              "?sort=controversial") == [
            "grindy", "divided", "steady", "downbad",
        ]



@pytest.fixture()
def ranking_indexes(app):
    """create_all() builds model indexes only; the D2 feed indexes live in
    the migration. Create them here with the same byte-identical expression
    so EXPLAIN QUERY PLAN sees the production schema."""
    from deaddit.dynamics.ranking import HOT_SQL_FRAGMENT
    from deaddit.extensions import db

    db.session.execute(sa_text("CREATE INDEX ix_post_score ON post (score)"))
    db.session.execute(
        sa_text(f"CREATE INDEX ix_post_hot_expr ON post (({HOT_SQL_FRAGMENT}))")
    )
    db.session.commit()
    yield

class TestQueryPlans:
    @staticmethod
    def _plan_lines(app, sub_filtered: bool, sort: str) -> list[str]:
        from deaddit.extensions import db

        q = db.session.query(Post)
        if sub_filtered:
            q = q.filter(Post.subdeaddit_name == "alpha")
        if sort == "rising":
            q = q.filter(rising_filter())
        q = q.order_by(*post_order_by(sort))

        sql = str(q.statement.compile(
            dialect=db.engine.dialect,
            compile_kwargs={"literal_binds": True},
        ))
        rows = db.session.execute(sa_text(f"EXPLAIN QUERY PLAN {sql}")).fetchall()
        return [row[3] for row in rows]

    @pytest.mark.parametrize("sort", POST_SORTS)
    @pytest.mark.parametrize("sub_filtered", [False, True],
                             ids=["all", "sub"])
    def test_no_bare_table_scan(self, app, d2_feed, ranking_indexes,
                                sub_filtered, sort):
        lines = self._plan_lines(app, sub_filtered, sort)
        plan = "\n".join(lines)
        for line in lines:
            ok = (
                "USING INDEX" in line
                or line.startswith("SEARCH")
                or "TEMP B-TREE" in line
            )
            assert ok, f"bare scan in plan:\n{plan}"
