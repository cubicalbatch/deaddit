"""Isolated headless-browser screenshot attachment for generated websites.

The attachment entry point runs strictly after a website post has committed.
Isolation contract: attachment must NEVER raise; any failure is rolled back on
the shared session, logged once as a warning, and leaves the post website-only.
This keeps optional preview capture from changing the durable website-post path.
"""

from __future__ import annotations

import functools
import html
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from flask import current_app
from sqlalchemy.exc import SQLAlchemyError

from deaddit.extensions import db
from deaddit.images.storage import delete_variants, media_root, store_variants
from deaddit.images.types import Deadline
from deaddit.models import PostImage
from deaddit.websites.serving import meta_content_security_policy
from deaddit.websites.storage import resolve_website_path, website_root

logger = logging.getLogger(__name__)

VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 800
SCREENSHOT_TIMEOUT_SECONDS = 30.0
MAX_SCREENSHOT_BYTES = 26_214_400
VIRTUAL_TIME_BUDGET_MS = 2000
PROVIDER_SNAPSHOT = "screenshot"

#: Proxy every request is forced through. Nothing should listen on the
#: loopback discard port, so connections are refused fast and fail closed.
DEAD_PROXY = "127.0.0.1:9"

#: The only variables Chrome inherits from the worker; the worker's
#: environment holds API_TOKEN, SECRET_KEY and provider keys. TMPDIR is kept
#: as-is: nesting it deeper can push Chrome's singleton socket path past the
#: 108-byte Unix socket limit, which aborts Chrome.
_CHROME_ENV_PASSTHROUGH = (
    "PATH",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
)
#: Font configuration overrides (e.g. FONTCONFIG_FILE on Nix-style systems).
_CHROME_ENV_PREFIXES = ("FONTCONFIG_",)

#: A leading doctype, optionally behind a BOM, whitespace or comments.
_LEADING_DOCTYPE_RE = re.compile(
    r"\A\ufeff?(?:\s|<!--.*?-->)*<!doctype\b[^>]*>", re.IGNORECASE | re.DOTALL
)


class ScreenshotError(Exception):
    """Base class for failures while rendering a website screenshot."""


class ScreenshotRenderError(ScreenshotError):
    """Chrome failed to render a screenshot."""


class ScreenshotTimeoutError(ScreenshotError):
    """Chrome did not render a screenshot before the deadline."""


class ScreenshotTooLargeError(ScreenshotError):
    """Chrome produced a screenshot larger than the byte cap."""


_warned_no_browser = False


def _warn_no_browser() -> None:
    global _warned_no_browser
    if _warned_no_browser:
        return
    _warned_no_browser = True
    logger.warning(
        "no usable browser found (DEADDIT_CHROME_BINARY); website posts will "
        "publish without screenshots"
    )


def _is_snap_confined(binary_path: str) -> bool:
    """True for snap-packaged browsers, which cannot write host paths.

    Snap Chromium runs under AppArmor confinement with a private
    filesystem namespace: it exits cleanly and even reports the written
    byte count, but the PNG never appears at the requested host path.
    """

    try:
        path_str = str(binary_path)
        if path_str.startswith(("/snap/", "/var/lib/snapd/")):
            return True
        real = os.path.realpath(path_str)
        if real.startswith(("/snap/", "/var/lib/snapd/")):
            return True
        if Path(real).name == "snap" or real in {"/usr/bin/snap", "/bin/snap"}:
            return True
        path = Path(path_str)
        if path.is_file() and not path.is_symlink():
            try:
                with path.open("rb") as f:
                    head = f.read(2048)
                    if head.startswith(b"#!") and (
                        b"/snap/" in head or b"snap " in head or b"snap\n" in head
                    ):
                        return True
            except OSError:
                pass
        return False
    except OSError:
        return False


@functools.lru_cache(maxsize=1)
def _resolve_uncached() -> str | None:
    configured = os.environ.get("DEADDIT_CHROME_BINARY", "").strip()
    if configured:
        resolved = shutil.which(configured)
    else:
        resolved = None
        for candidate in (
            # Ubuntu's chromium-browser is usually a shell wrapper that
            # execs the snap build (not a symlink, so the /snap realpath
            # skip cannot see through it); probe it only as a last resort.
            "chromium",
            "google-chrome",
            "google-chrome-stable",
            "chromium-browser",
            "headless-shell",
        ):
            found = shutil.which(candidate)
            if found and not _is_snap_confined(found):
                resolved = found
                break
    if not resolved:
        _warn_no_browser()
    return resolved


