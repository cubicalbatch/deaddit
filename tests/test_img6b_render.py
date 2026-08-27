"""Responsive image cards and detail rendering (plan 6B).

Feed cards across all four shared surfaces (front page, subdeaddit, user
profile, search) and the post detail page must render a post's image with
explicit width/height (layout-shift guard) and descriptive alt text, and an
image-only body (``content is None``) must never leave an empty text
container behind. The feed-wide image toolbar lives outside the
``.feed-page`` fragment so HTMX's ``hx-select=".feed-page"`` on "Load More"
never duplicates it.
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


def _solid_png(color=(200, 50, 10), size=(640, 360)) -> bytes:
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
        alt_text="A solid orange rectangle",
        source_prompt="a private prompt",
        provider_snapshot="Fal",
        model_snapshot="fal-ai/flux-1/schnell",
        request_snapshot="req-1",
    )
    db_session.add(image)
    db_session.commit()
    return post, image


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


class TestFeedCardThumbnails:
    """The four shared feed surfaces all render through post_list/post_card."""

    def _assert_thumb_present(self, html, image):
        thumb_name = Path(image.thumbnail_path).name
        assert 'class="post-card__thumb"' in html
        assert f"/media/images/thumbnail/{thumb_name}" in html
        assert f'width="{image.width}"' in html
        assert f'height="{image.height}"' in html
        assert 'loading="lazy"' in html
        assert "A solid orange rectangle" in html

    def test_front_page_renders_image_card(self, app, client, db_session):
        _seed(db_session)
        _post, image = _make_image_post(app, db_session)

        html = client.get("/").get_data(as_text=True)
        self._assert_thumb_present(html, image)

    def test_subdeaddit_page_renders_image_card(self, app, client, db_session):
        _seed(db_session)
        _post, image = _make_image_post(app, db_session)

        html = client.get("/d/testsub").get_data(as_text=True)
        self._assert_thumb_present(html, image)

    def test_user_profile_renders_image_card(self, app, client, db_session):
        _seed(db_session)
        _post, image = _make_image_post(app, db_session)

        html = client.get("/user/alice").get_data(as_text=True)
        self._assert_thumb_present(html, image)

    def test_search_renders_image_card(self, app, client, db_session):
        _seed(db_session)
        _post, image = _make_image_post(
            app, db_session, title="a unique searchable photo"
        )

        html = client.get("/search?q=searchable").get_data(as_text=True)
        self._assert_thumb_present(html, image)

    def test_text_only_card_has_no_media_block(self, app, client, db_session):
        _seed(db_session)
        _make_text_post(db_session)

        html = client.get("/").get_data(as_text=True)
        assert "post-card__media" not in html
        assert "post-card__thumb" not in html
        # The preview paragraph is untouched.
        assert 'class="post-card__preview"' in html

    def test_image_only_card_has_no_empty_preview_paragraph(
        self, app, client, db_session
    ):
        _seed(db_session)
        _make_image_post(app, db_session, content=None)

        html = client.get("/").get_data(as_text=True)
        assert "post-card__media" in html
        assert 'class="post-card__preview"' not in html


class TestPostDetailImage:
    def test_detail_renders_normalized_original(self, app, client, db_session):
        _seed(db_session)
        post, image = _make_image_post(app, db_session)

        html = client.get(f"/d/testsub/{post.id}").get_data(as_text=True)
        original_name = Path(image.original_path).name
        assert 'class="post-detail__image"' in html
        assert f"/media/images/original/{original_name}" in html
        assert f'width="{image.width}"' in html
        assert f'height="{image.height}"' in html
        assert "A solid orange rectangle" in html
        # The detail page must not also embed the thumbnail variant.
        assert "/media/images/thumbnail/" not in html

    def test_image_only_detail_has_no_empty_body_container(
        self, app, client, db_session
    ):
        _seed(db_session)
        post, _image = _make_image_post(app, db_session, content=None)

        html = client.get(f"/d/testsub/{post.id}").get_data(as_text=True)
        assert 'class="post-body"' not in html
        assert 'class="post-detail__image"' in html

    def test_text_only_detail_body_container_unchanged(self, app, client, db_session):
        _seed(db_session)
        post = _make_text_post(db_session)

        html = client.get(f"/d/testsub/{post.id}").get_data(as_text=True)
        assert 'class="post-body"' in html
        assert 'class="post-detail__image"' not in html

    def test_removed_post_detail_never_leaks_image(self, app, client, db_session):
        _seed(db_session)
        post, image = _make_image_post(app, db_session, removed=True)

        html = client.get(f"/d/testsub/{post.id}").get_data(as_text=True)
        assert "post-detail__image" not in html
        assert Path(image.original_path).name not in html


class TestFeedImageToolbar:
    def test_toolbar_markup_present_and_outside_feed_page(
        self, app, client, db_session
    ):
        _seed(db_session)
        _make_image_post(app, db_session)

        html = client.get("/").get_data(as_text=True)
        toolbar_pos = html.index('class="image-feed-toolbar"')
        feed_page_pos = html.index('class="feed-page"')
        # The toolbar's own div must open (and hence close) before the
        # feed-page fragment starts, i.e. it is a preceding sibling, not a
        # descendant.
        assert toolbar_pos < feed_page_pos
        assert 'data-feed-image-action="expand-all"' in html
        assert 'data-feed-image-action="minimize-all"' in html

    def test_toolbar_present_even_with_no_images(self, app, client, db_session):
        """Visibility is CSS-driven (:has()), so the markup always renders;
        only style.css decides whether it is shown."""
        _seed(db_session)
        _make_text_post(db_session)

        html = client.get("/").get_data(as_text=True)
        assert 'class="image-feed-toolbar"' in html
        assert "hidden" in html

    def test_toolbar_hides_via_css_has_rule_for_dom_visibility(self):
        """No JS in this phase: absence of image cards is communicated to
        the browser purely through the :has() relationship in style.css."""
        css_path = Path(__file__).resolve().parents[1] / "deaddit/static/style.css"
        css = css_path.read_text()
        assert ".feed-wrap:has(.post-card__media) .image-feed-toolbar" in css
