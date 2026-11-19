"""Unit tests for isolated generated-website screenshot attachment."""

from __future__ import annotations

import hashlib
import stat
import subprocess
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
from PIL import Image

from deaddit import create_app
from deaddit import db as _db
from deaddit.models import GeneratedWebsite, Post, PostImage, Setting, Subdeaddit, User
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
    sandbox_wrapper,
    with_csp_meta,
)
from deaddit.websites.serving import (
    CONTENT_SECURITY_POLICY,
    meta_content_security_policy,
)
from deaddit.websites.storage import resolve_website_path, store_website, website_root


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
    Setting.set_value("SETUP_COMPLETED_AT", "2026-01-01T00:00:00Z")
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


def test_resolve_binary_skips_snap_confined_candidates(monkeypatch, tmp_path):
    # Regression (live E2E 2026-08-28 & 2026-08-29): Ubuntu's chromium-browser is a snap
    # wrapper and /snap/bin/chromium is a symlink to /usr/bin/snap that exits cleanly
    # but writes the PNG into its private namespace, so the probe must fall through
    # to a host browser.
    invalidate_binary_cache()
    monkeypatch.delenv("DEADDIT_CHROME_BINARY", raising=False)

    # 1. Direct /snap/ prefix
    def fake_which(candidate):
        if candidate == "chromium":
            return "/snap/chromium/3507/usr/lib/chromium-browser/chromium"
        if candidate == "google-chrome":
            return "/usr/bin/google-chrome"
        return None

    monkeypatch.setattr(screenshot.shutil, "which", fake_which)
    assert resolve_chrome_binary() == "/usr/bin/google-chrome"

    # 2. Symlink to snap binary (e.g. /snap/bin/chromium -> /usr/bin/snap)
    snap_bin = _executable(tmp_path / "snap")
    snap_symlink = tmp_path / "chromium-snap-symlink"
    snap_symlink.symlink_to(snap_bin)

    invalidate_binary_cache()

    def fake_which_snap_symlink(candidate):
        if candidate == "chromium":
            return str(snap_symlink)
        if candidate == "google-chrome":
            return "/usr/bin/google-chrome"
        return None

    monkeypatch.setattr(screenshot.shutil, "which", fake_which_snap_symlink)
    assert resolve_chrome_binary() == "/usr/bin/google-chrome"

    # 3. Wrapper script referencing /snap/ or snap
    wrapper_script = _executable(
        tmp_path / "chromium-wrapper",
        content='#!/bin/sh\nexec /snap/bin/chromium "$@"\n',
    )

    invalidate_binary_cache()

    def fake_which_wrapper(candidate):
        if candidate == "chromium":
            return str(wrapper_script)
        if candidate == "google-chrome":
            return "/usr/bin/google-chrome"
        return None

    monkeypatch.setattr(screenshot.shutil, "which", fake_which_wrapper)
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
            "<!doctype html><html><body>Hi</body></html>",
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
            "<!doctype html><html><body>Hi</body></html>",
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
            # Clicking the screenshot opens the website, like the feed's
            # media link.
            media_marker = '<a class="post-detail__media-link"'
            media_start = detail_body.index(media_marker)
            media_open_end = detail_body.index(">", media_start)
            media_opening = detail_body[media_start:media_open_end]
            assert 'href="/out/www.example.test/page.html"' in media_opening
            assert (
                'aria-label="Open website www.example.test/page.html"' in media_opening
            )

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


# ---------------------------------------------------------------------------
# Render isolation: the screenshot path must give the untrusted page the same
# protections as /out/ serving (CSP, sandbox, no network, no secrets).
# ---------------------------------------------------------------------------

#: Quotes, entities, a literal </script>, non-ASCII, and an attempt to break
#: out of the srcdoc attribute into the trusted wrapper.
_TRICKY_PAGE = (
    "<!doctype html><html><head><title>T &amp; \"q\" 'a'</title>"
    '<link rel=icon href="data:,"><style>p::after{content:"&amp;\\""}</style>'
    '</head><body><p title="a &quot;b&quot; &amp; c">Tom &amp; Jerry\'s '
    '"show" &lt;3 caf\u00e9 \u2603</p>'
    "\"></iframe><script>alert('breakout')</script>"
    "<script>var s = '<b>&amp;</b>'; document.title = \"x\";</script>"
    "</body></html>"
)

_FORBIDDEN_SANDBOX_TOKENS = (
    "allow-same-origin",
    "allow-top-navigation",
    "allow-top-navigation-by-user-activation",
    "allow-top-navigation-to-custom-protocols",
    "allow-popups",
    "allow-popups-to-escape-sandbox",
    "allow-forms",
    "allow-modals",
)


