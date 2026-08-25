"""UX-4 profiles/people/search slice: profile tabs, users directory, SQLite search.

Tests capture the actual ``render_template`` context via the
``template_rendered`` signal so they assert the route contract (context vars,
ordering, pagination) independently of template markup.

Pinned contract under test:
- ``/user/<username>``: tab=posts|comments paging (20/page), stats dict,
  safe-parsed traits/interests ([] on NULL/invalid), bio_html ('' when bio
  is NULL).
- ``/users``: sort=username|activity rows with post_count/comment_count/
  activity.
- ``/search``: posts/communities/people sections, LIKE metachar escaping,
  newest-first ordering, empty-q empty state.
- Setup flow: fresh DB serves setup.html; save-config + load-default-data
  flips ``/`` to index.html with populated rails.
"""

from datetime import datetime, timedelta

import pytest

from deaddit.models import Comment, Post, Subdeaddit, User

BASE = datetime(2026, 8, 1, 12, 0, 0)
PER_PAGE = 20


@pytest.fixture()
def ctx(app):
    """Record template contexts rendered during a request."""
    from flask import template_rendered

    recorded: list[dict] = []

    def _record(sender, template, context, **extra):
        recorded.append({"name": template.name, "context": dict(context)})

    template_rendered.connect(_record)
    yield recorded
    template_rendered.disconnect(_record)


def _ctx_of(records, name):
    matches = [c for c in records if c["name"] == name]
    assert matches, f"{name} never rendered; got {[c['name'] for c in records]}"
    return matches[-1]["context"]


def _mk_user(username, **fields):
    fields.setdefault("interests", "[]")
    return User(username=username, **fields)


def _mk_sub(name, description="A subdeaddit"):
    return Subdeaddit(name=name, description=description)


def _mk_post(i, user, sub, title=None, content="body"):
    """Post i: created_at ascending with i, so newest-first == id DESC."""
    return Post(
        title=title if title is not None else f"Post {i:03d}",
        content=content,
        user=user,
        subdeaddit_name=sub,
        model="model-x",
        upvote_count=i % 5,
        created_at=BASE + timedelta(minutes=i),
    )


def _cell(row, key):
    """Read a context row field; rows may be dicts or named tuples."""
    try:
        return row[key]
    except TypeError:
        return getattr(row, key)


class TestNullBioRegression:
    """Users with NULL/empty bio must not crash /users or their profiles."""

    def test_null_and_empty_bio_render_directory_and_profiles(
        self, app, client, db_session, ctx
    ):
        db_session.add_all(
            [
                _mk_sub("s"),
                _mk_user("nobody", bio=None),  # interests also NULL on purpose
                _mk_user("blank", bio="", personality_traits=None),
                _mk_user("writer", bio="hello world"),
            ]
        )
        db_session.flush()
        db_session.add(_mk_post(0, "nobody", "s"))
        db_session.commit()

        resp = client.get("/users")
        assert resp.status_code == 200
        rows = _ctx_of(ctx, "users_list.html")["users"]
        by_name = {_cell(row, "username"): row for row in rows}
        assert _cell(by_name["nobody"], "bio") is None
        assert _cell(by_name["blank"], "bio") == ""

        resp = client.get("/user/nobody")
        assert resp.status_code == 200
        profile = _ctx_of(ctx, "user_profile.html")
        assert profile["traits"] == []
        assert profile["interests"] == []
        assert profile["bio_html"] == ""

        resp = client.get("/user/blank")
        assert resp.status_code == 200
        profile = _ctx_of(ctx, "user_profile.html")
        assert profile["traits"] == []
        assert profile["stats"]["post_count"] == 0


