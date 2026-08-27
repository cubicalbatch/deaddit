"""Deterministic tests for secure generated-image storage."""

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
    ReconcileReport,
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


def assert_no_files(root: Path) -> None:
    assert not [path for path in root.rglob("*") if path.is_file()]


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
        assert result.original_size > 0
        assert result.thumbnail_size > 0
        assert Path(result.original_path).parts[0] == "originals"
        assert Path(result.thumbnail_path).parts[0] == "thumbnails"
        assert Path(result.original_path).suffix == extension
        assert Path(result.thumbnail_path).suffix == extension
        assert (root / result.original_path).is_file()
        assert (root / result.thumbnail_path).is_file()
        assert not list((root / "tmp").iterdir())
        assert len(Path(result.original_path).stem) == 32
        assert len(Path(result.thumbnail_path).stem) == 32

        with Image.open(root / result.original_path) as original:
            assert original.size == (64, 48)
            assert "exif" not in original.info
        with Image.open(root / result.thumbnail_path) as thumbnail:
            assert max(thumbnail.size) <= 20


def test_store_variants_failure_leaves_no_files(tmp_path):
    root = tmp_path / "media"
    with pytest.raises(MalformedImageError):
        store_variants(b"not an image", root)

    assert_no_files(root)
    assert not list((root / "tmp").iterdir())


def test_download_image_happy_path_and_transport_options(monkeypatch):
    payload = image_bytes("PNG")
    response = FakeResponse(200, {"Content-Type": "image/png; charset=binary"}, payload)
    calls: list[tuple[str, str, dict]] = []

    def resolver(host: str):
        assert host == "images.example"
        return ["8.8.8.8"]

    def fetch(method: str, url: str, **kwargs):
        calls.append((method, url, kwargs))
        return response

    monkeypatch.setattr(storage, "_resolve_host", resolver)
    result = download_image("https://images.example/a.png", fetch=fetch, timeout=3.5)

    assert result.data == payload
    assert result.mime_type == "image/png"
    assert result.url == "https://images.example/a.png"
    assert calls == [
        (
            "GET",
            "https://images.example/a.png",
            {
                "allow_redirects": False,
                "stream": True,
                "timeout": (3.5, 3.5),
            },
        )
    ]
    assert response.closed


def test_download_image_redirects_are_manual_and_validated(monkeypatch):
    payload = image_bytes("JPEG")
    responses = {
        "https://images.example/1": FakeResponse(302, {"Location": "/2"}),
        "https://images.example/2": FakeResponse(
            307, {"Location": "https://cdn.example/3"}
        ),
        "https://cdn.example/3": FakeResponse(
            200, {"Content-Type": "image/jpeg"}, payload
        ),
    }
    resolved: list[str] = []

    def resolver(host: str):
        resolved.append(host)
        return ["8.8.8.8"]

    def fetch(method: str, url: str, **kwargs):
        assert kwargs["allow_redirects"] is False
        assert kwargs["stream"] is True
        return responses[url]

    monkeypatch.setattr(storage, "_resolve_host", resolver)
    result = download_image("https://images.example/1", fetch=fetch, max_redirects=2)

    assert result.data == payload
    assert resolved == ["images.example", "images.example", "cdn.example"]
    assert all(response.closed for response in responses.values())


def test_download_image_rejects_http_and_http_redirect(monkeypatch):
    monkeypatch.setattr(storage, "_resolve_host", lambda host: ["8.8.8.8"])
    with pytest.raises(UnsafeImageURLError):
        download_image("http://images.example/file", fetch=lambda *args, **kwargs: None)

    response = FakeResponse(302, {"Location": "http://private.example/file"})
    with pytest.raises(UnsafeImageURLError):
        download_image(
            "https://images.example/file",
            fetch=lambda *args, **kwargs: response,
        )
    assert response.closed


def test_download_image_rejects_too_many_redirects(monkeypatch):
    monkeypatch.setattr(storage, "_resolve_host", lambda host: ["8.8.8.8"])
    responses = {
        f"https://images.example/{number}": FakeResponse(
            302, {"Location": f"https://images.example/{number + 1}"}
        )
        for number in range(3)
    }
    with pytest.raises(UnsafeImageURLError):
        download_image(
            "https://images.example/0",
            max_redirects=2,
            fetch=lambda method, url, **kwargs: responses[url],
        )
    assert all(response.closed for response in responses.values())


