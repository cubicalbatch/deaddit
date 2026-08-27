"""Secure storage, normalization and download of generated images."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from deaddit import create_app
from deaddit.images import storage
from deaddit.images.storage import (
    ImageTooLargeError,
    MalformedImageError,
    MediaPathTraversalError,
    UnsafeImageURLError,
    UnsupportedImageMIMEError,
    delete_variants,
    download_image,
    media_root,
    reconcile_media,
    resolve_media_path,
    store_variants,
)


class FakeResponse:
    def __init__(self, status_code: int, headers: dict[str, str], *chunks: bytes):
        self.status_code = status_code
        self.headers = headers
        self.chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size: int):
        assert chunk_size > 0
        return iter(self.chunks)

    def close(self):
        self.closed = True


def image_bytes(image_format: str, *, exif: bool = False) -> bytes:
    image = Image.new("RGB", (64, 48), color=(30, 120, 220))
    output = BytesIO()
    kwargs = {"format": image_format}
    if exif:
        exif_data = Image.Exif()
        exif_data[0x010E] = "private marker"
        kwargs["exif"] = exif_data
    image.save(output, **kwargs)
    return output.getvalue()


def test_store_variants_normalizes_supported_formats_and_strips_exif(tmp_path):
    for image_format, extension, mime_type in (
        ("JPEG", ".jpg", "image/jpeg"),
        ("PNG", ".png", "image/png"),
        ("WEBP", ".webp", "image/webp"),
    ):
        root = tmp_path / image_format.lower()
        result = store_variants(
            image_bytes(image_format, exif=image_format == "JPEG"),
            root,
            thumbnail_max=20,
        )

        assert result.mime_type == mime_type
        assert (result.width, result.height) == (64, 48)
        assert result.original_size > 0 and result.thumbnail_size > 0
        assert Path(result.original_path).parts[0] == "originals"
        assert Path(result.thumbnail_path).parts[0] == "thumbnails"
        assert Path(result.original_path).suffix == extension
        assert Path(result.thumbnail_path).suffix == extension
        assert (root / result.original_path).is_file()
        assert (root / result.thumbnail_path).is_file()
        # Opaque UUID names, and no temp file left behind.
        assert len(Path(result.original_path).stem) == 32
        assert len(Path(result.thumbnail_path).stem) == 32
        assert not list((root / "tmp").iterdir())

        with Image.open(root / result.original_path) as original:
            assert original.size == (64, 48)
            assert "exif" not in original.info
        with Image.open(root / result.thumbnail_path) as thumbnail:
            assert max(thumbnail.size) <= 20


def test_store_failure_leaves_no_files_and_deletion_is_idempotent(tmp_path):
    failed_root = tmp_path / "failed"
    with pytest.raises(MalformedImageError):
        store_variants(b"not an image", failed_root)
    assert not [path for path in failed_root.rglob("*") if path.is_file()]
    assert not list((failed_root / "tmp").iterdir())

    stored = store_variants(image_bytes("PNG"), tmp_path)
    delete_variants(tmp_path, stored.original_path, stored.thumbnail_path)
    delete_variants(tmp_path, stored.original_path, stored.thumbnail_path)
    assert not (tmp_path / stored.original_path).exists()
    assert not (tmp_path / stored.thumbnail_path).exists()

    # Reconciliation removes files no row claims, and reports rows whose
    # files have gone missing without deleting anything they still own.
    stored = store_variants(image_bytes("PNG"), tmp_path)
    (tmp_path / "originals" / "orphan.png").write_bytes(b"orphan")
    rows = [
        SimpleNamespace(
            post_id=1,
            original_path=stored.original_path,
            thumbnail_path=stored.thumbnail_path,
        ),
        SimpleNamespace(
            post_id=42,
            original_path=stored.original_path,
            thumbnail_path="thumbnails/missing.png",
        ),
    ]

    report = reconcile_media(tmp_path, rows)

    assert report.removed_files == ["originals/orphan.png"]
    assert report.incomplete_rows == [
        {"post_id": 42, "missing": ["thumbnails/missing.png"]}
    ]
    assert (tmp_path / stored.original_path).is_file()


def test_download_image_streams_payload_and_validates_each_redirect_hop(monkeypatch):
    payload = image_bytes("JPEG")
    responses = {
        "https://images.example/1": FakeResponse(302, {"Location": "/2"}),
        "https://images.example/2": FakeResponse(
            307, {"Location": "https://cdn.example/3"}
        ),
        "https://cdn.example/3": FakeResponse(
            200, {"Content-Type": "image/jpeg; charset=binary"}, payload
        ),
    }
    resolved: list[str] = []
    calls: list[tuple[str, str, dict]] = []

    def fetch(method: str, url: str, **kwargs):
        calls.append((method, url, kwargs))
        return responses[url]

    monkeypatch.setattr(
        storage, "_resolve_host", lambda host: resolved.append(host) or ["8.8.8.8"]
    )
    result = download_image("https://images.example/1", fetch=fetch, timeout=3.5)

    assert result.data == payload
    assert result.mime_type == "image/jpeg"
    # Every hop is resolved and vetted by us, never by requests' own follower.
    assert resolved == ["images.example", "images.example", "cdn.example"]
    assert calls[0] == (
        "GET",
        "https://images.example/1",
        {"allow_redirects": False, "stream": True, "timeout": (3.5, 3.5)},
    )
    assert all(response.closed for response in responses.values())


def test_download_image_rejects_unsafe_or_unusable_responses(monkeypatch):
    """Every rejection path, as a table: bad scheme, SSRF target, bad body, oversize."""

    def attempt(*, address="8.8.8.8", url="https://images.example/file", **kwargs):
        monkeypatch.setattr(storage, "_resolve_host", lambda host: [address])
        responses = kwargs.pop("responses", [])
        fetch = kwargs.pop(
            "fetch", lambda *a, **kw: responses[0] if responses else None
        )
        with pytest.raises(kwargs.pop("error")):
            download_image(url, fetch=fetch, **kwargs)
        assert all(response.closed for response in responses)

    # Plaintext HTTP, up front and after a redirect.
    attempt(url="http://images.example/file", error=UnsafeImageURLError)
    attempt(
        responses=[FakeResponse(302, {"Location": "http://private.example/file"})],
        error=UnsafeImageURLError,
    )

    # Redirect chains are bounded.
    chain = {
        f"https://images.example/{n}": FakeResponse(
            302, {"Location": f"https://images.example/{n + 1}"}
        )
        for n in range(3)
    }
    attempt(
        url="https://images.example/0",
        responses=list(chain.values()),
        fetch=lambda method, u, **kw: chain[u],
        max_redirects=2,
        error=UnsafeImageURLError,
    )

    # SSRF: nothing outside the public internet is a valid image host.
    for address in (
        "127.0.0.1",
        "10.0.0.1",
        "::1",
        "169.254.1.1",
        "192.168.0.1",
        "fc00::1",
        "224.0.0.1",
    ):
        attempt(address=address, error=UnsafeImageURLError)

    # Content type must be a supported image, and must match the actual bytes.
    for mime_type, payload, error in (
        ("text/html", b"<html>", UnsupportedImageMIMEError),
        ("image/gif", b"GIF89a", UnsupportedImageMIMEError),
        ("image/jpeg", image_bytes("PNG"), MalformedImageError),
    ):
        attempt(
            responses=[FakeResponse(200, {"Content-Type": mime_type}, payload)],
            error=error,
        )

    # The byte ceiling is enforced mid-stream, not from Content-Length.
    attempt(
        responses=[
            FakeResponse(
                200,
                {"Content-Type": "image/png"},
                b"\x89PNG\r\n\x1a\n",
                b"more than allowed",
            )
        ],
        max_bytes=10,
        error=ImageTooLargeError,
    )


def test_media_paths_are_confined_to_the_configured_root(tmp_path):
    app = create_app(
        {
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "TESTING": True,
            "GENERATED_IMAGES_ROOT": str(tmp_path),
        }
    )
    assert media_root(app) == tmp_path

    safe = tmp_path / "originals" / ("a" * 32 + ".jpg")
    safe.parent.mkdir(parents=True)
    safe.touch()
    assert resolve_media_path(tmp_path, safe.relative_to(tmp_path).as_posix()) == safe

    outside = tmp_path.parent / "outside-media-target"
    outside.touch()
    (tmp_path / "originals" / "link.jpg").symlink_to(outside)

    for relpath in (
        "",
        "../secrets",
        "/etc/passwd",
        "originals/../secret",
        "C:/secret",
        "originals/link.jpg",
    ):
        with pytest.raises(MediaPathTraversalError):
            resolve_media_path(tmp_path, relpath)