class TestProfileTraits:
    def test_traits_exposed_in_context_and_body(self, app, client, db_session, ctx):
        db_session.add_all(
            [
                _mk_sub("s"),
                _mk_user(
                    "quirky",
                    personality_traits='["curious","dry wit"]',
                    writing_style="terse and dry",
                    bio="just quirky",
                ),
            ]
        )
        db_session.flush()
        post = _mk_post(0, "quirky", "s")
        db_session.add(post)
        db_session.flush()
        db_session.add_all(
            [
                Comment(
                    post_id=post.id,
                    content="c",
                    user="quirky",
                    model="m",
                    upvote_count=5,
                    created_at=BASE + timedelta(hours=1),
                )
            ]
        )
        db_session.commit()

        resp = client.get("/user/quirky")
        assert resp.status_code == 200
        profile = _ctx_of(ctx, "user_profile.html")
        assert "curious" in profile["traits"]
        assert "dry wit" in profile["traits"]

        body = resp.get_data(as_text=True)
        assert "dry wit" in body

    def test_stats_dict_counts_posts_comments_upvotes(
        self, app, client, db_session, ctx
    ):
        db_session.add_all(
            [_mk_sub("s"), _mk_user("counter", bio=None), _mk_user("other")]
        )
        db_session.flush()
        p1 = _mk_post(0, "counter", "s")  # upvote_count = 0
        p2 = _mk_post(1, "counter", "s")  # upvote_count = 1
        db_session.add_all([p1, p2])
        db_session.flush()
        db_session.add_all(
            [
                # upvotes: posts 0 + 1, comment 5 -> total_upvotes == 6
                Comment(
                    post_id=p1.id,
                    content="c",
                    user="counter",
                    model="m",
                    upvote_count=5,
                    created_at=BASE + timedelta(hours=1),
                )
            ]
        )
        db_session.commit()

        assert client.get("/user/counter").status_code == 200
        stats = _ctx_of(ctx, "user_profile.html")["stats"]
        assert stats["post_count"] == 2
        assert stats["comment_count"] == 1
        assert stats["total_upvotes"] == 6


class TestProfileHistoryPagination:
    @pytest.fixture()
    def history_db(self, app, db_session):
        db_session.add_all([_mk_user("prolific"), _mk_sub("s")])
        db_session.flush()
        posts = [_mk_post(i, "prolific", "s") for i in range(45)]
        db_session.add_all(posts)
        db_session.flush()
        comments = [
            Comment(
                post_id=posts[i].id,
                content=f"c{i}",
                user="prolific",
                model="m",
                created_at=BASE + timedelta(hours=2, minutes=i),
            )
            for i in range(25)
        ]
        db_session.add_all(comments)
        db_session.commit()
        return {"posts": posts, "comments": comments}

    def test_posts_tab_walks_45_posts_over_3_pages_without_repeats(
        self, app, client, db_session, ctx, history_db
    ):
        seen: list[int] = []
        for page in (1, 2, 3):
            resp = client.get(f"/user/prolific?tab=posts&page={page}")
            assert resp.status_code == 200
            context = _ctx_of(ctx, "user_profile.html")
            assert context["active_tab"] == "posts"
            ids = [p.id for p in context["posts"]]
            assert len(ids) <= PER_PAGE
            seen.extend(ids)

        expected = sorted(p.id for p in history_db["posts"])
        assert len(set(seen)) == 45, "posts repeat across pages"
        assert sorted(seen) == expected, "pages do not partition all 45 posts"
        assert seen == sorted(seen, reverse=True), "not newest first"

        last = _ctx_of(ctx, "user_profile.html")
        assert last["total_pages"] == 3
        assert last["has_more"] is False
        assert last["total_posts"] == 45
        assert set(last["comment_counts"]) == set(ids)

    def test_comments_tab_walks_25_comments_without_repeats(
        self, app, client, db_session, ctx, history_db
    ):
        seen: list[int] = []
        for page in (1, 2):
            resp = client.get(f"/user/prolific?tab=comments&page={page}")
            assert resp.status_code == 200
            context = _ctx_of(ctx, "user_profile.html")
            assert context["active_tab"] == "comments"
            ids = [c.id for c in context["comments"]]
            seen.extend(ids)

        expected = sorted(c.id for c in history_db["comments"])
        assert len(set(seen)) == 25, "comments repeat across pages"
        assert sorted(seen) == expected, "pages do not partition all 25 comments"
        assert seen == sorted(seen, reverse=True)

        last = _ctx_of(ctx, "user_profile.html")
        assert last["total_pages"] == 2
        assert last["has_more"] is False
        assert last["total_comments"] == 25


