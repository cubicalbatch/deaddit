"""Secure local storage for generated images.

Downloads are restricted to HTTPS and globally routable destinations on every
redirect hop.  Stored paths are normalized relative to the configured media
root, while Pillow decode/re-encode and atomic moves prevent untrusted bytes,
metadata, and path traversal from reaching the serving layer.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from PIL import Image


class MediaStorageError(Exception):
    """Base class for local media storage failures."""


class UnsafeImageURLError(MediaStorageError):
    """An image URL is not safe to fetch."""


class UnsupportedImageMIMEError(MediaStorageError):
    """The downloaded image has an unsupported declared MIME type."""


class ImageTooLargeError(MediaStorageError):
    """The downloaded image exceeds its configured byte limit."""


class MalformedImageError(MediaStorageError):
    """The image bytes cannot be decoded or fail content validation."""


class MediaPathTraversalError(MediaStorageError):
    """A media path escapes, or attempts to escape, the media root."""


@dataclass
class DownloadedImage:
    """A validated image response."""

    url: str
    mime_type: str
    data: bytes


@dataclass
class StoredImage:
    """Metadata and root-relative paths for a stored image and thumbnail."""

    original_path: str
    thumbnail_path: str
    mime_type: str
    width: int
    height: int
    original_size: int
    thumbnail_size: int


@dataclass
class ReconcileReport:
    """Results of removing unreferenced media files."""

    removed_files: list[str]
    incomplete_rows: list[dict[str, Any]]


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MIME_FORMATS = {
    "image/jpeg": ("JPEG", ".jpg"),
    "image/png": ("PNG", ".png"),
    "image/webp": ("WEBP", ".webp"),
}


def media_root(app: Any) -> Path:
    """Return the configured generated-image root as a :class:`Path`."""

    return Path(app.config["GENERATED_IMAGES_ROOT"])


def ensure_media_tree(root: Path) -> None:
    """Create the media directories used for atomic storage."""

    root = Path(root)
    for directory in (root / "originals", root / "thumbnails", root / "tmp"):
        directory.mkdir(parents=True, exist_ok=True)


def _resolve_host(host: str) -> list[str]:
    """Resolve *host* to address strings for the SSRF validation seam."""

    try:
        records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeImageURLError(f"could not resolve image host {host!r}") from exc

    addresses: list[str] = []
    for record in records:
        sockaddr = record[4]
        address = sockaddr[0] if isinstance(sockaddr, tuple) else sockaddr
        if address not in addresses:
            addresses.append(address)
    return addresses


def _validate_image_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise UnsafeImageURLError("image URLs must use HTTPS and include a host")

    try:
        addresses = _resolve_host(parsed.hostname)
    except UnsafeImageURLError:
        raise
    except Exception as exc:
        raise UnsafeImageURLError("could not resolve image host") from exc

    if not addresses:
        raise UnsafeImageURLError("image host did not resolve")

    for address in addresses:
        try:
            resolved = ipaddress.ip_address(address)
        except ValueError as exc:
            raise UnsafeImageURLError(
                "image host resolved to an invalid address"
            ) from exc
        if not (
            resolved.is_global
            and not resolved.is_private
            and not resolved.is_loopback
            and not resolved.is_link_local
            and not resolved.is_reserved
            and not resolved.is_multicast
            and not resolved.is_unspecified
        ):
            raise UnsafeImageURLError("image host resolves to a non-public address")


def _header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", {})
    try:
        value = headers.get(name)
    except AttributeError:
        value = None
    if value is not None:
        return str(value)
    try:
        for key, item in headers.items():
            if str(key).lower() == name.lower():
                return str(item)
    except AttributeError:
        return None
    return None


def _magic_matches(mime_type: str, data: bytes) -> bool:
    if mime_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if mime_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _default_fetch(method: str, url: str, **kwargs: Any) -> Any:
    return requests.request(method, url, **kwargs)


def download_image(
    url: str,
    *,
    max_bytes: int = 26_214_400,
    timeout: float = 20.0,
    max_redirects: int = 4,
    fetch: Callable[..., Any] | None = None,
) -> DownloadedImage:
    """Fetch and validate an image without allowing SSRF or unbounded bodies."""

    if max_bytes < 0:
        raise ValueError("max_bytes must not be negative")
    if max_redirects < 0:
        raise ValueError("max_redirects must not be negative")

    requested_url = url
    current_url = url
    transport = _default_fetch if fetch is None else fetch

    for redirect_count in range(max_redirects + 1):
        _validate_image_url(current_url)
        response = transport(
            "GET",
            current_url,
            allow_redirects=False,
            stream=True,
            timeout=(timeout, timeout),
        )
        try:
            status_code = int(getattr(response, "status_code", 0))
            if status_code in _REDIRECT_STATUSES:
                location = _header(response, "Location")
                if not location:
                    raise UnsafeImageURLError("image redirect has no Location header")
                if redirect_count >= max_redirects:
                    raise UnsafeImageURLError("image redirect limit exceeded")
                current_url = urljoin(current_url, location)
                continue
            if status_code != 200:
                raise MediaStorageError(f"image download returned HTTP {status_code}")

            declared = _header(response, "Content-Type")
            mime_type = declared.split(";", 1)[0].strip().lower() if declared else ""
            if mime_type not in _MIME_FORMATS:
                raise UnsupportedImageMIMEError(
                    f"unsupported image MIME type: {declared or '<missing>'}"
                )

            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                chunk_bytes = bytes(chunk)
                total += len(chunk_bytes)
                if total > max_bytes:
                    raise ImageTooLargeError("downloaded image exceeds byte limit")
                chunks.append(chunk_bytes)
            data = b"".join(chunks)
            if not _magic_matches(mime_type, data):
                raise MalformedImageError("image magic bytes do not match MIME type")
            return DownloadedImage(requested_url, mime_type, data)
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()

    raise UnsafeImageURLError("image redirect limit exceeded")


def _decode_image(data: bytes) -> tuple[Image.Image, str]:
    try:
        with Image.open(BytesIO(data)) as source:
            source.load()
            image_format = (source.format or "").upper()
            image = source.copy()
    except Exception as exc:
        raise MalformedImageError("image bytes could not be decoded") from exc

    if image_format not in {"JPEG", "PNG", "WEBP"}:
        image.close()
        raise MalformedImageError("unsupported decoded image format")
    return image, image_format


#: JPEG/WEBP encoder quality for every stored variant. Pillow's implicit
#: defaults (75 for JPEG, 80 for WEBP) leave blocky artifacts that the feed
#: magnifies by upscaling, so encode at an explicit near-transparent quality
#: instead. PNG is lossless and ignores this.
_ENCODE_QUALITY = 88


def _encode_image(image: Image.Image, image_format: str) -> bytes:
    options: dict[str, Any] = {}
    if image_format == "JPEG":
        options = {"quality": _ENCODE_QUALITY, "optimize": True}
    elif image_format == "WEBP":
        options = {"quality": _ENCODE_QUALITY}
    output = BytesIO()
    try:
        image.save(output, format=image_format, **options)
    except Exception as exc:
        raise MalformedImageError("decoded image could not be re-encoded") from exc
    return output.getvalue()


def _normalized_for_encoding(image: Image.Image, image_format: str) -> Image.Image:
    """Convert JPEG-incompatible modes to RGB, closing a swapped source."""

    if image_format == "JPEG" and image.mode in {"CMYK", "P", "RGBA", "LA"}:
        converted = image.convert("RGB")
        image.close()
        return converted
    return image


def _build_thumbnail(
    image: Image.Image, image_format: str, thumbnail_max: int
) -> bytes:
    """Downscale a copy of *image* and encode it as thumbnail bytes."""

    thumbnail = image.copy()
    try:
        thumbnail.thumbnail((thumbnail_max, thumbnail_max), Image.Resampling.LANCZOS)
        return _encode_image(thumbnail, image_format)
    finally:
        thumbnail.close()


def store_variants(
    data: bytes,
    root: Path,
    *,
    # Longest thumbnail edge. 800 matches the feed column's ~800 CSS px so
    # collapsed cards render 1:1 instead of upscaling a smaller file.
    thumbnail_max: int = 800,
) -> StoredImage:
    """Normalize an image and atomically store its original and thumbnail."""

    if thumbnail_max <= 0:
        raise ValueError("thumbnail_max must be positive")

    root = Path(root)
    ensure_media_tree(root)
    image, image_format = _decode_image(data)
    temporary_paths: list[Path] = []
    moved_paths: list[Path] = []
    try:
        image = _normalized_for_encoding(image, image_format)
        original_data = _encode_image(image, image_format)
        thumbnail_data = _build_thumbnail(image, image_format, thumbnail_max)

        extension = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}[image_format]
        mime_type = {
            "JPEG": "image/jpeg",
            "PNG": "image/png",
            "WEBP": "image/webp",
        }[image_format]
        original_path = Path("originals") / f"{uuid.uuid4().hex}{extension}"
        thumbnail_path = Path("thumbnails") / f"{uuid.uuid4().hex}{extension}"
        original_final = root / original_path
        thumbnail_final = root / thumbnail_path

        original_tmp = root / "tmp" / f"{original_path.name}.tmp"
        thumbnail_tmp = root / "tmp" / f"{thumbnail_path.name}.tmp"
        temporary_paths.extend((original_tmp, thumbnail_tmp))
        original_tmp.write_bytes(original_data)
        thumbnail_tmp.write_bytes(thumbnail_data)
        os.replace(original_tmp, original_final)
        moved_paths.append(original_final)
        os.replace(thumbnail_tmp, thumbnail_final)
        moved_paths.append(thumbnail_final)
        temporary_paths.clear()

        return StoredImage(
            original_path=str(original_path),
            thumbnail_path=str(thumbnail_path),
            mime_type=mime_type,
            width=image.width,
            height=image.height,
            original_size=len(original_data),
            thumbnail_size=len(thumbnail_data),
        )
    except Exception:
        for path in (*temporary_paths, *moved_paths):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        image.close()


def regenerate_thumbnail(
    root: Path,
    original_path: str,
    thumbnail_path: str,
    *,
    thumbnail_max: int = 800,
) -> None:
    """Rebuild an existing thumbnail file from its stored full-size original.

    Rewrites *thumbnail_path* in place - same filename, so the public URL
    and any cached references stay valid - using the current thumbnail size
    and encoder quality. The original file is never modified. Raises
    :class:`MalformedImageError` if the original cannot be decoded and
    :class:`MediaPathTraversalError` for unsafe paths.
    """

    if thumbnail_max <= 0:
        raise ValueError("thumbnail_max must be positive")

    root = Path(root)
    ensure_media_tree(root)
    original_file = resolve_media_path(root, original_path)
    thumbnail_file = resolve_media_path(root, thumbnail_path)
    image, image_format = _decode_image(original_file.read_bytes())
    try:
        image = _normalized_for_encoding(image, image_format)
        thumbnail_data = _build_thumbnail(image, image_format, thumbnail_max)
    finally:
        image.close()

    temporary = root / "tmp" / f"{thumbnail_file.name}.tmp"
    temporary.write_bytes(thumbnail_data)
    os.replace(temporary, thumbnail_file)


def resolve_media_path(root: Path, relpath: str) -> Path:
    """Resolve a root-relative media path, rejecting traversal and escapes."""

    root = Path(root)
    try:
        raw = os.fspath(relpath)
    except TypeError as exc:
        raise MediaPathTraversalError("media path must be path-like") from exc
    if not isinstance(raw, str):
        raise MediaPathTraversalError("media path must be text")
    windows_path = PureWindowsPath(raw)
    if (
        not raw
        or "\x00" in raw
        or Path(raw).is_absolute()
        or ".." in raw
        or windows_path.drive
        or windows_path.root
    ):
        raise MediaPathTraversalError("media path is empty, absolute, or traversing")

    candidate = (root / raw).resolve()
    root_resolved = root.resolve()
    if candidate == root_resolved:
        raise MediaPathTraversalError("media path must identify a file")
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise MediaPathTraversalError("media path escapes media root") from exc
    return candidate


def delete_variants(root: Path, original_path: str, thumbnail_path: str) -> None:
    """Delete both image variants, safely tolerating already absent files."""

    for relpath in (original_path, thumbnail_path):
        resolve_media_path(root, relpath).unlink(missing_ok=True)


def _row_path(row: Any, name: str) -> str:
    value = getattr(row, name)
    return os.fspath(value) if isinstance(value, os.PathLike) else str(value)


def _media_file_exists(root: Path, relpath: str) -> bool:
    try:
        return resolve_media_path(root, relpath).is_file()
    except (MediaPathTraversalError, OSError):
        return False


def reconcile_media(
    root: Path,
    image_rows: Iterable[Any],
) -> ReconcileReport:
    """Remove unreferenced variants and report rows with missing files."""

    root = Path(root).resolve()
    rows = list(image_rows)
    referenced: set[str] = set()
    incomplete_rows: list[dict[str, Any]] = []
    for row in rows:
        original_path = _row_path(row, "original_path")
        thumbnail_path = _row_path(row, "thumbnail_path")
        referenced.update((original_path, thumbnail_path))
        missing = [
            path
            for path in (original_path, thumbnail_path)
            if not _media_file_exists(root, path)
        ]
        if missing:
            incomplete_rows.append({"post_id": row.post_id, "missing": missing})

    removed_files: list[str] = []
    for directory_name in ("originals", "thumbnails"):
        try:
            directory = resolve_media_path(root, directory_name)
        except MediaPathTraversalError:
            continue
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if not (path.is_file() or path.is_symlink()):
                continue
            relative = path.relative_to(root).as_posix()
            if relative in referenced:
                continue
            path.unlink(missing_ok=True)
            removed_files.append(relative)

    removed_files.sort()
    return ReconcileReport(
        removed_files=removed_files,
        incomplete_rows=incomplete_rows,
    )
