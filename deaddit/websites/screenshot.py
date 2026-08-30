"""Isolated headless-browser screenshot attachment for generated websites.

The attachment entry point runs strictly after a website post has committed.
Isolation contract: attachment must NEVER raise; any failure is rolled back on
the shared session, logged once as a warning, and leaves the post website-only.
This keeps optional preview capture from changing the durable website-post path.
"""

from __future__ import annotations

import functools
import logging
import os
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
from deaddit.websites.storage import resolve_website_path, website_root

logger = logging.getLogger(__name__)

VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 800
SCREENSHOT_TIMEOUT_SECONDS = 30.0
MAX_SCREENSHOT_BYTES = 26_214_400
VIRTUAL_TIME_BUDGET_MS = 2000
PROVIDER_SNAPSHOT = "screenshot"


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


def render_page_png(url: str, *, binary: str, deadline: Deadline) -> bytes:
    """Render a fixed viewport using an isolated Chrome profile.

    ``--no-sandbox`` is required when containers run Chromium as root, where
    the sandbox cannot work.  The input is only our self-generated ``file://``
    HTML, so the blast radius is bounded by the timeout and size cap.
    """

    if deadline.expired():
        raise ScreenshotTimeoutError("screenshot deadline expired before rendering")

    with tempfile.TemporaryDirectory() as temporary:
        temporary_root = Path(temporary)
        profile = temporary_root / "chrome-profile"
        profile.mkdir()
        out_png = temporary_root / "screenshot.png"
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
            f"--virtual-time-budget={VIRTUAL_TIME_BUDGET_MS}",
            f"--user-data-dir={profile}",
            url,
        ]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
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
        url = page_file.resolve().as_uri()
        png = render_page_png(
            url,
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