class TestUsersDirectory:
    @pytest.fixture()
    def directory_db(self, app, db_session):
        db_session.add_all(
            [
                _mk_user("alice", age=30, gender="Female"),
                _mk_user("bob"),
                _mk_user("carol"),
                _mk_user("dave"),
                _mk_sub("s"),
            ]
        )
        db_session.flush()
        counts = {"alice": (10, 5), "bob": (8, 9), "carol": (3, 1)}
        posts: list[Post] = []
        for username, (n_posts, _) in counts.items():
            posts.extend(
                _mk_post(i, username, "s") for i in range(n_posts)
            )
        db_session.add_all(posts)
        db_session.flush()
        for username, (_, n_comments) in counts.items():
            db_session.add_all(
                [
                    Comment(
                        post_id=posts[0].id,
                        content=f"{username} {j}",
                        user=username,
                        model="m",
                        created_at=BASE + timedelta(hours=3, minutes=j),
                    )
                    for j in range(n_comments)
                ]
            )
        db_session.commit()

    def test_activity_sort_orders_by_post_plus_comment_desc(
        self, app, client, db_session, ctx, directory_db
    ):
        resp = client.get("/users?sort=activity")
        assert resp.status_code == 200
        context = _ctx_of(ctx, "users_list.html")
        assert context["sort"] == "activity"

        rows = context["users"]
        assert _cell(rows[0], "username") == "bob"  # 17 > alice 15 > carol 4 > dave 0
        by_name = {_cell(row, "username"): row for row in rows}
        assert _cell(by_name["alice"], "post_count") == 10
        assert _cell(by_name["alice"], "comment_count") == 5
        assert _cell(by_name["alice"], "activity") == 15
        assert _cell(by_name["dave"], "activity") == 0
        for row in rows:
            for key in (
                "username",
                "bio",
                "age",
                "gender",
                "post_count",
                "comment_count",
                "activity",
            ):
                assert hasattr(row, key) or (isinstance(row, dict) and key in row)

    def test_unknown_sort_falls_back_to_username(self, app, client, db_session, ctx, directory_db):
        resp = client.get("/users?sort=bogus")
        assert resp.status_code == 200
        context = _ctx_of(ctx, "users_list.html")
        assert context["sort"] == "username"
        assert [_cell(row, "username") for row in context["users"]] == [
            "alice",
            "bob",
            "carol",
            "dave",
        ]

    def test_page_walk_covers_all_users_without_repeats(self, app, client, db_session, ctx):
        db_session.add_all(_mk_user(f"u{i:03d}") for i in range(55))
        db_session.commit()

        seen: list[str] = []
        for page in (1, 2):
            resp = client.get(f"/users?page={page}")
            assert resp.status_code == 200
            names = [
                _cell(row, "username")
                for row in _ctx_of(ctx, "users_list.html")["users"]
            ]
            assert len(names) <= 50
            seen.extend(names)

        assert len(set(seen)) == 55
        assert seen == sorted(seen)  # username asc within/across pages


class TestSearchSections:
    @pytest.fixture()
    def search_db(self, app, db_session):
        db_session.add_all(
            [
                _mk_sub("quantum-lab", description="All things quantum computing"),
                _mk_sub("cooking", description="Food talk"),
                _mk_user("qm_fan", bio="loves quantum physics"),
                _mk_user("chef", bio="cakes only"),
            ]
        )
        db_session.flush()
        posts = [
            _mk_post(0, "chef", "quantum-lab", title="Quantum breakthrough announced"),
            _mk_post(
                1, "chef", "cooking", title="weekly thread", content="quantum entanglement chat"
            ),
            _mk_post(2, "qm_fan", "cooking", title="unrelated", content="bread"),
            _mk_post(3, "chef", "quantum-lab", title="another quantum note"),
        ]
        db_session.add_all(posts)
        db_session.commit()
        return {"posts": posts}

    def test_query_populates_posts_communities_people(self, app, client, db_session, ctx, search_db):
        resp = client.get("/search?q=quantum")
        assert resp.status_code == 200
        context = _ctx_of(ctx, "search.html")

        assert context["q"] == "quantum"
        titles = {p.title for p in context["posts"]}
        assert "Quantum breakthrough announced" in titles  # title match
        assert "weekly thread" in titles  # content match
        assert "unrelated" not in titles

        community_names = [_cell(c, "name") for c in context["communities"]]
        assert "quantum-lab" in community_names  # description match
        assert "cooking" not in community_names
        comm = next(
            c for c in context["communities"] if _cell(c, "name") == "quantum-lab"
        )
        assert _cell(comm, "description") == "All things quantum computing"
        assert _cell(comm, "post_count") == 2

        people_names = [_cell(u, "username") for u in context["people"]]
        assert "qm_fan" in people_names  # bio match
        assert "chef" not in people_names
        person = next(
            u for u in context["people"] if _cell(u, "username") == "qm_fan"
        )
        for key in ("bio", "post_count", "comment_count"):
            assert hasattr(person, key) or (isinstance(person, dict) and key in person)
        assert _cell(person, "comment_count") == 0

    def test_posts_ordered_newest_first(self, app, client, db_session, ctx, search_db):
        assert client.get("/search?q=quantum").status_code == 200
        ids = [p.id for p in _ctx_of(ctx, "search.html")["posts"]]
        assert len(ids) >= 2
        assert ids == sorted(ids, reverse=True)

    def test_many_matches_paginate_without_repeats(self, app, client, db_session, ctx):
        db_session.add_all([_mk_user("author"), _mk_sub("lab")])
        db_session.flush()
        posts = [
            _mk_post(i, "author", "lab", title=f"quantum study {i:03d}")
            for i in range(25)
        ]
        db_session.add_all(posts)
        db_session.commit()

        seen: list[int] = []
        for page in (1, 2):
            resp = client.get(f"/search?q=quantum&page={page}")
            assert resp.status_code == 200
            context = _ctx_of(ctx, "search.html")
            ids = [p.id for p in context["posts"]]
            assert len(ids) <= PER_PAGE
            seen.extend(ids)

        assert len(set(seen)) == 25
        assert sorted(seen) == sorted(p.id for p in posts)

        context = _ctx_of(ctx, "search.html")
        assert context["total_posts"] == 25
        assert context["total_pages"] == 2
        assert context["has_more"] is False


