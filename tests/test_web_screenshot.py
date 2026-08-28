"""Unit tests for isolated generated-website screenshot attachment."""

from __future__ import annotations

import stat
import subprocess
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from deaddit import create_app
from deaddit import db as _db
from deaddit.models import GeneratedWebsite, Post, PostImage, Subdeaddit, User
from deaddit.services.content import (
    PendingGeneratedWebsite,
    PendingPostImage,
    create_image_post,
    create_post,
    create_website_post,
)
from deaddit.websites import screenshot
from deaddit.websites.screenshot import (
    MAX_SCREENSHOT_BYTES,
    ScreenshotRenderError,
    ScreenshotTooLargeError,
    attach_website_screenshot,
    invalidate_binary_cache,
    render_page_png,
    resolve_chrome_binary,
)
from deaddit.websites.storage import store_website, website_root


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "TESTING": True,
            "GENERATED_IMAGES_ROOT": str(tmp_path / "images"),
            "GENERATED_WEBSITES_ROOT": str(tmp_path / "websites"),
        }
    )
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


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


def _png(size=(400, 300), color=(20, 100, 180)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color=color).save(output, format="PNG")
    return output.getvalue()


def _executable(path: Path, content: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _website(app):
    return store_website(
        "<!doctype html><html><body>Hi</body></html>", website_root(app)
    )


def _pending_website(app, **overrides) -> PendingGeneratedWebsite:
    stored = store_website(
        "<!doctype html><html><body>Generated page</body></html>",
        website_root(app),
    )
    fields = {
        "storage_path": stored.storage_path,
        "byte_size": stored.byte_size,
        "sha256": stored.sha256,
        "public_path": "www.example.test/page.html",
        "hostname": "www.example.test",
        "page_name": "page.html",
        "source_description": "A generated website for integration testing",
        "creator_username_snapshot": "alice",
        "api_url_snapshot": "http://example.test/v1",
        "model_snapshot": "test-model",
    }
    fields.update(overrides)
    return PendingGeneratedWebsite(**fields)


def _pending_image() -> PendingPostImage:
    return PendingPostImage(
        original_path="originals/one.png",
        thumbnail_path="thumbnails/one.png",
        mime_type="image/png",
        byte_size=1024,
        width=512,
        height=512,
        alt_text="A real provider image",
        source_prompt="A private image prompt",
        provider_snapshot="real-provider",
        model_snapshot="real-model",
    )


def _post(db_session):
    post = Post(
        title="A generated website",
        content=None,
        subdeaddit_name="testsub",
        user="alice",
    )
    db_session.add(post)
    db_session.commit()
    return post


def test_resolve_binary_probe_order_and_configured_path(monkeypatch, tmp_path):
    invalidate_binary_cache()
    monkeypatch.delenv("DEADDIT_CHROME_BINARY", raising=False)
    calls = []

    def fake_which(candidate):
        calls.append(candidate)
        return "/opt/" + candidate if candidate == "google-chrome" else None

    monkeypatch.setattr(screenshot.shutil, "which", fake_which)
    assert resolve_chrome_binary() == "/opt/google-chrome"
    assert calls == ["chromium", "google-chrome"]

    configured = _executable(tmp_path / "my-chrome")
    invalidate_binary_cache()
    monkeypatch.setenv("DEADDIT_CHROME_BINARY", str(configured))
    monkeypatch.setattr(screenshot.shutil, "which", lambda value: value)
    assert resolve_chrome_binary() == str(configured)


def test_resolve_binary_skips_snap_confined_candidates(monkeypatch):
    # Regression (live E2E 2026-08-28): Ubuntu's chromium-browser is a snap
    # wrapper that exits cleanly but writes the PNG into its private
    # namespace, so the probe must fall through to a host browser.
    invalidate_binary_cache()
    monkeypatch.delenv("DEADDIT_CHROME_BINARY", raising=False)

    def fake_which(candidate):
        if candidate == "chromium":
            return "/snap/chromium/3507/usr/lib/chromium-browser/chromium"
        if candidate == "google-chrome":
            return "/usr/bin/google-chrome"
        return None

    monkeypatch.setattr(screenshot.shutil, "which", fake_which)
    assert resolve_chrome_binary() == "/usr/bin/google-chrome"


def test_render_clean_exit_without_output_is_distinct_error(monkeypatch):
    # A returncode-0 run whose PNG never landed at the target path is a
    # confinement/profile failure, not a Chrome crash: the message must say
    # so instead of the misleading "failed (returncode 0)".
    def fake_run(_argv, **_kwargs):
        return subprocess.CompletedProcess(
            _argv, 0, stdout=b"", stderr=b"AppArmor policy prevents this sender"
        )

    monkeypatch.setattr(screenshot.subprocess, "run", fake_run)
    with pytest.raises(ScreenshotRenderError) as excinfo:
        render_page_png(
            "file:///tmp/page.html",
            binary="chromium-browser",
            deadline=screenshot.Deadline.after(5),
        )
    message = str(excinfo.value)
    assert "exited cleanly but wrote no screenshot" in message
    assert "failed (returncode 0)" not in message
    assert "AppArmor" in message


def test_resolve_binary_warns_once_and_invalidation_rearms(monkeypatch, caplog):
    invalidate_binary_cache()
    monkeypatch.delenv("DEADDIT_CHROME_BINARY", raising=False)
    monkeypatch.setattr(screenshot.shutil, "which", lambda _value: None)
    caplog.set_level("WARNING", logger=screenshot.logger.name)

    assert resolve_chrome_binary() is None
    assert resolve_chrome_binary() is None
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "DEADDIT_CHROME_BINARY" in warnings[0].message
    assert "website posts will publish without screenshots" in warnings[0].message

    invalidate_binary_cache()
    assert resolve_chrome_binary() is None
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 2


def test_attach_happy_path(app, db_session, monkeypatch, tmp_path):
    website = _website(app)
    post = _post(db_session)
    binary = _executable(tmp_path / "fake-chrome")
    monkeypatch.setenv("DEADDIT_CHROME_BINARY", str(binary))
    monkeypatch.setattr(screenshot, "resolve_chrome_binary", lambda: str(binary))
    monkeypatch.setattr(screenshot, "render_page_png", lambda *_args, **_kwargs: _png())

    with app.app_context():
        assert (
            attach_website_screenshot(
                post.id,
                storage_path=website.storage_path,
                hostname="example.test",
                page_name="home.html",
            )
            is None
        )
        image = PostImage.query.one()
        assert image.provider_id is None
        assert image.provider_snapshot == "screenshot"
        assert image.model_snapshot == "fake-chrome"
        assert image.alt_text == "Screenshot of example.test/home.html"
        assert image.mime_type == "image/png"
        assert image.width == 400 and image.height == 300
        assert image.byte_size > 0
        root = Path(app.config["GENERATED_IMAGES_ROOT"])
        assert (root / image.original_path).is_file()
        assert (root / image.thumbnail_path).is_file()
        with Image.open(root / image.thumbnail_path) as thumbnail:
            assert max(thumbnail.size) <= 800
        client = app.test_client()
        assert (
            client.get(
                f"/media/images/thumbnail/{Path(image.thumbnail_path).name}"
            ).status_code
            == 200
        )
        assert (
            client.get(
                f"/media/images/original/{Path(image.original_path).name}"
            ).status_code
            == 200
        )


def test_attach_failure_isolated_and_session_usable(
    app, db_session, monkeypatch, caplog
):
    website = _website(app)
    post = _post(db_session)
    monkeypatch.setenv("DEADDIT_CHROME_BINARY", "/fake/chrome")
    monkeypatch.setattr(screenshot, "resolve_chrome_binary", lambda: "/fake/chrome")
    monkeypatch.setattr(
        screenshot,
        "render_page_png",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    caplog.set_level("WARNING", logger=screenshot.logger.name)

    with app.app_context():
        attach_website_screenshot(
            post.id,
            storage_path=website.storage_path,
            hostname="example.test",
            page_name="home.html",
        )
        assert PostImage.query.count() == 0
        root = Path(app.config["GENERATED_IMAGES_ROOT"])
        assert not [path for path in root.rglob("*") if path.is_file()]
        assert Post.query.count() == 1
        assert (
            len([record for record in caplog.records if record.levelname == "WARNING"])
            == 1
        )


def test_attach_garbage_isolated(app, db_session, monkeypatch):
    website = _website(app)
    post = _post(db_session)
    monkeypatch.setenv("DEADDIT_CHROME_BINARY", "/fake/chrome")
    monkeypatch.setattr(screenshot, "resolve_chrome_binary", lambda: "/fake/chrome")
    monkeypatch.setattr(
        screenshot, "render_page_png", lambda *_args, **_kwargs: b"not an image"
    )

    with app.app_context():
        attach_website_screenshot(
            post.id,
            storage_path=website.storage_path,
            hostname="example.test",
            page_name="home.html",
        )
        assert PostImage.query.count() == 0
        root = Path(app.config["GENERATED_IMAGES_ROOT"])
        assert not [path for path in root.rglob("*") if path.is_file()]


def test_render_rejects_oversize_before_read(monkeypatch, tmp_path):
    output = tmp_path / "oversize.png"

    def fake_run(argv, **_kwargs):
        target = next(
            Path(arg.split("=", 1)[1])
            for arg in argv
            if arg.startswith("--screenshot=")
        )
        target.write_bytes(b"x" * 2048)
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(screenshot.subprocess, "run", fake_run)
    monkeypatch.setattr(screenshot, "MAX_SCREENSHOT_BYTES", 1024)
    with pytest.raises(ScreenshotTooLargeError):
        render_page_png(
            "file:///tmp/page.html",
            binary="fake-chrome",
            deadline=screenshot.Deadline.after(5),
        )
    assert not output.exists()


def test_attach_idempotency_skips_renderer(app, db_session, monkeypatch, tmp_path):
    website = _website(app)
    post = _post(db_session)
    monkeypatch.setenv("DEADDIT_CHROME_BINARY", "/fake/chrome")
    binary = _executable(tmp_path / "fake-chrome")
    stored = screenshot.store_variants(_png(), screenshot.media_root(app))
    db_session.add(
        PostImage(
            post_id=post.id,
            original_path=stored.original_path,
            thumbnail_path=stored.thumbnail_path,
            mime_type=stored.mime_type,
            byte_size=stored.original_size,
            width=stored.width,
            height=stored.height,
            alt_text="existing",
            source_prompt="existing",
            provider_snapshot="existing",
            model_snapshot="existing",
        )
    )
    db_session.commit()
    called = []
    monkeypatch.setattr(screenshot, "resolve_chrome_binary", lambda: str(binary))
    monkeypatch.setattr(
        screenshot, "render_page_png", lambda *args, **kwargs: called.append(True)
    )

    with app.app_context():
        attach_website_screenshot(
            post.id, storage_path=website.storage_path, hostname="x", page_name="x.html"
        )
    assert called == []


def test_testing_gate_requires_explicit_env(app, db_session, monkeypatch, tmp_path):
    website = _website(app)
    post = _post(db_session)
    monkeypatch.delenv("DEADDIT_CHROME_BINARY", raising=False)
    called = []
    monkeypatch.setattr(screenshot, "resolve_chrome_binary", lambda: "/fake-chrome")
    monkeypatch.setattr(
        screenshot, "render_page_png", lambda *args, **kwargs: called.append(True)
    )
    with app.app_context():
        attach_website_screenshot(
            post.id, storage_path=website.storage_path, hostname="x", page_name="x.html"
        )
    assert called == []

    binary = _executable(tmp_path / "opted-in-chrome")
    monkeypatch.setenv("DEADDIT_CHROME_BINARY", str(binary))
    monkeypatch.setattr(
        screenshot,
        "render_page_png",
        lambda *args, **kwargs: called.append(True) or _png(),
    )
    with app.app_context():
        attach_website_screenshot(
            post.id, storage_path=website.storage_path, hostname="x", page_name="x.html"
        )
    assert called == [True]


def test_real_subprocess_plumbing(app, db_session, monkeypatch, tmp_path):
    website = _website(app)
    post = _post(db_session)
    source = tmp_path / "source.png"
    source.write_bytes(_png())
    script = _executable(
        tmp_path / "fake-chrome",
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in --screenshot=*) cp "'
        + str(source)
        + '" "${arg#--screenshot=}";; esac\n'
        "done\n",
    )
    monkeypatch.setenv("DEADDIT_CHROME_BINARY", str(script))
    invalidate_binary_cache()
    monkeypatch.setattr(screenshot.shutil, "which", lambda value: value)

    with app.app_context():
        attach_website_screenshot(
            post.id,
            storage_path=website.storage_path,
            hostname="example.test",
            page_name="home.html",
        )
        image = PostImage.query.one()
        assert (
            Path(app.config["GENERATED_IMAGES_ROOT"]) / image.original_path
        ).is_file()
        assert (
            Path(app.config["GENERATED_IMAGES_ROOT"]) / image.thumbnail_path
        ).is_file()


class TestWebsitePostScreenshotIntegration:
    def test_create_website_post_happy_path_renders_on_real_surfaces(
        self, app, db_session, monkeypatch, tmp_path
    ):
        binary = _executable(tmp_path / "fake-chrome")
        monkeypatch.setenv("DEADDIT_CHROME_BINARY", str(binary))
        monkeypatch.setattr(screenshot, "resolve_chrome_binary", lambda: str(binary))
        monkeypatch.setattr(
            screenshot,
            "render_page_png",
            lambda _url, *, binary, deadline: _png(size=(640, 480)),
        )

        website = _pending_website(app)
        with app.app_context():
            post = create_website_post(
                title="Screenshot integration website",
                content=None,
                user="alice",
                subdeaddit="testsub",
                website=website,
            )
            image = post.image
            persisted_post = Post.query.filter_by(id=post.id).one()
            assert persisted_post.title == "Screenshot integration website"
            assert persisted_post.content is None
            assert image is not None
            assert image.provider_snapshot == "screenshot"
            assert image.alt_text == "Screenshot of www.example.test/page.html"
            assert GeneratedWebsite.query.filter_by(post_id=post.id).one().hostname == (
                "www.example.test"
            )

            root = Path(app.config["GENERATED_IMAGES_ROOT"])
            original_name = Path(image.original_path).name
            thumbnail_name = Path(image.thumbnail_path).name
            assert (root / image.original_path).is_file()
            assert (root / image.thumbnail_path).is_file()

            client = app.test_client()
            front = client.get("/")
            front_body = front.get_data(as_text=True)
            assert front.status_code == 200
            assert f"/media/images/thumbnail/{thumbnail_name}" in front_body
            media_marker = '<a class="post-card__media-link"'
            media_start = front_body.index(media_marker)
            media_open_end = front_body.index(">", media_start)
            media_opening = front_body[media_start:media_open_end]
            assert 'href="/out/www.example.test/page.html"' in media_opening
            assert (
                'aria-label="Open website www.example.test/page.html"' in media_opening
            )
            media_end = front_body.index("</a>", media_open_end)
            media_html = front_body[media_start:media_end]
            assert 'class="post-card__thumb"' in media_html
            assert f'src="/media/images/thumbnail/{thumbnail_name}"' in media_html
            assert (
                f'data-original-src="/media/images/original/{original_name}"'
                in media_html
            )
            assert 'class="post-card__thumb"' in front_body
            assert f'width="{image.width}"' in front_body
            assert f'height="{image.height}"' in front_body
            assert 'class="post-card__website"' in front_body
            assert "www.example.test/page.html" in front_body

            detail = client.get(f"/d/testsub/{post.id}")
            detail_body = detail.get_data(as_text=True)
            assert detail.status_code == 200
            assert f"/media/images/original/{original_name}" in detail_body
            assert 'class="post-detail__image"' in detail_body

            for variant, filename in (
                ("thumbnail", thumbnail_name),
                ("original", original_name),
            ):
                media = client.get(f"/media/images/{variant}/{filename}")
                assert media.status_code == 200
                assert media.mimetype == "image/png"

    def test_create_website_post_renderer_failure_leaves_website_only(
        self, app, db_session, monkeypatch, caplog
    ):
        monkeypatch.setenv("DEADDIT_CHROME_BINARY", "/fake/chrome")
        monkeypatch.setattr(screenshot, "resolve_chrome_binary", lambda: "/fake/chrome")

        def fail_renderer(_url, *, binary, deadline):
            raise RuntimeError("renderer failed")

        monkeypatch.setattr(screenshot, "render_page_png", fail_renderer)
        caplog.set_level("WARNING", logger=screenshot.logger.name)
        website = _pending_website(app, public_path="www.example.test/failure.html")

        with app.app_context():
            post = create_website_post(
                title="Screenshot failure website",
                content=None,
                user="alice",
                subdeaddit="testsub",
                website=website,
            )
            assert post.image is None
            assert GeneratedWebsite.query.filter_by(post_id=post.id).count() == 1
            root = Path(app.config["GENERATED_IMAGES_ROOT"])
            assert not [path for path in root.rglob("*") if path.is_file()]
            assert Post.query.filter_by(id=post.id).count() == 1
            assert Post.query.filter_by(title="Screenshot failure website").count() == 1

        warnings = [
            record
            for record in caplog.records
            if record.levelname == "WARNING"
            and "website screenshot attachment failed" in record.message
        ]
        assert len(warnings) == 1

    def test_create_website_post_skips_capture_without_browser_opt_in(
        self, app, db_session, monkeypatch, caplog
    ):
        monkeypatch.delenv("DEADDIT_CHROME_BINARY", raising=False)
        resolved = []
        rendered = []
        monkeypatch.setattr(
            screenshot,
            "resolve_chrome_binary",
            lambda: resolved.append(True) or "/fake/chrome",
        )
        monkeypatch.setattr(
            screenshot,
            "render_page_png",
            lambda *args, **kwargs: rendered.append(True),
        )
        caplog.set_level("WARNING", logger=screenshot.logger.name)
        website = _pending_website(app, public_path="www.example.test/no-browser.html")

        with app.app_context():
            post = create_website_post(
                title="Screenshot no browser website",
                content=None,
                user="alice",
                subdeaddit="testsub",
                website=website,
            )
            assert post.image is None
            assert GeneratedWebsite.query.filter_by(post_id=post.id).count() == 1

        assert resolved == []
        assert rendered == []
        assert not any(
            record.levelname == "WARNING"
            and "website screenshot attachment failed" in record.message
            for record in caplog.records
        )

    def test_other_post_creators_do_not_attach_screenshots(
        self, app, db_session, monkeypatch
    ):
        monkeypatch.setenv("DEADDIT_CHROME_BINARY", "/fake/chrome")
        text_post = create_post(
            title="Text regression guard",
            content="Text content",
            user="alice",
            subdeaddit="testsub",
        )
        assert Post.query.get(text_post.id).image is None

        image_post = create_image_post(
            title="Image regression guard",
            content=None,
            user="alice",
            subdeaddit="testsub",
            image=_pending_image(),
        )
        attached = PostImage.query.filter_by(post_id=image_post.id).one()
        assert PostImage.query.filter_by(post_id=image_post.id).count() == 1
        assert attached.provider_snapshot == "real-provider"
        assert attached.model_snapshot == "real-model"


def test_screenshot_cap_matches_image_download_cap():
    assert MAX_SCREENSHOT_BYTES == 26_214_400
