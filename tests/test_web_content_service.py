"""Unit tests for the website preflight/atomic-publish trio in
deaddit.services.content (subphase 2.2): PendingGeneratedWebsite,
preflight_website_post, create_website_post.

Mirrors tests/test_content_service.py's image-post conventions. Every
generated-website file used here is written through the real
deaddit.websites.storage primitives into a tmp_path-rooted
GENERATED_WEBSITES_ROOT - never the real instance/ directory - so failure
tests can assert the file was actually deleted from disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from deaddit import create_app
from deaddit import db as _db
from deaddit.models import GeneratedWebsite, Post, Subdeaddit, User
from deaddit.services import content as content_service
from deaddit.services.content import (
    ContentValidationError,
    PendingGeneratedWebsite,
    create_website_post,
    preflight_website_post,
)
from deaddit.websites.storage import store_website, website_root

_PRIVATE_DESCRIPTION = (
    "a private, detailed site brief that must never leak into cleanup logs"
)


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "TESTING": True,
            "GENERATED_WEBSITES_ROOT": str(tmp_path / "websites"),
        }
    )
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def db_session(app):
    return _db.session


@pytest.fixture()
def seeded_db(app, db_session):
    users = [
        User(username="alice", bio="curious alice", interests='["testing"]'),
        User(username="bob", bio="bob builds things", interests='["coding"]'),
    ]
    subs = [Subdeaddit(name="testsub", description="A test subdeaddit")]
    db_session.add_all(users + subs)
    db_session.commit()
    return {"users": users, "subs": subs}


@pytest.fixture()
def cache_spy(monkeypatch):
    calls = []
    monkeypatch.setattr(
        content_service, "_clear_read_caches", lambda: calls.append("clear")
    )
    return calls


def _store_html(app, html: str = "<!doctype html><html><body>Hi</body></html>"):
    return store_website(html, website_root(app))


def _pending_website(app, **overrides) -> PendingGeneratedWebsite:
    stored = overrides.pop("stored", None) or _store_html(app)
    fields = {
        "storage_path": stored.storage_path,
        "byte_size": stored.byte_size,
        "sha256": stored.sha256,
        "public_path": "www.example.test/page.html",
        "hostname": "www.example.test",
        "page_name": "page.html",
        "source_description": _PRIVATE_DESCRIPTION,
        "creator_username_snapshot": "alice",
        "api_url_snapshot": "http://example.test/v1",
        "model_snapshot": "test-model",
    }
    fields.update(overrides)
    return PendingGeneratedWebsite(**fields)


def _file_path(app, storage_path: str) -> Path:
    return website_root(app) / storage_path


# ---------------------------------------------------------------------------
# preflight_website_post


def test_preflight_website_post_accepts_valid_input(seeded_db):
    preflight_website_post(user="alice", subdeaddit="testsub", title="A title")


def test_preflight_website_post_rejects_empty_title(seeded_db):
    with pytest.raises(ContentValidationError):
        preflight_website_post(user="alice", subdeaddit="testsub", title="")
    assert GeneratedWebsite.query.count() == 0


def test_preflight_website_post_missing_user_message(seeded_db):
    with pytest.raises(ContentValidationError) as exc:
        preflight_website_post(user="ghost", subdeaddit="testsub", title="T")
    assert str(exc.value) == "User 'ghost' does not exist"
    assert GeneratedWebsite.query.count() == 0


def test_preflight_website_post_invalid_community_message(seeded_db):
    with pytest.raises(ContentValidationError) as exc:
        preflight_website_post(user="alice", subdeaddit="nope", title="T")
    assert str(exc.value) == "Subdeaddit 'nope' does not exist"
    assert GeneratedWebsite.query.count() == 0


def test_preflight_website_post_rejects_rate_limited_user(seeded_db, monkeypatch):
    monkeypatch.setitem(
        content_service._RATE_LIMITS, "post", ("rate_limit_posts_per_hour", 0)
    )
    with pytest.raises(ContentValidationError) as exc:
        preflight_website_post(user="alice", subdeaddit="testsub", title="T")
    assert str(exc.value) == "rate_limited"
    assert GeneratedWebsite.query.count() == 0


# ---------------------------------------------------------------------------
# create_website_post - success


def test_create_website_post_persists_post_and_website_with_blank_content(
    app, seeded_db, db_session, cache_spy
):
    website = _pending_website(app)
    post = create_website_post(
        title="I found this odd site",
        content=None,
        user="alice",
        subdeaddit="testsub",
        website=website,
        model="agent:alice",
    )

    assert Post.query.count() == 1
    assert GeneratedWebsite.query.count() == 1

    fetched = Post.query.filter_by(id=post.id).one()
    assert fetched.title == "I found this odd site"
    assert fetched.content is None

    row = GeneratedWebsite.query.filter_by(post_id=post.id).one()
    assert row.public_path == website.public_path
    assert row.storage_path == website.storage_path
    assert row.hostname == website.hostname
    assert row.page_name == website.page_name
    assert row.source_description == _PRIVATE_DESCRIPTION
    assert row.byte_size == website.byte_size
    assert row.sha256 == website.sha256
    assert row.creator_username_snapshot == "alice"
    assert row.api_url_snapshot == "http://example.test/v1"
    assert row.model_snapshot == "test-model"

    # Correctly linked both ways.
    assert fetched.website is row
    assert row.post_id == fetched.id

    assert _file_path(app, website.storage_path).is_file()
    assert cache_spy == ["clear"]


def test_create_website_post_persists_empty_string_content_as_none(
    app, seeded_db, db_session
):
    post = create_website_post(
        title="Title",
        content="",
        user="alice",
        subdeaddit="testsub",
        website=_pending_website(app, public_path="www.example.test/blank.html"),
    )
    assert Post.query.get(post.id).content is None


def test_create_website_post_accepts_optional_body_text(app, seeded_db, db_session):
    post = create_website_post(
        title="Title",
        content="Found this on my walk today, thought it was neat.",
        user="alice",
        subdeaddit="testsub",
        website=_pending_website(app, public_path="www.example.test/body.html"),
    )
    assert (
        Post.query.get(post.id).content
        == "Found this on my walk today, thought it was neat."
    )


def test_create_website_post_runs_hooks_exactly_once(app, seeded_db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        content_service.notifications,
        "notify_post_created",
        lambda post: calls.append(("notify", post.id)),
    )
    monkeypatch.setattr(
        content_service.activity,
        "record_event",
        lambda **kwargs: calls.append(("activity", kwargs)),
    )
    monkeypatch.setattr(
        content_service.degeneracy,
        "detect_repetition_for_post",
        lambda post: calls.append(("degeneracy", post.id)),
    )
    cleared = []
    monkeypatch.setattr(
        content_service, "_clear_read_caches", lambda: cleared.append("clear")
    )

    post = create_website_post(
        title="Title",
        content=None,
        user="alice",
        subdeaddit="testsub",
        website=_pending_website(app, public_path="www.example.test/hooks.html"),
    )

    assert cleared == ["clear"]
    assert calls == [
        ("notify", post.id),
        ("activity", {"event_type": "post", "username": "alice", "post_id": post.id}),
        ("degeneracy", post.id),
    ]


# ---------------------------------------------------------------------------
# create_website_post - validation failures (must remove the stored file)


def test_create_website_post_rejects_empty_title_and_removes_file(app, seeded_db):
    website = _pending_website(app, public_path="www.example.test/empty-title.html")
    file_path = _file_path(app, website.storage_path)
    assert file_path.is_file()

    with pytest.raises(ContentValidationError) as exc:
        create_website_post(
            title="",
            content=None,
            user="alice",
            subdeaddit="testsub",
            website=website,
        )

    assert str(exc.value) == "Invalid post data"
    assert Post.query.count() == 0
    assert GeneratedWebsite.query.count() == 0
    assert not file_path.exists()


def test_create_website_post_unknown_user_message_and_removes_file(app, seeded_db):
    website = _pending_website(app, public_path="www.example.test/ghost-user.html")
    file_path = _file_path(app, website.storage_path)

    with pytest.raises(ContentValidationError) as exc:
        create_website_post(
            title="T",
            content=None,
            user="ghost",
            subdeaddit="testsub",
            website=website,
        )

    assert str(exc.value) == "User 'ghost' does not exist"
    assert Post.query.count() == 0
    assert GeneratedWebsite.query.count() == 0
    assert not file_path.exists()


def test_create_website_post_unknown_subdeaddit_message_and_removes_file(
    app, seeded_db
):
    website = _pending_website(app, public_path="www.example.test/ghost-sub.html")
    file_path = _file_path(app, website.storage_path)

    with pytest.raises(ContentValidationError) as exc:
        create_website_post(
            title="T",
            content=None,
            user="alice",
            subdeaddit="nope",
            website=website,
        )

    assert str(exc.value) == "Subdeaddit 'nope' does not exist"
    assert Post.query.count() == 0
    assert GeneratedWebsite.query.count() == 0
    assert not file_path.exists()


def test_create_website_post_rechecks_rate_limit_established_after_preflight(
    app, seeded_db, monkeypatch
):
    preflight_website_post(user="alice", subdeaddit="testsub", title="T")
    monkeypatch.setitem(
        content_service._RATE_LIMITS, "post", ("rate_limit_posts_per_hour", 0)
    )

    website = _pending_website(app, public_path="www.example.test/rate-limited.html")
    file_path = _file_path(app, website.storage_path)

    with pytest.raises(ContentValidationError) as exc:
        create_website_post(
            title="T",
            content=None,
            user="alice",
            subdeaddit="testsub",
            website=website,
        )

    assert str(exc.value) == "rate_limited"
    assert Post.query.count() == 0
    assert GeneratedWebsite.query.count() == 0
    assert not file_path.exists()


# ---------------------------------------------------------------------------
# create_website_post - DB failures (must remove the stored file, no hooks)


def test_create_website_post_db_failure_leaves_no_post_or_website_and_no_file(
    app, seeded_db, monkeypatch
):
    hook_calls = []
    monkeypatch.setattr(
        content_service, "_run_post_hooks", lambda post: hook_calls.append(post.id)
    )

    def _boom():
        raise SQLAlchemyError("boom")

    monkeypatch.setattr(content_service.db.session, "commit", _boom)

    website = _pending_website(app, public_path="www.example.test/db-boom.html")
    file_path = _file_path(app, website.storage_path)

    with pytest.raises(SQLAlchemyError):
        create_website_post(
            title="T",
            content=None,
            user="alice",
            subdeaddit="testsub",
            website=website,
        )

    assert Post.query.count() == 0
    assert GeneratedWebsite.query.count() == 0
    assert hook_calls == []
    assert not file_path.exists()


def test_create_website_post_public_path_collision_fails_cleanly(app, seeded_db):
    shared_public_path = "www.example.test/collide.html"

    first = create_website_post(
        title="First",
        content=None,
        user="alice",
        subdeaddit="testsub",
        website=_pending_website(app, public_path=shared_public_path),
    )

    second_website = _pending_website(app, public_path=shared_public_path)
    second_file_path = _file_path(app, second_website.storage_path)
    assert second_file_path.is_file()

    with pytest.raises(IntegrityError):
        create_website_post(
            title="Second",
            content=None,
            user="alice",
            subdeaddit="testsub",
            website=second_website,
        )

    # Only the first post/row survive; the second's file is gone.
    assert Post.query.count() == 1
    assert GeneratedWebsite.query.count() == 1
    assert GeneratedWebsite.query.one().post_id == first.id
    assert not second_file_path.exists()


# ---------------------------------------------------------------------------
# create_website_post - cleanup-itself-fails logging


def test_create_website_post_cleanup_failure_logs_opaque_path_only(
    app, seeded_db, monkeypatch, caplog
):
    website = _pending_website(app, public_path="www.example.test/cleanup-fails.html")
    file_path = _file_path(app, website.storage_path)

    def _boom(root, storage_path):
        raise OSError("disk exploded")

    monkeypatch.setattr("deaddit.websites.storage.delete_website", _boom)

    with caplog.at_level("WARNING"):
        with pytest.raises(ContentValidationError) as exc:
            create_website_post(
                title="",
                content=None,
                user="alice",
                subdeaddit="testsub",
                website=website,
            )

    # The original validation error still surfaces - a cleanup failure must
    # never mask or replace it.
    assert str(exc.value) == "Invalid post data"
    assert Post.query.count() == 0
    assert GeneratedWebsite.query.count() == 0
    # Cleanup itself failed, so the file predictably remains on disk; this
    # is exactly what Phase 5's reconciliation sweep exists for.
    assert file_path.exists()

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert website.storage_path in record.getMessage()
    assert _PRIVATE_DESCRIPTION not in record.getMessage()
    assert "http://example.test/v1" not in record.getMessage()