class TestSearchInjectionProbes:
    @pytest.fixture()
    def tiny_db(self, app, db_session):
        db_session.add_all([_mk_user("u1"), _mk_sub("s")])
        db_session.flush()
        db_session.add_all(
            [
                # "%100% match" below only occurs literally in this title;
                # unescaped LIKE would also match "100% match guaranteed".
                _mk_post(0, "u1", "s", title="debian_x_tips"),
                _mk_post(1, "u1", "s", title="max_power"),
                _mk_post(2, "u1", "s", title="%100% match found"),
                _mk_post(3, "u1", "s", title="100% match guaranteed"),
                _mk_post(4, "u1", "s", title="hello world"),
            ]
        )
        db_session.commit()

    def test_script_tag_is_escaped_not_executed(self, app, client, db_session, ctx, tiny_db):
        resp = client.get("/search", query_string={"q": "<script>alert(1)</script>"})
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # pages legitimately contain <script> tags; only the payload must not
        assert "<script>alert(1)" not in body
        assert "&lt;script&gt;" in body  # query echoed back escaped

        posts = _ctx_of(ctx, "search.html")["posts"]
        assert posts == []

    def test_percent_and_underscore_treated_literally(self, app, client, db_session, ctx, tiny_db):
        resp = client.get("/search", query_string={"q": "%100% match"})
        assert resp.status_code == 200
        titles = [p.title for p in _ctx_of(ctx, "search.html")["posts"]]
        assert titles == ["%100% match found"]

        resp = client.get("/search?q=_x")
        assert resp.status_code == 200
        titles = [p.title for p in _ctx_of(ctx, "search.html")["posts"]]
        # without LIKE-escaping `_` matches any char, so this would also
        # hit "max_power" ("aX_p" contains char+'x').
        assert titles == ["debian_x_tips"], "underscore matched as a LIKE wildcard"

    def test_sql_quote_probe_returns_sane_empty_result(self, app, client, db_session, ctx, tiny_db):
        resp = client.get("/search", query_string={"q": "' OR 1=1 --"})
        assert resp.status_code == 200
        context = _ctx_of(ctx, "search.html")
        assert context["posts"] == []


class TestSetupPage:
    def test_fresh_db_serves_setup_template(self, app, client, ctx):
        resp = client.get("/")
        assert resp.status_code == 200
        assert _ctx_of(ctx, "setup.html") is not None

        body = resp.data
        assert b"bootstrap.min.css" not in body
        assert b"Load default data" in body
        assert b"openai_api_url" in body


class TestNewUserFlow:
    def test_save_config_then_load_default_data_flips_index(
        self, app, client, ctx
    ):
        with client.session_transaction() as sess:
            sess["admin_authenticated"] = True

        resp = client.post(
            "/admin/api/save-config",
            json={
                "openai_api_url": "http://localhost:9999/v1",
                "openai_key": "sk-test",
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

        resp = client.post("/admin/api/load-default-data")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

        resp = client.get("/")
        assert resp.status_code == 200
        rendered = [c["name"] for c in ctx]
        assert "setup.html" not in rendered
        assert "index.html" in rendered

        rail_subs = _ctx_of(ctx, "index.html")["rail_subs"]
        assert rail_subs, "default communities were not loaded"


class TestSearchSmoke:
    def test_missing_q_renders_empty_state(self, app, client, ctx):
        resp = client.get("/search")
        assert resp.status_code == 200
        context = _ctx_of(ctx, "search.html")
        assert context["q"] == ""
        assert context["posts"] == []
        assert context["communities"] == []
        assert context["people"] == []

    def test_page_beyond_range_does_not_crash(self, app, client, db_session):
        db_session.add_all([_mk_user("u1"), _mk_sub("s")])
        db_session.flush()
        db_session.add(_mk_post(0, "u1", "s", title="findme x"))
        db_session.commit()

        resp = client.get("/search?q=x&page=99")
        assert resp.status_code == 200
