"""The public image surface: media serving, JSON payloads, rendering, lifecycle.

The original/thumbnail routes resolve a concrete PostImage row before ever
touching a file, so an unknown filename, a traversal attempt, a filename that
belongs to the other variant, and a missing file on disk all return 404. Public
JSON and HTML expose only the public image contract (URLs, dimensions, alt text)
never the private generation prompt, provider snapshots, request IDs, or
filesystem paths. Every hard-delete route that can remove a post must also
remove that post's stored files.

Path strings are captured immediately after a fixture creates a ``PostImage``
row: once a hard-delete route removes the row, the same session's identity map
holds an expired instance and re-touching its attributes raises
``ObjectDeletedError`` instead of the answer the test wants.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pytest
from click.testing import CliRunner
from PIL import Image

from deaddit import create_app
from deaddit import db as _db
from deaddit.images import cli as images_cli
from deaddit.images.storage import store_variants
from deaddit.models import Post, PostImage, Setting, Subdeaddit, User

_PRIVATE_PROMPT = "a private generation prompt that must never leak publicly"
_PUBLIC_IMAGE_KEYS = {
    "original_url",
    "thumbnail_url",
    "mime_type",
    "width",
    "height",
    "alt_text",
}
STATIC_ROOT = Path(__file__).resolve().parents[1] / "deaddit/static"


@dataclass(frozen=True)
class _ImagePaths:
    post_id: int
    original_path: str
    thumbnail_path: str


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "TESTING": True,
            "GENERATED_IMAGES_ROOT": str(tmp_path / "media"),
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
            User(username="bob", bio="", interests="[]"),
            Subdeaddit(name="testsub", description="A test subdeaddit"),
            Subdeaddit(name="othersub", description="Another test subdeaddit"),
        ]
    )
    _db.session.commit()
    Setting.set_value("SETUP_COMPLETED_AT", "2026-01-01T00:00:00Z")
    return _db.session


def _solid_png(color=(200, 50, 10), size=(640, 360)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def _make_image_post(
    app,
    db_session,
    *,
    title="A photo",
    content="body text",
    user="alice",
    subdeaddit="testsub",
) -> _ImagePaths:
    root = Path(app.config["GENERATED_IMAGES_ROOT"])
    stored = store_variants(_solid_png(color=(title.encode()[0], 50, 10)), root)
    post = Post(
        title=title,
        content=content,
        subdeaddit_name=subdeaddit,
        user=user,
    )
    db_session.add(post)
    db_session.flush()
    db_session.add(
        PostImage(
            post_id=post.id,
            original_path=stored.original_path,
            thumbnail_path=stored.thumbnail_path,
            mime_type=stored.mime_type,
            byte_size=stored.original_size,
            width=stored.width,
            height=stored.height,
            alt_text="A solid orange rectangle",
            source_prompt=_PRIVATE_PROMPT,
            provider_snapshot="Fal",
            model_snapshot="fal-ai/flux-1/schnell",
            request_snapshot="req-secret-1",
        )
    )
    db_session.commit()
    return _ImagePaths(post.id, stored.original_path, stored.thumbnail_path)


def _make_text_post(db_session, *, title="Just words") -> Post:
    post = Post(
        title=title,
        content="Some plain text body.",
        subdeaddit_name="testsub",
        user="alice",
    )
    db_session.add(post)
    db_session.commit()
    return post


def _files_exist(app, paths: _ImagePaths) -> bool:
    root = Path(app.config["GENERATED_IMAGES_ROOT"])
    return (root / paths.original_path).is_file() and (
        root / paths.thumbnail_path
    ).is_file()


def _media_link(body: str, *, href: str, label: str) -> str:
    marker = '<a class="post-card__media-link"'
    start = body.index(marker)
    opening_end = body.index(">", start)
    opening = body[start:opening_end]
    assert f'href="{href}"' in opening
    assert f'aria-label="{label}"' in opening
    end = body.index("</a>", opening_end)
    return body[start:end]


def _assert_media_link(
    body: str, *, href: str, label: str, thumbnail_url: str, original_url: str
) -> None:
    media_link = _media_link(body, href=href, label=label)
    assert 'class="post-card__thumb"' in media_link
    assert f'src="{thumbnail_url}"' in media_link
    assert f'data-original-src="{original_url}"' in media_link


def test_media_routes_serve_only_files_owned_by_a_live_post(app, client, db_session):
    paths = _make_image_post(app, db_session)
    image = PostImage.query.one()
    original_name = Path(paths.original_path).name
    thumbnail_name = Path(paths.thumbnail_path).name

    original = client.get(f"/media/images/original/{original_name}")
    assert original.status_code == 200
    assert original.mimetype == "image/png"
    assert original.content_length == image.byte_size
    assert original.headers["Cache-Control"] == "public, max-age=300"

    thumbnail = client.get(f"/media/images/thumbnail/{thumbnail_name}")
    assert thumbnail.status_code == 200
    assert thumbnail.mimetype == "image/png"

    for path in (
        "/media/images/original/does-not-exist.png",
        "/media/images/thumbnail/does-not-exist.png",
        "/media/images/original/..%2f..%2fetc%2fpasswd",
        "/media/images/original/../../etc/passwd",
        # A filename belonging to the other variant is not servable here.
        f"/media/images/original/{thumbnail_name}",
    ):
        assert client.get(path).status_code == 404, path

    # A row whose file has vanished 404s rather than erroring.
    (Path(app.config["GENERATED_IMAGES_ROOT"]) / paths.original_path).unlink()
    assert client.get(f"/media/images/original/{original_name}").status_code == 404


def test_public_json_and_html_show_images_without_leaking_private_metadata(
    app, client, db_session
):
    paths = _make_image_post(app, db_session)
    image = PostImage.query.one()
    original_name = Path(paths.original_path).name
    thumbnail_name = Path(paths.thumbnail_path).name

    listed = next(
        entry
        for entry in client.get("/api/posts").get_json()["posts"]
        if entry["id"] == paths.post_id
    )
    detail = client.get(f"/api/post/{paths.post_id}").get_json()
    assert set(listed["image"]) == _PUBLIC_IMAGE_KEYS
    assert set(detail["image"]) == _PUBLIC_IMAGE_KEYS
    assert listed["image"]["original_url"].startswith("/media/images/original/")
    assert listed["image"]["thumbnail_url"].startswith("/media/images/thumbnail/")
    assert listed["image"]["alt_text"] == "A solid orange rectangle"

    forbidden = (
        "source_prompt",
        _PRIVATE_PROMPT,
        "provider_snapshot",
        "model_snapshot",
        "request_snapshot",
        "req-secret-1",
        "fal-ai/flux-1/schnell",
        str(Path(app.config["GENERATED_IMAGES_ROOT"])),
        "provider_id",
        "byte_size",
    )
    for blob in (
        client.get("/api/posts").get_data(as_text=True),
        client.get(f"/api/post/{paths.post_id}").get_data(as_text=True),
        client.get("/").get_data(as_text=True),
        client.get(f"/d/testsub/{paths.post_id}").get_data(as_text=True),
    ):
        for needle in forbidden:
            assert needle not in blob, f"leaked {needle!r}"

    # All four shared feed surfaces render through post_list/post_card.
    for url in ("/", "/d/testsub", "/user/alice", "/search?q=photo"):
        html = client.get(url).get_data(as_text=True)
        _assert_media_link(
            html,
            href=f"/d/testsub/{paths.post_id}",
            label="Open post",
            thumbnail_url=f"/media/images/thumbnail/{thumbnail_name}",
            original_url=f"/media/images/original/{original_name}",
        )
        assert 'class="post-card__thumb"' in html, url
        assert f"/media/images/thumbnail/{thumbnail_name}" in html, url
        # Explicit dimensions are the layout-shift guard.
        assert f'width="{image.width}"' in html and f'height="{image.height}"' in html
        assert 'loading="lazy"' in html
        assert "A solid orange rectangle" in html
        # The full-size source rides along for JS to swap in on expand.
        assert f'data-original-src="/media/images/original/{original_name}"' in html

    # The detail page shows the normalized original, and not the thumbnail.
    detail_html = client.get(f"/d/testsub/{paths.post_id}").get_data(as_text=True)
    assert 'class="post-detail__image"' in detail_html
    assert f"/media/images/original/{original_name}" in detail_html
    assert "/media/images/thumbnail/" not in detail_html

    # The image-feed.js script module is loaded.
    front = client.get("/").get_data(as_text=True)
    assert '<script type="module" src="/static/js/image-feed.js"></script>' in front

    # An image-only body leaves no empty text container behind, in either view.
    image_only = _make_image_post(app, db_session, title="Bodyless", content=None)
    body_html = client.get(f"/d/testsub/{image_only.post_id}").get_data(as_text=True)
    assert 'class="post-body"' not in body_html
    assert 'class="post-detail__image"' in body_html

    # A text-only post carries no image markup and a null image in JSON.
    _db.session.query(PostImage).delete()
    _db.session.query(Post).delete()
    _db.session.commit()
    # Bulk deletes bypass the session: drop stale identity-map entries so
    # the reused SQLite rowid cannot collide with a dead instance.
    _db.session.expunge_all()
    text_post = _make_text_post(db_session)
    text_html = client.get("/").get_data(as_text=True)
    assert "post-card__media" not in text_html
    assert "post-card__thumb" not in text_html
    assert "data-original-src" not in text_html
    assert 'class="post-card__preview"' in text_html
    text_detail = client.get(f"/d/testsub/{text_post.id}").get_data(as_text=True)
    assert 'class="post-body"' in text_detail
    assert 'class="post-detail__image"' not in text_detail
    assert client.get(f"/api/post/{text_post.id}").get_json()["image"] is None

    # The expand behaviour is delegated once on `document`, so HTMX-appended
    # cards never accumulate duplicate listeners, and hidden purely via CSS.
    js = (STATIC_ROOT / "js/image-feed.js").read_text()
    assert "document.addEventListener('click'" in js
    assert "data-image-toggle" in js
    for hook in ("htmx:afterSwap", "htmx:load"):
        assert hook in js
    css = (STATIC_ROOT / "style.css").read_text()
    assert ".feed-wrap:has(.post-card__media) .image-feed-toolbar" in css
    assert ".post-card__expand" in css
    assert ".post-card__thumb.is-expanded" in css


def test_hard_deletes_remove_files_and_reconciliation_defaults_to_a_dry_run(
    app, admin_client, db_session, monkeypatch
):
    def ok(response):
        assert response.status_code == 200 and response.get_json()["success"] is True
        return response

    # Single post delete, including a text post with no image at all.
    single = _make_image_post(app, db_session, title="Single")
    ok(admin_client.delete(f"/admin/api/posts/{single.post_id}"))
    assert not _files_exist(app, single)
    assert PostImage.query.filter_by(post_id=single.post_id).first() is None
    ok(admin_client.delete(f"/admin/api/posts/{_make_text_post(db_session).id}"))

    # Bulk post delete.
    first = _make_image_post(app, db_session, title="First")
    second = _make_image_post(app, db_session, title="Second")
    bulk = ok(
        admin_client.post(
            "/admin/api/posts/bulk-delete",
            json={"post_ids": [first.post_id, second.post_id]},
        )
    )
    assert bulk.get_json()["deleted"]["posts"] == 2
    assert not _files_exist(app, first) and not _files_exist(app, second)

    # User delete, single and bulk - these routes bypass ORM cascades, so the
    # files must be collected before the rows go.
    owned = _make_image_post(app, db_session, title="Owned", user="alice")
    ok(admin_client.delete("/admin/api/users/alice"))
    assert not _files_exist(app, owned)
    assert db_session.get(Post, owned.post_id) is None
    db_session.add(User(username="alice", bio="", interests="[]"))
    db_session.commit()

    alice = _make_image_post(app, db_session, title="Alice", user="alice")
    bob = _make_image_post(app, db_session, title="Bob", user="bob")
    ok(
        admin_client.post(
            "/admin/api/users/bulk-delete", json={"usernames": ["alice", "bob"]}
        )
    )
    assert not _files_exist(app, alice) and not _files_exist(app, bob)
    db_session.add_all(
        [
            User(username="alice", bio="", interests="[]"),
            User(username="bob", bio="", interests="[]"),
        ]
    )
    db_session.commit()

    # Subdeaddit delete only touches its own posts.
    doomed = _make_image_post(app, db_session, title="Doomed", subdeaddit="testsub")
    spared = _make_image_post(app, db_session, title="Spared", subdeaddit="othersub")
    ok(admin_client.delete("/admin/api/subdeaddits/testsub"))
    assert not _files_exist(app, doomed)
    assert _files_exist(app, spared)
    assert db_session.get(Post, spared.post_id) is not None
    ok(
        admin_client.post(
            "/admin/api/subdeaddits/bulk-delete", json={"names": ["othersub"]}
        )
    )
    assert not _files_exist(app, spared)

    # Reconciliation: dry run by default, and never touches referenced files.
    db_session.add(Subdeaddit(name="testsub", description="A test subdeaddit"))
    db_session.commit()
    monkeypatch.setattr(images_cli, "create_app", lambda *a, **k: app)
    from deaddit.cli import cli

    runner = CliRunner()
    root = Path(app.config["GENERATED_IMAGES_ROOT"])
    orphan = root / store_variants(_solid_png(), root).original_path
    kept = _make_image_post(app, db_session, title="Kept")
    missing = _make_image_post(app, db_session, title="Missing")
    (root / missing.original_path).unlink()
    db_session.commit()

    dry_run = runner.invoke(cli, ["images", "reconcile-media"])
    assert dry_run.exit_code == 0
    assert "dry-run" in dry_run.output
    assert orphan.name in dry_run.output
    assert "missing files" in dry_run.output
    assert orphan.is_file(), "a dry run must never delete files"

    # An apply against a production database needs an explicit override.
    monkeypatch.setattr(
        images_cli.seeding, "_resolves_to_production", lambda *a, **k: True
    )
    refused = runner.invoke(cli, ["images", "reconcile-media", "--apply"])
    assert refused.exit_code != 0
    assert "production" in refused.output.lower()
    assert orphan.is_file()

    applied = runner.invoke(
        cli, ["images", "reconcile-media", "--apply", "--i-know-this-is-prod"]
    )
    assert applied.exit_code == 0
    assert not orphan.is_file()
    assert _files_exist(app, kept)


def test_regenerate_thumbnails_cli_rebuilds_legacy_thumbnails(
    app, db_session, monkeypatch
):
    monkeypatch.setattr(images_cli, "create_app", lambda *a, **k: app)
    from deaddit.cli import cli

    runner = CliRunner()
    root = Path(app.config["GENERATED_IMAGES_ROOT"])

    # Legacy state: a post whose thumbnail was written small (20px here)
    # under the old default-quality pipeline, plus a row whose original
    # file has gone missing and can only be skipped.
    source = Image.new("RGB", (1600, 1200), color=(40, 100, 200))
    buf = BytesIO()
    source.save(buf, format="JPEG")
    legacy_stored = store_variants(buf.getvalue(), root, thumbnail_max=20)
    post = Post(
        title="Legacy",
        content="body text",
        subdeaddit_name="testsub",
        user="alice",
    )
    db_session.add(post)
    db_session.flush()
    db_session.add(
        PostImage(
            post_id=post.id,
            original_path=legacy_stored.original_path,
            thumbnail_path=legacy_stored.thumbnail_path,
            mime_type=legacy_stored.mime_type,
            byte_size=legacy_stored.original_size,
            width=legacy_stored.width,
            height=legacy_stored.height,
            alt_text="A solid blue rectangle",
            source_prompt=_PRIVATE_PROMPT,
            provider_snapshot="Fal",
            model_snapshot="fal-ai/flux-1-schnell",
            request_snapshot="req-secret-1",
        )
    )
    gone = _make_image_post(app, db_session, title="Gone")
    (root / gone.original_path).unlink()
    db_session.commit()

    thumbnail_file = root / legacy_stored.thumbnail_path
    with Image.open(thumbnail_file) as thumb:
        assert thumb.size == (20, 15)

    before = thumbnail_file.read_bytes()
    dry_run = runner.invoke(cli, ["images", "regenerate-thumbnails"])
    assert dry_run.exit_code == 0
    assert "dry-run" in dry_run.output
    assert f"post_id={gone.post_id}" in dry_run.output
    assert thumbnail_file.read_bytes() == before, "a dry run must never rewrite files"

    # An apply against a production database needs an explicit override.
    monkeypatch.setattr(
        images_cli.seeding, "_resolves_to_production", lambda *a, **k: True
    )
    refused = runner.invoke(cli, ["images", "regenerate-thumbnails", "--apply"])
    assert refused.exit_code != 0
    assert "production" in refused.output.lower()

    applied = runner.invoke(
        cli,
        ["images", "regenerate-thumbnails", "--apply", "--i-know-this-is-prod"],
    )
    assert applied.exit_code == 0
    assert "Regenerated 1 thumbnail(s)" in applied.output
    # Same URL-bearing filename, now sized for the feed column; the
    # original is untouched and the missing-original row was skipped.
    with Image.open(thumbnail_file) as thumb:
        assert thumb.size == (800, 600)
    with Image.open(root / legacy_stored.original_path) as original:
        assert original.size == (1600, 1200)
