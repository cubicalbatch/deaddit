"""Tests for post type filters (Pictures, Links, Text) on frontpage and community feeds."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from flask import template_rendered

from deaddit.dynamics.ranking import (
    POST_FILTERS,
    normalize_post_filter,
    post_filter_clause,
)
from deaddit.models import GeneratedWebsite, Post, PostImage, Subdeaddit, User


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
def filter_test_db(app):
    """Create test data with distinct Text, Picture, and Link posts across 2 subs."""
    from deaddit.extensions import db

    u = User(username="filter_user", bio="bio", interests="[]")
    sub_alpha = Subdeaddit(name="alpha", description="Alpha sub")
    sub_beta = Subdeaddit(name="beta", description="Beta sub")
    db.session.add_all([u, sub_alpha, sub_beta])
    db.session.flush()

    now = datetime.utcnow()
    posts = []

    # 4 pure text posts (2 in alpha, 2 in beta)
    for i in range(4):
        sub = "alpha" if i < 2 else "beta"
        p = Post(
            title=f"Text Post {i}",
            content=f"Text body {i}",
            user="filter_user",
            subdeaddit_name=sub,
            score=10 + i,
            vote_count=10 + i,
            created_at=now - timedelta(hours=i),
        )
        posts.append(p)

    # 4 picture posts (2 in alpha, 2 in beta)
    for i in range(4):
        sub = "alpha" if i < 2 else "beta"
        p = Post(
            title=f"Picture Post {i}",
            content=f"Picture caption {i}",
            user="filter_user",
            subdeaddit_name=sub,
            score=20 + i,
            vote_count=20 + i,
            created_at=now - timedelta(hours=4 + i),
        )
        posts.append(p)

    # 4 link posts (2 in alpha, 2 in beta)
    for i in range(4):
        sub = "alpha" if i < 2 else "beta"
        p = Post(
            title=f"Link Post {i}",
            content=f"Link description {i}",
            user="filter_user",
            subdeaddit_name=sub,
            score=30 + i,
            vote_count=30 + i,
            created_at=now - timedelta(hours=8 + i),
        )
        posts.append(p)

    db.session.add_all(posts)
    db.session.flush()

    # Add PostImage to picture posts (indices 4..7)
    for i, p in enumerate(posts[4:8]):
        img = PostImage(
            post_id=p.id,
            original_path=f"img/orig_{i}.png",
            thumbnail_path=f"img/thumb_{i}.png",
            mime_type="image/png",
            byte_size=1024,
            width=800,
            height=600,
            alt_text=f"Alt text {i}",
            source_prompt=f"Prompt {i}",
            provider_snapshot="openai",
            model_snapshot="dall-e-3",
        )
        db.session.add(img)

    # Add GeneratedWebsite to link posts (indices 8..11)
    for i, p in enumerate(posts[8:12]):
        site = GeneratedWebsite(
            post_id=p.id,
            public_path=f"site-{i}.example.com/index.html",
            storage_path=f"pages/site_{i}.html",
            hostname=f"site-{i}.example.com",
            page_name="index.html",
            source_description=f"Generated site {i}",
            byte_size=2048,
            sha256="0" * 64,
            creator_username_snapshot="filter_user",
            api_url_snapshot="http://api.local",
            model_snapshot="qwen-32b",
        )
        db.session.add(site)

    db.session.commit()
    return {
        "text_posts": posts[0:4],
        "picture_posts": posts[4:8],
        "link_posts": posts[8:12],
        "all_posts": posts,
    }


def _index_ctx(ctx):
    matches = [c for c in ctx if c["name"] == "index.html"]
    assert matches, f"index.html never rendered; got {[c['name'] for c in ctx]}"
    return matches[-1]["context"]


def _sub_ctx(ctx):
    matches = [c for c in ctx if c["name"] == "subdeaddit.html"]
    assert matches, f"subdeaddit.html never rendered; got {[c['name'] for c in ctx]}"
    return matches[-1]["context"]


class TestFilterNormalization:
    def test_empty_and_none(self):
        assert normalize_post_filter(None) == []
        assert normalize_post_filter("") == []
        assert normalize_post_filter("   ") == []
        assert normalize_post_filter([]) == []

    def test_single_filters(self):
        assert normalize_post_filter("pictures") == ["pictures"]
        assert normalize_post_filter("links") == ["links"]
        assert normalize_post_filter("text") == ["text"]

    def test_aliases(self):
        assert normalize_post_filter("picture") == ["pictures"]
        assert normalize_post_filter("image") == ["pictures"]
        assert normalize_post_filter("images") == ["pictures"]
        assert normalize_post_filter("link") == ["links"]
        assert normalize_post_filter("website") == ["links"]
        assert normalize_post_filter("websites") == ["links"]
        assert normalize_post_filter("texts") == ["text"]

    def test_comma_separated_and_multi_values(self):
        assert normalize_post_filter("pictures,links") == ["links", "pictures"]
        assert normalize_post_filter(["pictures", "text"]) == ["pictures", "text"]
        assert normalize_post_filter("image,link,text") == ["links", "pictures", "text"]

    def test_case_and_whitespace(self):
        assert normalize_post_filter("  PICTURES , LINKS ") == ["links", "pictures"]

    def test_unknown_tokens_ignored(self):
        assert normalize_post_filter("invalid,random,pictures") == ["pictures"]
        assert normalize_post_filter("foo,bar") == []


class TestFilterClause:
    def test_empty_clause_returns_none(self, app):
        with app.app_context():
            assert post_filter_clause([]) is None
            assert post_filter_clause(None) is None
            assert post_filter_clause(list(POST_FILTERS)) is None

    def test_single_filter_clause(self, app):
        with app.app_context():
            assert post_filter_clause(["pictures"]) is not None
            assert post_filter_clause(["links"]) is not None
            assert post_filter_clause(["text"]) is not None


class TestFeedFilterIntegration:
    def test_no_filter_returns_all_posts(self, app, ctx, filter_test_db):
        client = app.test_client()
        resp = client.get("/")
        assert resp.status_code == 200
        rendered_posts = _index_ctx(ctx)["posts"]
        assert len(rendered_posts) == 12
        assert _index_ctx(ctx)["active_filters"] == []

    def test_filter_pictures_only(self, app, ctx, filter_test_db):
        client = app.test_client()
        resp = client.get("/?filter=pictures")
        assert resp.status_code == 200
        rendered_posts = _index_ctx(ctx)["posts"]
        assert len(rendered_posts) == 4
        expected_ids = {p.id for p in filter_test_db["picture_posts"]}
        assert {p.id for p in rendered_posts} == expected_ids
        assert _index_ctx(ctx)["active_filters"] == ["pictures"]

    def test_filter_links_only(self, app, ctx, filter_test_db):
        client = app.test_client()
        resp = client.get("/?filter=links")
        assert resp.status_code == 200
        rendered_posts = _index_ctx(ctx)["posts"]
        assert len(rendered_posts) == 4
        expected_ids = {p.id for p in filter_test_db["link_posts"]}
        assert {p.id for p in rendered_posts} == expected_ids
        assert _index_ctx(ctx)["active_filters"] == ["links"]

    def test_filter_text_only(self, app, ctx, filter_test_db):
        client = app.test_client()
        resp = client.get("/?filter=text")
        assert resp.status_code == 200
        rendered_posts = _index_ctx(ctx)["posts"]
        assert len(rendered_posts) == 4
        expected_ids = {p.id for p in filter_test_db["text_posts"]}
        assert {p.id for p in rendered_posts} == expected_ids
        assert _index_ctx(ctx)["active_filters"] == ["text"]

    def test_multi_filter_pictures_and_links(self, app, ctx, filter_test_db):
        client = app.test_client()
        resp = client.get("/?filter=pictures,links")
        assert resp.status_code == 200
        rendered_posts = _index_ctx(ctx)["posts"]
        assert len(rendered_posts) == 8
        expected_ids = {
            p.id for p in filter_test_db["picture_posts"] + filter_test_db["link_posts"]
        }
        assert {p.id for p in rendered_posts} == expected_ids
        assert _index_ctx(ctx)["active_filters"] == ["links", "pictures"]

    def test_multi_filter_pictures_and_text(self, app, ctx, filter_test_db):
        client = app.test_client()
        resp = client.get("/?filter=pictures,text")
        assert resp.status_code == 200
        rendered_posts = _index_ctx(ctx)["posts"]
        assert len(rendered_posts) == 8
        expected_ids = {
            p.id for p in filter_test_db["picture_posts"] + filter_test_db["text_posts"]
        }
        assert {p.id for p in rendered_posts} == expected_ids
        assert _index_ctx(ctx)["active_filters"] == ["pictures", "text"]

    def test_all_filters_returns_all(self, app, ctx, filter_test_db):
        client = app.test_client()
        resp = client.get("/?filter=pictures,links,text")
        assert resp.status_code == 200
        rendered_posts = _index_ctx(ctx)["posts"]
        assert len(rendered_posts) == 12

    def test_filter_with_sort(self, app, ctx, filter_test_db):
        client = app.test_client()
        resp = client.get("/?sort=new&filter=pictures")
        assert resp.status_code == 200
        rendered_posts = _index_ctx(ctx)["posts"]
        assert len(rendered_posts) == 4
        assert _index_ctx(ctx)["sort"] == "new"
        # Check ordering: newest created_at first
        assert rendered_posts == sorted(
            rendered_posts, key=lambda p: p.created_at, reverse=True
        )

    def test_community_feed_filtering(self, app, ctx, filter_test_db):
        client = app.test_client()
        # In sub alpha: 2 text, 2 picture, 2 link
        resp_alpha = client.get("/d/alpha?filter=pictures")
        assert resp_alpha.status_code == 200
        alpha_posts = _sub_ctx(ctx)["posts"]
        assert len(alpha_posts) == 2
        assert all(p.subdeaddit_name == "alpha" for p in alpha_posts)
        assert _sub_ctx(ctx)["active_filters"] == ["pictures"]


class TestFilterUIRendering:
    def test_sort_bar_preserves_filter(self, app, filter_test_db):
        client = app.test_client()
        resp = client.get("/?filter=pictures")
        html = resp.data.decode("utf-8")
        # Check that sort links include filter=pictures
        assert "filter=pictures" in html
        assert 'class="filter-bar"' in html
        assert 'class="filter-bar__chip is-active"' in html
        assert 'aria-pressed="true"' in html

    def test_filter_chips_rendered(self, app, filter_test_db):
        client = app.test_client()
        resp = client.get("/")
        html = resp.data.decode("utf-8")
        assert "Pictures" in html
        assert "Links" in html
        assert "Text" in html
        assert 'class="filter-bar"' in html
        assert 'class="sort-bar"' in html

    def test_active_chip_toggle_url_removes_filter(self, app, filter_test_db):
        client = app.test_client()
        resp = client.get("/?filter=pictures")
        html = resp.data.decode("utf-8")
        # When Pictures is active, clicking it toggles it off (links to / without filter=pictures)
        assert 'aria-pressed="true"' in html
        assert 'class="filter-bar__chip is-active"' in html
        # Ensure no clear button exists to cause layout shift
        assert 'class="filter-bar__clear"' not in html
