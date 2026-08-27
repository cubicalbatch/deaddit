"""Per-image and feed-wide expand/minimize plumbing (plan 6C).

The actual expand/minimize behavior lives in client-side JS
(``deaddit/static/js/image-feed.js``) and is exercised in a real browser, not
here. This module covers the small server-rendered contract that JS depends
on: the thumbnail carries the full-image URL as inert data, the module is
actually wired into every page, and the CSS support classes it toggles
exist.
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


def _solid_png(color=(10, 120, 200), size=(640, 360)) -> bytes:
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


def _make_image_post(app, db_session, *, title="A photo") -> tuple[Post, PostImage]:
    root = app.config["GENERATED_IMAGES_ROOT"]
    stored = store_variants(_solid_png(), Path(root))
    post = Post(
        title=title, content="body text", subdeaddit_name="testsub", user="alice"
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
        alt_text="A solid blue rectangle",
        source_prompt="a private prompt",
        provider_snapshot="Fal",
        model_snapshot="fal-ai/flux-1/schnell",
        request_snapshot="req-1",
    )
    db_session.add(image)
    db_session.commit()
    return post, image


class TestExpandDataWiring:
    def test_card_thumb_carries_original_src_data_attribute(
        self, app, client, db_session
    ):
        _seed(db_session)
        _post, image = _make_image_post(app, db_session)

        html = client.get("/").get_data(as_text=True)
        original_name = Path(image.original_path).name
        thumb_name = Path(image.thumbnail_path).name

        assert f'data-original-src="/media/images/original/{original_name}"' in html
        # The visible src is still the thumbnail; JS swaps it on expand.
        assert f'src="/media/images/thumbnail/{thumb_name}"' in html

    def test_text_only_card_has_no_original_src_attribute(
        self, app, client, db_session
    ):
        _seed(db_session)
        db_session.add(
            Post(
                title="Just words",
                content="Some plain text body.",
                subdeaddit_name="testsub",
                user="alice",
            )
        )
        db_session.commit()

        html = client.get("/").get_data(as_text=True)
        assert "data-original-src" not in html


class TestImageFeedScriptWiring:
    def test_base_page_loads_image_feed_module(self, app, client, db_session):
        _seed(db_session)
        html = client.get("/").get_data(as_text=True)
        assert '<script type="module" src="/static/js/image-feed.js"></script>' in html

    def test_image_feed_module_uses_delegated_listeners(self):
        """The click handling must be registered once on `document`, not
        per-card, so HTMX-appended cards never accumulate duplicate
        listeners."""
        js_path = (
            Path(__file__).resolve().parents[1] / "deaddit/static/js/image-feed.js"
        )
        js = js_path.read_text()
        assert "document.addEventListener('click'" in js
        assert "data-image-toggle" in js
        assert "data-feed-image-action" in js
        for hook in ("htmx:afterSwap", "htmx:load"):
            assert hook in js


class TestExpandCss:
    def test_expand_support_classes_present(self):
        css_path = Path(__file__).resolve().parents[1] / "deaddit/static/style.css"
        css = css_path.read_text()
        assert ".post-card__expand" in css
        assert ".post-card__thumb.is-expanded" in css