@pytest.mark.parametrize(
    ("mime_type", "payload", "error"),
    [
        ("text/html", b"<html>", UnsupportedImageMIMEError),
        ("image/gif", b"GIF89a", UnsupportedImageMIMEError),
        ("image/jpeg", image_bytes("PNG"), MalformedImageError),
    ],
)
def test_download_image_rejects_unsupported_or_mismatched_content(
    monkeypatch, mime_type, payload, error
):
    monkeypatch.setattr(storage, "_resolve_host", lambda host: ["8.8.8.8"])
    response = FakeResponse(200, {"Content-Type": mime_type}, payload)
    with pytest.raises(error):
        download_image(
            "https://images.example/file",
            fetch=lambda *args, **kwargs: response,
        )
    assert response.closed


def test_download_image_enforces_hard_byte_ceiling(monkeypatch):
    monkeypatch.setattr(storage, "_resolve_host", lambda host: ["8.8.8.8"])
    response = FakeResponse(
        200,
        {"Content-Type": "image/png"},
        b"\x89PNG\r\n\x1a\n",
        b"more than allowed",
    )
    with pytest.raises(ImageTooLargeError):
        download_image(
            "https://images.example/file",
            max_bytes=10,
            fetch=lambda *args, **kwargs: response,
        )
    assert response.closed


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "::1",
        "169.254.1.1",
        "192.168.0.1",
        "fc00::1",
        "224.0.0.1",
    ],
)
def test_download_image_rejects_non_global_destinations(monkeypatch, address):
    monkeypatch.setattr(storage, "_resolve_host", lambda host: [address])
    with pytest.raises(UnsafeImageURLError):
        download_image(
            "https://images.example/file", fetch=lambda *args, **kwargs: None
        )


def test_download_image_accepts_global_destinations(monkeypatch):
    monkeypatch.setattr(storage, "_resolve_host", lambda host: ["8.8.8.8"])
    payload = image_bytes("WEBP")
    response = FakeResponse(200, {"Content-Type": "image/webp"}, payload)
    result = download_image(
        "https://images.example/file", fetch=lambda *args, **kwargs: response
    )
    assert result.data == payload


def test_delete_variants_is_idempotent(tmp_path):
    result = store_variants(image_bytes("PNG"), tmp_path)
    delete_variants(tmp_path, result.original_path, result.thumbnail_path)
    delete_variants(tmp_path, result.original_path, result.thumbnail_path)
    assert not (tmp_path / result.original_path).exists()
    assert not (tmp_path / result.thumbnail_path).exists()


def test_reconcile_media_removes_orphans_and_reports_incomplete_rows(tmp_path):
    kept = store_variants(image_bytes("PNG"), tmp_path)
    orphan = tmp_path / "originals" / "orphan.png"
    orphan.write_bytes(b"orphan")
    missing_row = SimpleNamespace(
        post_id=42,
        original_path=kept.original_path,
        thumbnail_path="thumbnails/missing.png",
    )
    complete_row = SimpleNamespace(
        post_id=1,
        original_path=kept.original_path,
        thumbnail_path=kept.thumbnail_path,
    )

    report = reconcile_media(tmp_path, [complete_row, missing_row])

    assert isinstance(report, ReconcileReport)
    assert report.removed_files == ["originals/orphan.png"]
    assert report.incomplete_rows == [
        {"post_id": 42, "missing": ["thumbnails/missing.png"]}
    ]
    assert (tmp_path / kept.original_path).is_file()


def test_resolve_media_path_rejects_traversal_and_accepts_normal_path(tmp_path):
    safe = tmp_path / "originals" / ("a" * 32 + ".jpg")
    safe.parent.mkdir(parents=True)
    safe.touch()
    assert resolve_media_path(tmp_path, safe.relative_to(tmp_path).as_posix()) == safe

    for relpath in (
        "",
        "../secrets",
        "/etc/passwd",
        "originals/../secret",
        "C:/secret",
    ):
        with pytest.raises(MediaPathTraversalError):
            resolve_media_path(tmp_path, relpath)

    outside = tmp_path.parent / "outside-media-target"
    outside.touch()
    link = tmp_path / "originals" / "link.jpg"
    link.symlink_to(outside)
    with pytest.raises(MediaPathTraversalError):
        resolve_media_path(tmp_path, "originals/link.jpg")


def test_media_root_honors_config_override(tmp_path):
    app = create_app(
        {
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "TESTING": True,
            "GENERATED_IMAGES_ROOT": str(tmp_path),
        }
    )
    assert media_root(app) == tmp_path