class _TagCollector(HTMLParser):
    """Collect (tag, attrs) pairs; attribute values decoded like a browser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    handle_startendtag = handle_starttag


def _tags(document: str) -> list[tuple[str, dict[str, str | None]]]:
    collector = _TagCollector()
    collector.feed(document)
    collector.close()
    return collector.tags


def _capturing_run(captured: dict):
    """Fake ``subprocess.run`` that records argv/env and the wrapper Chrome got."""

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["env"] = kwargs.get("env")
        url = argv[-1]
        captured["url"] = url
        wrapper_path = Path(unquote(urlparse(url).path))
        captured["wrapper_path"] = wrapper_path
        captured["wrapper"] = wrapper_path.read_text("utf-8")
        target = next(
            Path(arg.split("=", 1)[1])
            for arg in argv
            if arg.startswith("--screenshot=")
        )
        captured["render_dir"] = target.parent
        target.write_bytes(_png())
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    return fake_run


def _render(monkeypatch, page: str = _TRICKY_PAGE) -> dict:
    captured: dict = {}
    monkeypatch.setattr(screenshot.subprocess, "run", _capturing_run(captured))
    png = render_page_png(
        page, binary="fake-chrome", deadline=screenshot.Deadline.after(5)
    )
    assert png == _png()
    return captured


def _framed_document(wrapper: str) -> str:
    iframes = [attrs for tag, attrs in _tags(wrapper) if tag == "iframe"]
    assert len(iframes) == 1
    return iframes[0]["srcdoc"]


def _meta_policy(document: str) -> str:
    metas = [
        attrs
        for tag, attrs in _tags(document)
        if tag == "meta"
        and (attrs.get("http-equiv") or "").lower() == "content-security-policy"
    ]
    assert len(metas) == 1
    return metas[0]["content"]


def _directive_names(policy: str) -> list[str]:
    return [part.split()[0] for part in policy.split(";") if part.strip()]


def test_render_argv_routes_all_traffic_to_dead_proxy(monkeypatch):
    argv = _render(monkeypatch)["argv"]

    assert "--proxy-server=127.0.0.1:9" in argv
    assert "--proxy-bypass-list=<-loopback>" in argv
    assert "--host-resolver-rules=MAP * ~NOTFOUND" in argv
    assert "--disable-background-networking" in argv
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in argv
    assert not any(arg.startswith("--proxy-pac-url") for arg in argv)
    # Same-process frame, so page scripts see the 1280x800 viewport at load.
    assert "--disable-features=IsolateSandboxedIframes" in argv
    # The only positional argument is the wrapper URL.
    assert [arg for arg in argv[1:] if not arg.startswith("--")] == [argv[-1]]
    assert argv[-1].startswith("file://")


def test_render_loads_script_free_wrapper_with_strict_sandbox(monkeypatch):
    captured = _render(monkeypatch)
    wrapper = captured["wrapper"]

    # Chrome is pointed at the generated wrapper in the per-render temp dir.
    assert captured["url"] == captured["wrapper_path"].as_uri()
    assert captured["wrapper_path"].parent == captured["render_dir"]
    assert not captured["wrapper_path"].exists()  # removed with the temp dir

    tags = _tags(wrapper)
    assert [tag for tag, _attrs in tags].count("iframe") == 1
    assert "script" not in [tag for tag, _attrs in tags]
    # Not even the framed page's scripts appear unescaped in the wrapper.
    assert "<script" not in wrapper.lower()
    iframe = next(attrs for tag, attrs in tags if tag == "iframe")
    tokens = iframe["sandbox"].split()
    assert tokens == ["allow-scripts"]
    for forbidden in _FORBIDDEN_SANDBOX_TOKENS:
        assert forbidden not in tokens
    assert set(iframe) == {"sandbox", "style", "srcdoc"}
    style = iframe["style"].replace(" ", "")
    assert "border:0" in style
    assert "width:100vw" in style and "height:100vh" in style
    body = next(attrs for tag, attrs in tags if tag == "body")
    assert "margin:0" in body["style"].replace(" ", "")


def test_render_framed_page_has_csp_meta_first_and_round_trips(monkeypatch):
    framed = _framed_document(_render(monkeypatch)["wrapper"])

    lowered = framed.lower()
    meta_at = lowered.index('<meta http-equiv="content-security-policy"')
    first_active = min(
        lowered.index(marker) for marker in ("<script", "<style", "<link")
    )
    assert meta_at < first_active
    assert framed.startswith("<!doctype html><meta ")

    policy = _meta_policy(framed)
    assert "default-src 'none'" in policy
    assert "sandbox" not in _directive_names(policy)
    assert "frame-ancestors" not in _directive_names(policy)
    assert policy == meta_content_security_policy()

    # Exactly the original page plus the meta tag: quotes, entities, the
    # literal </script> and non-ASCII survive the attribute escaping.
    assert framed == with_csp_meta(_TRICKY_PAGE)
    meta_tag = framed[meta_at : framed.index(">", meta_at) + 1]
    assert framed.replace(meta_tag, "", 1) == _TRICKY_PAGE


def test_render_scrubs_secrets_from_chrome_environment(monkeypatch):
    secrets = {
        "API_TOKEN": "tok-should-not-leak",
        "SECRET_KEY": "flask-should-not-leak",
        "OPENAI_API_KEY": "sk-should-not-leak",
        "ANTHROPIC_API_KEY": "ant-should-not-leak",
        "FAL_KEY": "fal-should-not-leak",
        "AWS_SECRET_ACCESS_KEY": "aws-should-not-leak",
        "HTTPS_PROXY": "http://proxy.invalid:3128",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("FONTCONFIG_FILE", "/etc/fonts/fonts.conf")

    captured = _render(monkeypatch)
    env = captured["env"]

    assert env is not None, "Chrome must not inherit the worker environment"
    for name, value in secrets.items():
        assert name not in env
        assert value not in env.values()
    assert not [
        name
        for name in env
        if any(word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASS"))
    ]
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert env["LANG"] == "C.UTF-8"
    assert env["FONTCONFIG_FILE"] == "/etc/fonts/fonts.conf"
    assert env["HOME"] == str(captured["render_dir"])
    assert set(env) <= {
        "PATH",
        "HOME",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TMPDIR",
        "FONTCONFIG_FILE",
    }


def test_attach_renders_stored_page_through_wrapper_and_leaves_file_unchanged(
    app, db_session, monkeypatch, tmp_path
):
    stored = store_website(_TRICKY_PAGE, website_root(app))
    page_file = resolve_website_path(website_root(app), stored.storage_path)
    before = hashlib.sha256(page_file.read_bytes()).hexdigest()
    assert before == stored.sha256
    post = _post(db_session)
    binary = _executable(tmp_path / "fake-chrome")
    monkeypatch.setenv("DEADDIT_CHROME_BINARY", str(binary))
    monkeypatch.setattr(screenshot, "resolve_chrome_binary", lambda: str(binary))
    captured: dict = {}
    monkeypatch.setattr(screenshot.subprocess, "run", _capturing_run(captured))

    with app.app_context():
        attach_website_screenshot(
            post.id,
            storage_path=stored.storage_path,
            hostname="example.test",
            page_name="home.html",
        )
        assert PostImage.query.filter_by(post_id=post.id).count() == 1

    assert hashlib.sha256(page_file.read_bytes()).hexdigest() == before
    assert page_file.read_text("utf-8") == _TRICKY_PAGE
    # Chrome never sees the stored file, only the wrapper around a copy.
    assert captured["url"] != page_file.resolve().as_uri()
    assert str(page_file) not in " ".join(captured["argv"])
    assert _framed_document(captured["wrapper"]) == with_csp_meta(_TRICKY_PAGE)


def test_meta_csp_drops_header_only_directives_from_served_policy():
    policy = meta_content_security_policy()

    assert policy == meta_content_security_policy(CONTENT_SECURITY_POLICY)
    names = _directive_names(policy)
    assert "sandbox" not in names
    assert "frame-ancestors" not in names
    served = [
        part.strip() for part in CONTENT_SECURITY_POLICY.split(";") if part.strip()
    ]
    expected = [
        part for part in served if part.split()[0] not in {"sandbox", "frame-ancestors"}
    ]
    assert policy.split("; ") == expected
    assert "default-src 'none'" in expected
    assert "script-src 'unsafe-inline'" in expected


def test_meta_csp_strips_reporting_and_tolerates_messy_input():
    policy = meta_content_security_policy(
        " default-src 'none' ;; Report-URI /csp ; report-to grp; "
        "FRAME-ANCESTORS 'none'; img-src data: ; Sandbox allow-scripts ;"
    )
    assert policy == "default-src 'none'; img-src data:"
    assert meta_content_security_policy("") == ""


@pytest.mark.parametrize(
    ("page", "prefix"),
    [
        ("<!doctype html><html><body>x</body></html>", "<!doctype html>"),
        ("<!DOCTYPE HTML>\n<html><body>x</body></html>", "<!DOCTYPE HTML>"),
        (
            "\ufeff  <!-- c <script> --> <!doctype html><body>x</body>",
            "\ufeff  <!-- c <script> --> <!doctype html>",
        ),
        ("<html><head><script>x()</script></head></html>", ""),
        ("<p>no doctype</p><!doctype html>", ""),
    ],
)
def test_with_csp_meta_places_tag_after_doctype_or_at_start(page, prefix):
    result = with_csp_meta(page)

    assert result.startswith(prefix)
    rest = result[len(prefix) :]
    assert rest.startswith('<meta http-equiv="Content-Security-Policy" content="')
    assert rest[rest.index(">") + 1 :] == page[len(prefix) :]
    assert _meta_policy(result) == meta_content_security_policy()


def test_sandbox_wrapper_cannot_be_escaped_by_page_markup():
    hostile = (
        '<!doctype html>"></iframe><script>top.location="file:///etc/passwd"'
        "</script><iframe srcdoc='x' sandbox='allow-same-origin'>"
    )
    wrapper = sandbox_wrapper(hostile)

    tags = _tags(wrapper)
    assert [tag for tag, _attrs in tags] == ["html", "head", "meta", "body", "iframe"]
    assert _framed_document(wrapper) == with_csp_meta(hostile)
