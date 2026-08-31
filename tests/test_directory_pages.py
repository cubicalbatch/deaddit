"""Directory pages (`/list_subdeaddit`, `/users`): sort, filter, counts, paging.

Both pages share one card system, so they share one contract: a sorted,
filterable, paged list plus the two counts the header renders
(``match_count`` inside ``total_*``). Assertions read the recorded
``render_template`` context, so markup can be restyled freely.

Pinned here:
- ``/list_subdeaddit``: ``communities`` rows (name/description/post_types/
  post_count/comment_count), sort=name|posts with unknown falling back to
  name, ``q`` filtering name+description, 24 per page.
- ``/users``: ``q`` filtering username+bio+occupation, 24 per page, sort
  preserved across a filter.
- LIKE metacharacters in ``q`` are escaped, not treated as wildcards.
- Removed posts/comments never inflate a community card's counts.
"""

from datetime import datetime, timedelta

import pytest

from deaddit.models import Comment, Post, Subdeaddit, User

BASE = datetime(2026, 8, 1, 12, 0, 0)
PER_PAGE = 24


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


def _post(i, user, sub, removed=False):
    return Post(
        title=f"Post {i:03d}",
        content="body",
        user=user,
        subdeaddit_name=sub,
        model="m",
        removed=removed,
        created_at=BASE + timedelta(minutes=i),
    )


class TestCommunitiesDirectory:
    @pytest.fixture()
    def communities_db(self, app, db_session):
        db_session.add_all(
            [
                User(username="author", interests="[]"),
                Subdeaddit(
                    name="alpha",
                    description="Talk about telescopes",
                    post_types='["discussion", "questions"]',
                ),
                Subdeaddit(name="beta", description="Books and reading"),
                Subdeaddit(name="gamma", description="Empty for now"),
            ]
        )
        db_session.flush()
        posts = [_post(i, "author", "alpha") for i in range(3)]
        posts.append(_post(9, "author", "alpha", removed=True))
        posts.extend(_post(i, "author", "beta") for i in range(20, 21))
        db_session.add_all(posts)
        db_session.flush()
        db_session.add_all(
            [
                Comment(post_id=posts[0].id, content="c1", user="author", model="m"),
                Comment(post_id=posts[0].id, content="c2", user="author", model="m"),
                # Neither of these may reach a card: one is removed, the other
                # hangs off a removed post.
                Comment(
                    post_id=posts[1].id,
                    content="gone",
                    user="author",
                    model="m",
                    removed=True,
                ),
                Comment(
                    post_id=posts[3].id, content="orphan", user="author", model="m"
                ),
            ]
        )
        db_session.commit()

    def test_rows_carry_counts_and_post_types(self, client, ctx, communities_db):
        resp = client.get("/list_subdeaddit")
        assert resp.status_code == 200
        context = _ctx_of(ctx, "list_subdeaddit.html")

        assert [row.name for row in context["communities"]] == [
            "alpha",
            "beta",
            "gamma",
        ]
        assert context["total_communities"] == 3
        assert context["match_count"] == 3
        assert context["sort"] == "name"
        assert context["q"] == ""

        alpha = context["communities"][0]
        assert alpha.post_types == ["discussion", "questions"]
        assert alpha.description == "Talk about telescopes"
        # 3 visible posts (the removed one is excluded) and 2 visible comments
        # (removed comment and comment on the removed post both excluded).
        assert alpha.post_count == 3
        assert alpha.comment_count == 2
        assert context["communities"][2].post_count == 0
        assert context["communities"][2].comment_count == 0

    def test_invalid_post_types_degrade_to_empty_list(self, client, ctx, db_session):
        db_session.add(Subdeaddit(name="broken", description="d", post_types="{oops"))
        db_session.commit()

        assert client.get("/list_subdeaddit").status_code == 200
        context = _ctx_of(ctx, "list_subdeaddit.html")
        assert context["communities"][0].post_types == []

    def test_sort_by_posts_then_name(self, client, ctx, communities_db):
        resp = client.get("/list_subdeaddit?sort=posts")
        assert resp.status_code == 200
        context = _ctx_of(ctx, "list_subdeaddit.html")
        assert context["sort"] == "posts"
        assert [row.name for row in context["communities"]] == [
            "alpha",
            "beta",
            "gamma",
        ]
        assert [row.post_count for row in context["communities"]] == [3, 1, 0]

    def test_unknown_sort_falls_back_to_name(self, client, ctx, communities_db):
        assert client.get("/list_subdeaddit?sort=bogus").status_code == 200
        context = _ctx_of(ctx, "list_subdeaddit.html")
        assert context["sort"] == "name"
        assert [row.name for row in context["communities"]] == [
            "alpha",
            "beta",
            "gamma",
        ]

    def test_filter_matches_name_or_description(self, client, ctx, communities_db):
        resp = client.get("/list_subdeaddit?q=books")
        assert resp.status_code == 200
        context = _ctx_of(ctx, "list_subdeaddit.html")
        assert [row.name for row in context["communities"]] == ["beta"]
        assert context["q"] == "books"
        assert context["match_count"] == 1
        # The header reads "1 of 3": the total stays unfiltered.
        assert context["total_communities"] == 3
        assert context["total_pages"] == 1

        assert client.get("/list_subdeaddit?q=alph").status_code == 200
        context = _ctx_of(ctx, "list_subdeaddit.html")
        assert [row.name for row in context["communities"]] == ["alpha"]

    def test_filter_escapes_like_metacharacters(self, client, ctx, communities_db):
        # A bare % must not behave as "match everything".
        assert client.get("/list_subdeaddit?q=%25").status_code == 200
        context = _ctx_of(ctx, "list_subdeaddit.html")
        assert context["communities"] == []
        assert context["match_count"] == 0

    def test_pagination_partitions_all_communities(self, client, ctx, db_session):
        db_session.add_all(
            Subdeaddit(name=f"s{i:03d}", description="d") for i in range(PER_PAGE + 5)
        )
        db_session.commit()

        seen: list[str] = []
        for page in (1, 2):
            assert client.get(f"/list_subdeaddit?page={page}").status_code == 200
            context = _ctx_of(ctx, "list_subdeaddit.html")
            names = [row.name for row in context["communities"]]
            assert len(names) <= PER_PAGE
            seen.extend(names)

        assert context["total_pages"] == 2
        assert context["has_more"] is False
        assert len(set(seen)) == PER_PAGE + 5
        assert seen == sorted(seen)


