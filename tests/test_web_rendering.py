"""Website link-post rendering and listing query behavior."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import pytest
from sqlalchemy import event

from deaddit import create_app
from deaddit import db as _db
from deaddit.models import Comment, GeneratedWebsite, Post, Setting, Subdeaddit, User
from deaddit.websites.storage import store_website


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "TESTING": True,
            "GENERATED_WEBSITES_ROOT": str(tmp_path / "websites"),
        }
    )
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db_session(app):
    _db.session.add_all(
        [
            User(username="alice", bio="", interests="[]"),
            Subdeaddit(name="testsub", description="A test subdeaddit"),
        ]
    )
    _db.session.commit()
    Setting.set_value("SETUP_COMPLETED_AT", "2026-01-01T00:00:00Z")
    return _db.session


@dataclass(frozen=True)
class _WebsitePost:
    post_id: int
    title: str
    hostname: str
    page_name: str

    @property
    def site_url(self):
        return f"/out/{self.hostname}/{self.page_name}"

    @property
    def discussion_url(self):
        return f"/d/testsub/{self.post_id}"


class _AnchorParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._current = {
                "href": dict(attrs).get("href", ""),
                "text": "",
            }
            self.links.append(self._current)

    def handle_data(self, data):
        if self._current is not None:
            self._current["text"] += data

    def handle_endtag(self, tag):
        if tag == "a":
            self._current = None


def _links(response):
    parser = _AnchorParser()
    parser.feed(response.get_data(as_text=True))
    return parser.links


def _make_website_post(
    app,
    db_session,
    *,
    title="Interesting website",
    content="A little commentary about this link.",
    hostname="www.example-observatory.com",
    page_name="aurora-map.html",
):
    stored = store_website(
        f"<html><body>{title}</body></html>".encode(),
        Path(app.config["GENERATED_WEBSITES_ROOT"]),
    )
    post = Post(
        title=title,
        content=content,
        subdeaddit_name="testsub",
        user="alice",
    )
    db_session.add(post)
    db_session.flush()
    db_session.add(
        GeneratedWebsite(
            post_id=post.id,
            public_path=f"{hostname}/{page_name}",
            storage_path=stored.storage_path,
            hostname=hostname,
            page_name=page_name,
            source_description="private website brief",
            byte_size=stored.byte_size,
            sha256=stored.sha256,
            creator_username_snapshot="alice",
            api_url_snapshot="https://llm.example/v1",
            model_snapshot="website-test-model",
        )
    )
    db_session.commit()
    return _WebsitePost(post.id, title, hostname, page_name)


def _make_text_post(db_session, *, title="Text-only post token"):
    post = Post(
        title=title,
        content="Plain post body.",
        subdeaddit_name="testsub",
        user="alice",
    )
    db_session.add(post)
    db_session.commit()
    return post.id


def _assert_listing(response, website: _WebsitePost, body: str):
    assert response.status_code == 200
    links = _links(response)
    assert any(
        link["href"] == website.site_url and website.title in link["text"]
        for link in links
    )
    assert any(
        link["href"] == website.discussion_url and "comments" in link["text"]
        for link in links
    )
    assert website.site_url != website.discussion_url
    assert website.hostname in body


def test_website_posts_render_distinct_site_and_discussion_links_on_listings(
    app, client, db_session
):
    website = _make_website_post(
        app,
        db_session,
        title="Website rendering token",
        content="This post explains why I am sharing this link.",
    )
    paths = (
        "/",
        "/d/testsub",
        "/user/alice?tab=posts",
        "/search?q=Website+rendering+token",
    )

    for path in paths:
        response = client.get(path)
        body = response.get_data(as_text=True)
        _assert_listing(response, website, body)
        assert "This post explains why I am sharing this link." in body


def test_website_detail_has_title_host_and_visit_links_and_comments(
    app, client, db_session
):
    website = _make_website_post(
        app,
        db_session,
        title="A detail website token",
        content="A sanitized explanation for the generated page.",
    )
    db_session.add(
        Comment(
            post_id=website.post_id,
            content="I visited this page.",
            user="alice",
            model="test",
        )
    )
    db_session.commit()

    response = client.get(website.discussion_url)
    body = response.get_data(as_text=True)
    links = _links(response)
    site_links = [link for link in links if link["href"] == website.site_url]

    assert response.status_code == 200
    assert len(site_links) >= 2
    assert any(website.title in link["text"] for link in site_links)
    assert any("Visit website" in link["text"] for link in site_links)
    assert website.hostname in body
    assert "A sanitized explanation for the generated page." in body
    assert "Comments" in body
    assert "I visited this page." in body


def test_website_without_body_and_text_posts_keep_existing_discussion_behavior(
    app, client, db_session
):
    website = _make_website_post(
        app,
        db_session,
        title="No body website token",
        content=None,
        hostname="www.no-body.example",
        page_name="index.html",
    )

    for path in (
        "/",
        "/d/testsub",
        "/user/alice?tab=posts",
        "/search?q=No+body+website+token",
    ):
        response = client.get(path)
        body = response.get_data(as_text=True)
        _assert_listing(response, website, body)
        assert "post-card__preview" not in body

    detail = client.get(website.discussion_url)
    detail_body = detail.get_data(as_text=True)
    assert detail.status_code == 200
    assert "No body website token" in detail_body
    assert "post-body" not in detail_body

    text_post_id = _make_text_post(db_session, title="Text-only post token")

    for path in (
        "/",
        "/d/testsub",
        "/user/alice?tab=posts",
        "/search?q=Text-only+post+token",
    ):
        response = client.get(path)
        links = _links(response)
        assert response.status_code == 200
        assert any(
            link["href"] == f"/d/testsub/{text_post_id}"
            and "Text-only post token" in link["text"]
            for link in links
        )


def _select_count(client, engine, path):
    statements = 0

    def tick(_conn, _cursor, statement, *_args):
        nonlocal statements
        if statement.lstrip().upper().startswith("SELECT"):
            statements += 1

    event.listen(engine, "before_cursor_execute", tick)
    try:
        response = client.get(path)
    finally:
        event.remove(engine, "before_cursor_execute", tick)
    assert response.status_code == 200
    return statements


def test_listing_website_lookup_does_not_add_relationship_n_plus_one(
    app, client, db_session
):
    for index in range(3):
        _make_website_post(
            app,
            db_session,
            title=f"Bulk website query token {index}",
            hostname=f"www.bulk-{index}.example",
            page_name="page.html",
        )
    paths = (
        "/",
        "/d/testsub",
        "/user/alice?tab=posts",
        "/search?q=Bulk+website+query+token",
    )
    # Warm process-local configuration caches before measuring request deltas.
    for path in paths:
        assert client.get(path).status_code == 200
    small = {
        path: _select_count(client, app.extensions["sqlalchemy"].engine, path)
        for path in paths
    }

    for index in range(3, 11):
        _make_website_post(
            app,
            db_session,
            title=f"Bulk website query token {index}",
            hostname=f"www.bulk-{index}.example",
            page_name="page.html",
        )
    large = {
        path: _select_count(client, app.extensions["sqlalchemy"].engine, path)
        for path in paths
    }

    assert large == small