def resolve_chrome_binary() -> str | None:
    """Return the configured or first discoverable headless Chrome binary."""

    return _resolve_uncached()


def invalidate_binary_cache() -> None:
    """Clear binary discovery and permit a subsequent no-browser warning."""

    global _warned_no_browser
    _resolve_uncached.cache_clear()
    _warned_no_browser = False


def with_csp_meta(page_html: str) -> str:
    """Return a copy of ``page_html`` carrying the served CSP as a ``<meta>``.

    The tag goes straight after the doctype (so the page keeps standards
    mode), or at the very start without one. Either way the parser puts it
    first in ``<head>``, ahead of every script, style and link it governs.
    """
    policy = html.escape(meta_content_security_policy(), quote=True)
    meta = f'<meta http-equiv="Content-Security-Policy" content="{policy}">'
    match = _LEADING_DOCTYPE_RE.match(page_html)
    split = match.end() if match else 0
    return page_html[:split] + meta + page_html[split:]


def sandbox_wrapper(page_html: str) -> str:
    """Return the trusted, script-free document Chrome is pointed at.

    The page renders in a viewport-sized ``srcdoc`` frame sandboxed with
    ``allow-scripts`` alone, mirroring the served ``sandbox allow-scripts``.
    The frame gets an opaque origin, so it cannot load or navigate to
    ``file:`` URLs, navigate the top frame, open popups or submit forms.
    Attribute escaping keeps the page inert inside the wrapper; the HTML
    parser decodes it back to the exact original text.
    """
    framed = html.escape(with_csp_meta(page_html), quote=True)
    return (
        '<!doctype html><html><head><meta charset="utf-8"></head>'
        '<body style="margin:0;overflow:hidden">'
        '<iframe sandbox="allow-scripts" '
        'style="display:block;border:0;margin:0;width:100vw;height:100vh" '
        f'srcdoc="{framed}"></iframe></body></html>'
    )


def _chrome_environment(home: Path) -> dict[str, str]:
    """Return the allowlisted environment Chrome runs with.

    Everything else is dropped: the worker's API keys and tokens, proxy
    variables, and session-bus/display sockets. ``HOME`` points into the
    per-render directory, so nothing reads or writes the real home.
    """
    env = {
        name: value
        for name, value in os.environ.items()
        if name in _CHROME_ENV_PASSTHROUGH or name.startswith(_CHROME_ENV_PREFIXES)
    }
    env["HOME"] = str(home)
    return env


