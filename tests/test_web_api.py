"""Public and admin website payloads never expose generation provenance."""

from __future__ import annotations

from pathlib import Path

import pytest

from deaddit import create_app
from deaddit import db as _db
from deaddit.models import GeneratedWebsite, Post, Subdeaddit, User
from deaddit.websites.storage import store_website

_PRIVATE_SENTINELS = (
    "SECRET-DESC-9f1a",
    "http://secret-endpoint-9f1a/v1",
    "secret-model-9f1a",
    "req-secret-9f1a",
    "918231",
    "918232",
    "918233",
    "secret-stop-reason-9f1a",
)


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
def admin_client(client):
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


@pytest.fixture()
def db_session(app):
    _db.session.add_all(
        [
            User(username="alice", bio="", interests="[]"),
            Subdeaddit(name="testsub", description="A test subdeaddit"),
        ]
    )
    _db.session.commit()
    return _db.session


def _make_website_post(app, db_session, *, removed=False):
    hostname = "www.redaction-example.test"
    page_name = "aurora-9f1a.html"
    stored = store_website(
        b"<html><body>Safe generated website</body></html>",
        Path(app.config["GENERATED_WEBSITES_ROOT"]),
    )
    post = Post(
        title="website-redaction-token",
        content="Public commentary for the website post.",
        subdeaddit_name="testsub",
        user="alice",
        removed=removed,
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
            source_description=_PRIVATE_SENTINELS[0],
            byte_size=stored.byte_size,
            sha256=stored.sha256,
            creator_username_snapshot="alice",
            api_url_snapshot=_PRIVATE_SENTINELS[1],
            model_snapshot=_PRIVATE_SENTINELS[2],
            request_id=_PRIVATE_SENTINELS[3],
            prompt_tokens=int(_PRIVATE_SENTINELS[4]),
            completion_tokens=int(_PRIVATE_SENTINELS[5]),
            total_tokens=int(_PRIVATE_SENTINELS[6]),
            finish_reason=_PRIVATE_SENTINELS[7],
        )
    )
    db_session.commit()
    return post.id, hostname, page_name, stored.storage_path


def _website_payload(hostname, page_name):
    return {
        "url": f"/out/{hostname}/{page_name}",
        "hostname": hostname,
        "page_name": page_name,
    }


def test_public_and_admin_website_payloads_redact_provenance(
    app, client, admin_client, db_session
):
    post_id, hostname, page_name, storage_path = _make_website_post(app, db_session)
    text_post = Post(
        title="plain-text-redaction-token",
        content="A text post in the same listing.",
        subdeaddit_name="testsub",
        user="alice",
    )
    db_session.add(text_post)
    db_session.commit()

    expected_website = _website_payload(hostname, page_name)
    public_listing = client.get("/api/posts")
    assert public_listing.status_code == 200
    listed = {entry["id"]: entry for entry in public_listing.get_json()["posts"]}
    assert listed[post_id]["website"] == expected_website
    assert set(listed[post_id]["website"]) == {"url", "hostname", "page_name"}
    assert listed[text_post.id]["website"] is None

    public_detail = client.get(f"/api/post/{post_id}")
    assert public_detail.status_code == 200
    assert public_detail.get_json()["website"] == expected_website

    public_text_detail = client.get(f"/api/post/{text_post.id}")
    assert public_text_detail.status_code == 200
    assert public_text_detail.get_json()["website"] is None

    admin_listing = admin_client.get("/admin/api/posts")
    assert admin_listing.status_code == 200
    admin_entries = {entry["id"]: entry for entry in admin_listing.get_json()["posts"]}
    assert admin_entries[post_id]["website"] == expected_website
    assert admin_entries[text_post.id]["website"] is None

    admin_detail = admin_client.get(f"/admin/api/posts/{post_id}")
    assert admin_detail.status_code == 200
    assert admin_detail.get_json()["website"] == expected_website

    admin_text_detail = admin_client.get(f"/admin/api/posts/{text_post.id}")
    assert admin_text_detail.status_code == 200
    assert admin_text_detail.get_json()["website"] is None

    storage_uuid = Path(storage_path).stem
    responses = [
        public_listing,
        public_detail,
        admin_listing,
        admin_detail,
        client.get("/"),
        client.get("/d/testsub"),
        client.get(f"/d/testsub/{post_id}"),
        client.get("/user/alice"),
        client.get("/search?q=website-redaction-token"),
    ]
    forbidden = (*_PRIVATE_SENTINELS, storage_path, storage_uuid, "pages/")
    for response in responses:
        body = response.get_data(as_text=True)
        for needle in forbidden:
            assert needle not in body, f"leaked {needle!r} in {response.request.path}"

    db_session.query(Post).filter_by(id=post_id).update({"removed": True})
    db_session.commit()

    removed_detail = client.get(f"/api/post/{post_id}")
    assert removed_detail.status_code == 200
    assert removed_detail.get_json()["website"] is None
    assert all(
        entry["id"] != post_id for entry in client.get("/api/posts").get_json()["posts"]
    )

    removed_admin = admin_client.get(f"/admin/api/posts/{post_id}")
    assert removed_admin.status_code == 200
    assert removed_admin.get_json()["website"] == expected_website