class TestUsersDirectoryFilter:
    @pytest.fixture()
    def people_db(self, app, db_session):
        db_session.add_all(
            [
                User(
                    username="alice",
                    bio="keeps bees",
                    occupation="plumber",
                    interests="[]",
                ),
                User(
                    username="bob",
                    bio="fixes taps",
                    occupation="teacher",
                    interests="[]",
                ),
                User(username="plumbeline", bio=None, occupation=None, interests="[]"),
            ]
        )
        db_session.commit()

    @pytest.mark.parametrize(
        ("term", "expected"),
        [
            ("plumb", ["alice", "plumbeline"]),  # occupation and username
            ("bees", ["alice"]),  # bio
            ("teacher", ["bob"]),
            ("nobody", []),
        ],
    )
    def test_filter_matches_username_bio_or_occupation(
        self, client, ctx, people_db, term, expected
    ):
        assert client.get(f"/users?q={term}").status_code == 200
        context = _ctx_of(ctx, "users_list.html")
        assert [row.username for row in context["users"]] == expected
        assert context["match_count"] == len(expected)
        assert context["total_users"] == 3
        assert context["q"] == term

    def test_filter_keeps_the_active_sort(self, client, ctx, people_db):
        assert client.get("/users?q=plumb&sort=activity").status_code == 200
        context = _ctx_of(ctx, "users_list.html")
        assert context["sort"] == "activity"
        assert {row.username for row in context["users"]} == {"alice", "plumbeline"}

    def test_filter_escapes_like_metacharacters(self, client, ctx, people_db):
        assert client.get("/users?q=%25").status_code == 200
        context = _ctx_of(ctx, "users_list.html")
        assert context["users"] == []
        assert context["match_count"] == 0

    def test_rows_carry_occupation(self, client, ctx, people_db):
        assert client.get("/users").status_code == 200
        context = _ctx_of(ctx, "users_list.html")
        by_name = {row.username: row for row in context["users"]}
        assert by_name["alice"].occupation == "plumber"
        assert by_name["plumbeline"].occupation is None
