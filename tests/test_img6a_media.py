"""Guarded media serving and public image serialization (plan 6A).

The original/thumbnail routes resolve a concrete PostImage row before ever
touching a file: an unknown filename, a traversal attempt, a filename that
belongs to the other variant, a missing file on disk, and a soft-removed
post's own file all return 404. Public list/detail JSON exposes only the
public image contract (URLs, dimensions, alt text) - never the private
generation prompt, provider snapshots, request IDs, or filesystem paths -
and removed posts never surface an image at all.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from deaddit import create_app
from deaddit import db as _db
from deaddit.images.storage import store_variants
from deaddit.models import Post, PostImage, Subdeaddit, User

_PRIVATE_PROMPT = "a private generation prompt that must never leak publicly"


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
def db_session(app):
    return _db.session


def _solid_png(color=(10, 20, 30), size=(16, 16)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def _seed(db_session):
    db_session.add_all(
        [
            User(username="alice", bio="", interests="[]"),
            Subdeaddit(name="testsub", description="A test subdeaddit"),
        ]
    )
    db_session.commit()


def _make_image_post(
    app, db_session, *, title="A photo", content="body text", removed=False
) -> tuple[Post, PostImage]:
    root = app.config["GENERATED_IMAGES_ROOT"]
    stored = store_variants(_solid_png(), Path(root))
    post = Post(
        title=title,
        content=content,
        subdeaddit_name="testsub",
        user="alice",
        removed=removed,
    )
    db_session.add(post)
    db_session.flush()
    image = PostImage(
        post_id=post.id,
        original_path=stored.original_path,
        thumbnail_path=stored.thumbnail_path,
        mime_type=stored.mime_type,
        byte_size=stored.original_size,
        width=stored.width,
        height=stored.height,
        alt_text="A solid test square",
        source_prompt=_PRIVATE_PROMPT,
        provider_snapshot="Fal",
        model_snapshot="fal-ai/flux-1/schnell",
        request_snapshot="req-secret-1",
    )
    db_session.add(image)
    db_session.commit()
    return post, image


class TestMediaServing:
    def test_original_and_thumbnail_served_with_correct_mime_and_size(
        self, app, client, db_session
    ):
        _seed(db_session)
        _post, image = _make_image_post(app, db_session)

        resp = client.get(f"/media/images/original/{Path(image.original_path).name}")
        assert resp.status_code == 200
        assert resp.mimetype == "image/png"
        assert resp.content_length == image.byte_size

        thumb_resp = client.get(
            f"/media/images/thumbnail/{Path(image.thumbnail_path).name}"
        )
        assert thumb_resp.status_code == 200
        assert thumb_resp.mimetype == "image/png"

    def test_cache_headers_are_bounded_public_not_immutable(
        self, app, client, db_session
    ):
        _seed(db_session)
        _post, image = _make_image_post(app, db_session)

        resp = client.get(f"/media/images/original/{Path(image.original_path).name}")
        cache_control = resp.headers["Cache-Control"]
        assert cache_control == "public, max-age=300"
        assert "immutable" not in cache_control

    def test_unknown_filename_returns_404(self, client):
        assert (
            client.get("/media/images/original/does-not-exist.png").status_code == 404
        )
        assert (
            client.get("/media/images/thumbnail/does-not-exist.png").status_code == 404
        )

    def test_traversal_attempts_return_404(self, client):
        assert (
            client.get("/media/images/original/..%2f..%2fetc%2fpasswd").status_code
            == 404
        )
        assert client.get("/media/images/original/../../etc/passwd").status_code == 404

    def test_thumbnail_filename_is_rejected_on_original_route(
        self, app, client, db_session
    ):
        _seed(db_session)
        _post, image = _make_image_post(app, db_session)

        thumb_name = Path(image.thumbnail_path).name
        assert client.get(f"/media/images/original/{thumb_name}").status_code == 404

    def test_removed_post_media_returns_404_for_both_variants(
        self, app, client, db_session
    ):
        _seed(db_session)
        _post, image = _make_image_post(app, db_session, removed=True)

        original_resp = client.get(
            f"/media/images/original/{Path(image.original_path).name}"
        )
        thumbnail_resp = client.get(
            f"/media/images/thumbnail/{Path(image.thumbnail_path).name}"
        )
        assert original_resp.status_code == 404
        assert thumbnail_resp.status_code == 404

    def test_missing_file_on_disk_returns_404(self, app, client, db_session):
        _seed(db_session)
        _post, image = _make_image_post(app, db_session)

        root = Path(app.config["GENERATED_IMAGES_ROOT"])
        (root / image.original_path).unlink()

        resp = client.get(f"/media/images/original/{Path(image.original_path).name}")
        assert resp.status_code == 404


class TestPublicSerialization:
    def test_api_posts_list_exposes_public_image_contract_only(
        self, app, client, db_session
    ):
        _seed(db_session)
        post, _image = _make_image_post(app, db_session)

        payload = client.get("/api/posts").get_json()
        entry = next(p for p in payload["posts"] if p["id"] == post.id)
        img = entry["image"]
        assert set(img.keys()) == {
            "original_url",
            "thumbnail_url",
            "mime_type",
            "width",
            "height",
            "alt_text",
        }
        assert img["original_url"].startswith("/media/images/original/")
        assert img["thumbnail_url"].startswith("/media/images/thumbnail/")
        assert img["mime_type"] == "image/png"
        assert img["alt_text"] == "A solid test square"

    def test_api_post_detail_exposes_public_image_contract_only(
        self, app, client, db_session
    ):
        _seed(db_session)
        post, _image = _make_image_post(app, db_session)

        payload = client.get(f"/api/post/{post.id}").get_json()
        img = payload["image"]
        assert set(img.keys()) == {
            "original_url",
            "thumbnail_url",
            "mime_type",
            "width",
            "height",
            "alt_text",
        }

    def test_removed_post_suppresses_image_in_detail_and_list(
        self, app, client, db_session
    ):
        _seed(db_session)
        post, _image = _make_image_post(app, db_session, removed=True)

        detail_payload = client.get(f"/api/post/{post.id}").get_json()
        assert detail_payload["image"] is None

        list_payload = client.get("/api/posts").get_json()
        assert all(p["id"] != post.id for p in list_payload["posts"])

    def test_text_post_has_null_image(self, app, client, db_session):
        _seed(db_session)
        post = Post(
            title="just text",
            content="hello",
            subdeaddit_name="testsub",
            user="alice",
        )
        db_session.add(post)
        db_session.commit()

        list_payload = client.get("/api/posts").get_json()
        entry = next(p for p in list_payload["posts"] if p["id"] == post.id)
        assert entry["image"] is None

        detail_payload = client.get(f"/api/post/{post.id}").get_json()
        assert detail_payload["image"] is None

    def test_image_post_with_none_content_serializes_null_safely(
        self, app, client, db_session
    ):
        _seed(db_session)
        post, _image = _make_image_post(app, db_session, content=None)

        detail_payload = client.get(f"/api/post/{post.id}").get_json()
        assert detail_payload["content"] is None
        assert detail_payload["image"] is not None

        list_payload = client.get("/api/posts").get_json()
        entry = next(p for p in list_payload["posts"] if p["id"] == post.id)
        assert entry["content"] is None

    def test_private_fields_never_appear_in_public_payloads(
        self, app, client, db_session
    ):
        _seed(db_session)
        post, _image = _make_image_post(app, db_session)

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

        list_text = client.get("/api/posts").get_data(as_text=True)
        detail_text = client.get(f"/api/post/{post.id}").get_data(as_text=True)
        for blob in (list_text, detail_text):
            for needle in forbidden:
                assert needle not in blob, f"leaked {needle!r}"