def render_page_png(page_html: str, *, binary: str, deadline: Deadline) -> bytes:
    """Render generated HTML at a fixed viewport in an isolated Chrome.

    ``page_html`` is untrusted (LLM-written, prompt-injectable), so it gets
    the served page's isolation: the CSP as a ``<meta>``, a sandboxed frame
    (:func:`sandbox_wrapper`), all network traffic sent to a dead proxy, and
    an allowlisted environment. ``--no-sandbox`` is still required when
    containers run Chromium as root, where the OS sandbox cannot work.
    """

    if deadline.expired():
        raise ScreenshotTimeoutError("screenshot deadline expired before rendering")

    with tempfile.TemporaryDirectory() as temporary:
        temporary_root = Path(temporary)
        profile = temporary_root / "chrome-profile"
        profile.mkdir()
        out_png = temporary_root / "screenshot.png"
        wrapper = temporary_root / "wrapper.html"
        wrapper.write_text(sandbox_wrapper(page_html), encoding="utf-8")
        # CLI screenshots are viewport-bounded by design. Full-height capture
        # needs CDP; fixed 1280x800 keeps output deterministic and bounded.
        argv = [
            binary,
            "--headless=new",
            f"--screenshot={out_png}",
            f"--window-size={VIEWPORT_WIDTH},{VIEWPORT_HEIGHT}",
            "--hide-scrollbars",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-crash-reporter",
            "--disable-extensions",
            # Send every request to a proxy nothing listens on. Besides
            # subresources, this stops what CSP cannot: the frame navigating
            # itself to http://169.254.169.254/ or an internal service and
            # the response landing in the screenshot.
            f"--proxy-server={DEAD_PROXY}",
            # Chrome sends localhost/127.0.0.1/[::1] DIRECT even with a proxy;
            # "<-loopback>" removes that implicit bypass.
            "--proxy-bypass-list=<-loopback>",
            # Fail every hostname lookup: DNS prefetch hints are not covered
            # by CSP and resolve outside the proxy (a DNS beacon).
            "--host-resolver-rules=MAP * ~NOTFOUND",
            # WebRTC sends STUN/TURN over UDP straight past the proxy and is
            # not governed by CSP; allow only proxied (so dead) connections.
            "--webrtc-ip-handling-policy=disable_non_proxied_udp",
            # No component updates, Safe Browsing or other Chrome traffic.
            "--disable-background-networking",
            # Keep the sandboxed frame in the wrapper's renderer. In its own
            # process it learns its size only after the wrapper's layout, so
            # page scripts reading innerWidth/innerHeight at load see 0x0 and
            # size canvases to nothing. The opaque origin, not the process
            # split, is what keeps it off file: URLs (and with --no-sandbox a
            # renderer exploit escapes either way).
            "--disable-features=IsolateSandboxedIframes",
            f"--virtual-time-budget={VIRTUAL_TIME_BUDGET_MS}",
            f"--user-data-dir={profile}",
            wrapper.as_uri(),
        ]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                env=_chrome_environment(temporary_root),
                timeout=max(deadline.remaining(), 1.0),
            )
        except subprocess.TimeoutExpired as exc:
            raise ScreenshotTimeoutError(
                f"Chrome {Path(binary).name} exceeded screenshot deadline"
            ) from exc

        stderr = completed.stderr or b""
        if isinstance(stderr, bytes):
            stderr_text = stderr.decode(errors="replace")
        else:
            stderr_text = str(stderr)
        diagnostics = stderr_text[-500:]
        try:
            output_size = os.path.getsize(out_png)
        except OSError:
            output_size = 0
        if completed.returncode != 0:
            raise ScreenshotRenderError(
                f"Chrome {Path(binary).name} failed (returncode "
                f"{completed.returncode}); stderr: {diagnostics}"
            )
        if output_size == 0:
            raise ScreenshotRenderError(
                f"Chrome {Path(binary).name} exited cleanly but wrote no "
                f"screenshot to {out_png} (snap confinement or a broken "
                f"profile); stderr: {diagnostics}"
            )
        if output_size > MAX_SCREENSHOT_BYTES:
            raise ScreenshotTooLargeError(
                f"Chrome {Path(binary).name} produced {output_size} bytes; "
                f"maximum is {MAX_SCREENSHOT_BYTES}"
            )
        try:
            return out_png.read_bytes()
        except OSError as exc:
            raise ScreenshotRenderError(
                f"Chrome {Path(binary).name} output could not be read "
                f"(returncode {completed.returncode}); stderr: {diagnostics}"
            ) from exc


def attach_website_screenshot(
    post_id: int,
    *,
    storage_path: str,
    hostname: str,
    page_name: str,
) -> None:
    """Best-effort attach a rendered website preview to a committed post."""

    try:
        if current_app.config.get("TESTING") and not os.environ.get(
            "DEADDIT_CHROME_BINARY"
        ):
            # Keep deterministic tests free of Chrome subprocesses when Chrome
            # happens to be installed on PATH; live tests opt in via the env.
            logger.debug("skipping website screenshot in testing mode")
            return

        if db.session.get(PostImage, post_id) is not None:
            return

        binary = resolve_chrome_binary()
        if binary is None:
            return

        page_file = resolve_website_path(website_root(current_app), storage_path)
        png = render_page_png(
            page_file.read_text("utf-8"),
            binary=binary,
            deadline=Deadline.after(SCREENSHOT_TIMEOUT_SECONDS),
        )
        stored = store_variants(png, media_root(current_app))
        db.session.add(
            PostImage(
                post_id=post_id,
                original_path=stored.original_path,
                thumbnail_path=stored.thumbnail_path,
                mime_type=stored.mime_type,
                byte_size=stored.original_size,
                width=stored.width,
                height=stored.height,
                alt_text=f"Screenshot of {hostname}/{page_name}",
                source_prompt=(
                    "Headless-browser screenshot preview of the generated website "
                    f"{hostname}/{page_name}."
                ),
                provider_id=None,
                provider_snapshot=PROVIDER_SNAPSHOT,
                model_snapshot=Path(binary).name,
                request_snapshot=None,
            )
        )
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            try:
                delete_variants(
                    media_root(current_app),
                    stored.original_path,
                    stored.thumbnail_path,
                )
            except Exception:  # noqa: BLE001 - cleanup must not mask commit failure
                logger.warning(
                    "website screenshot cleanup failed for post %s",
                    post_id,
                    exc_info=True,
                )
            raise

        from deaddit.extensions import cache as flask_cache

        flask_cache.clear()
    except Exception:  # noqa: BLE001 - screenshot isolation must never raise
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001 - preserve the non-raising boundary
            pass
        logger.warning(
            "website screenshot attachment failed for post %s",
            post_id,
            exc_info=True,
        )
        return None
