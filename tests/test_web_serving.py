"""Guarded public serving for generated website pages.

The public ``/out/<hostname>/<page_name>`` route serves the generated
document with the trusted context bar injected at serve time: a sticky bar
linking back to the post's discussion, with the post title and model. The
route resolves the GeneratedWebsite row first and 404s on anything off; the
CSP sandbox remains the security boundary for the untrusted generated HTML.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from deaddit import create_app
from deaddit import db as _db
from deaddit.models import GeneratedWebsite, Post, Subdeaddit, User
from deaddit.websites.storage import store_website

_EXPECTED_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; base-uri 'none'; connect-src 'none'; "
    "form-action 'none'; frame-ancestors 'none'; img-src data:; "
    "font-src data:; media-src data:; object-src 'none'; worker-src 'none'; "
    "style-src 'unsafe-inline'; script-src 'unsafe-inline'; sandbox allow-scripts"
)
_EXPECTED_X_CONTENT_TYPE_OPTIONS = "nosniff"
_EXPECTED_REFERRER_POLICY = "no-referrer"
_EXPECTED_PERMISSIONS_POLICY = (
    "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
)

#: What old generation runs baked into the stored document; serving replaces
#: it with the dynamic bar.
_LEGACY_BAR = (
    '<div data-deaddit-navigation="true" style="display:block;">'
    '<a href="/">← Back to deaddit</a></div>'
)


@dataclass(frozen=True)
class _WebsitePaths:
    post_id: int
    public_path: str
    storage_path: str


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
    return _db.session


def _make_website_post(
    app,
    db_session,
    *,
    hostname="www.fake-observatory.com",
    page_name="aurora-map.html",
    html="<html><body>hello</body></html>",
    storage_path: str | None = None,
) -> _WebsitePaths:
    stored = store_website(
        html.encode("utf-8"), Path(app.config["GENERATED_WEBSITES_ROOT"])
    )
    post = Post(
        title="Aurora map",
        content="A generated website",
        subdeaddit_name="testsub",
        user="alice",
        llm_model="website-test-model",
    )
    db_session.add(post)
    db_session.flush()
    website = GeneratedWebsite(
        post_id=post.id,
        public_path=f"{hostname}/{page_name}",
        storage_path=storage_path or stored.storage_path,
        hostname=hostname,
        page_name=page_name,
        source_description="A private source description",
        byte_size=stored.byte_size,
        sha256=stored.sha256,
        creator_username_snapshot="alice",
        api_url_snapshot="https://llm.example/v1",
        model_snapshot="test-model",
    )
    db_session.add(website)
    db_session.commit()
    return _WebsitePaths(post.id, website.public_path, website.storage_path)


def test_public_page_serves_document_with_context_bar_and_headers(
    app, client, db_session
):
    html = (
        "<html><body>snowman: ☃"
        "<script>document.body.dataset.ready = 'yes';</script></body></html>"
    )
    paths = _make_website_post(app, db_session, html=html)

    response = client.get(f"/out/{paths.public_path}")

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "text/html; charset=utf-8"
    assert response.headers["Cache-Control"] == "public, max-age=300"
    assert (
        response.headers["Content-Security-Policy"] == _EXPECTED_CONTENT_SECURITY_POLICY
    )
    assert (
        response.headers["X-Content-Type-Options"] == _EXPECTED_X_CONTENT_TYPE_OPTIONS
    )
    assert response.headers["Referrer-Policy"] == _EXPECTED_REFERRER_POLICY
    assert response.headers["Permissions-Policy"] == _EXPECTED_PERMISSIONS_POLICY

    csp = response.headers["Content-Security-Policy"]
    assert "sandbox allow-scripts" in csp
    assert "allow-same-origin" not in csp
    assert "connect-src 'none'" in csp
    assert "form-action 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    for token in (
        "allow-forms",
        "allow-popups",
        "allow-top-navigation",
        "allow-top-navigation-by-user-activation",
        "allow-downloads",
        "allow-storage-access-by-user-activation",
    ):
        assert token not in csp

    body = response.get_data(as_text=True)
    assert body.count("data-deaddit-navigation") == 1
    # The bar sits at the start of the body and the document is otherwise
    # untouched.
    assert body.startswith("<html><body>")
    assert body.index("data-deaddit-navigation") < body.index("snowman")
    assert "<script>document.body.dataset.ready = 'yes';</script></body></html>" in body
    # Back link targets the discussion, not the home page; title and model
    # come along; the bar sticks to the top.
    assert f'href="/d/testsub/{paths.post_id}"' in body
    assert "Back to post" in body
    assert "Aurora map" in body
    assert "website-test-model" in body
    assert "position:sticky" in body


def test_legacy_baked_home_link_bar_is_replaced(app, client, db_session):
    html = f"<html><body>{_LEGACY_BAR}<p>site</p></body></html>"
    paths = _make_website_post(app, db_session, html=html)

    body = client.get(f"/out/{paths.public_path}").get_data(as_text=True)

    assert body.count("data-deaddit-navigation") == 1
    assert "Back to deaddit" not in body
    assert 'href="/"' not in body
    assert "<p>site</p>" in body


def test_public_page_404_matrix_and_never_opens_request_path(app, client, db_session):
    paths = _make_website_post(app, db_session)
    stored_file = Path(app.config["GENERATED_WEBSITES_ROOT"]) / paths.storage_path

    stored_file.unlink()
    missing_file = client.get(f"/out/{paths.public_path}")
    assert missing_file.status_code == 404

    assert client.get("/out/unknown.example/not-there.html").status_code == 404
    assert (
        client.get("/out/www.fake-observatory.com/wrong-page.html").status_code == 404
    )
    assert client.get("/out/..%2f..%2fetc%2fpasswd").status_code == 404
    assert client.get("/out/../../etc/passwd").status_code == 404
    assert client.get("/out/..%2fpages/evil.html").status_code == 404
    assert client.get("/out/www.fake-observatory.com/%00evil.html").status_code == 404
    assert client.get("/out/%zz").status_code == 404


def test_hostile_storage_path_is_rejected_after_row_lookup(app, client, db_session):
    paths = _make_website_post(
        app,
        db_session,
        storage_path="../../etc/passwd",
    )

    response = client.get(f"/out/{paths.public_path}")

    assert response.status_code == 404
