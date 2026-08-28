"""Secure storage, URL-hint normalization, and reconciliation for websites."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from deaddit import create_app
from deaddit.websites import storage
from deaddit.websites.storage import (
    WEBSITE_MAX_OUTPUT_TOKENS_FLOOR,
    InvalidHostnameHintError,
    InvalidPageNameHintError,
    WebsitePathTraversalError,
    WebsiteStorageError,
    allocate_public_path,
    delete_website,
    normalize_hostname_hint,
    normalize_page_name_hint,
    reconcile_websites,
    resolve_website_path,
    resolve_website_settings,
    store_website,
    website_root,
)

# ---------------------------------------------------------------------------
# Root configuration
# ---------------------------------------------------------------------------


def test_generated_websites_root_defaults_under_instance_path():
    app = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite://", "TESTING": True})
    assert app.config["GENERATED_WEBSITES_ROOT"] == os.path.join(
        app.instance_path, "generated_websites"
    )


def test_website_root_honors_explicit_config(tmp_path):
    app = create_app(
        {
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "TESTING": True,
            "GENERATED_WEBSITES_ROOT": str(tmp_path),
        }
    )
    assert website_root(app) == tmp_path


# ---------------------------------------------------------------------------
# Hostname hint normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hint,expected",
    [
        ("Example.COM", "example.com"),
        ("  www.Fake-Observatory.com  ", "www.fake-observatory.com"),
        ("https://example.com", "example.com"),
        ("http://example.com/", "example.com"),
        ("//example.com", "example.com"),
        ("example.com:8080", "example.com"),
        ("example.com/some/path?x=1#frag", "example.com"),
        ("https://example.com:8080/some/path", "example.com"),
        ("a-b-c.example.co.uk", "a-b-c.example.co.uk"),
        ("x" * 63 + ".com", "x" * 63 + ".com"),
        # The path portion - including any ".." inside it - is discarded
        # entirely before hostname validation runs, so traversal markers
        # confined to the path never reach (or need to be rejected by) the
        # hostname check; only the still-clean host remains.
        ("example.com/../../etc/passwd", "example.com"),
    ],
)
def test_normalize_hostname_hint_accepts_valid_forms(hint, expected):
    assert normalize_hostname_hint(hint) == expected


@pytest.mark.parametrize(
    "hint",
    [
        "",
        "   ",
        "user:pass@example.com",
        "https://user:pass@example.com/",
        "example.com\\evil.com",
        "..\\evil",
        "C:\\Windows\\System32",
        "/etc/passwd",
        "192.168.1.1",
        "192.168.1.1:8080",
        "10.0.0.1",
        "::1",
        "2001:db8::1",
        "fe80::1",
        "[::1]",
        "[::1]:8080",
        "0.0.0.0",
        "..",
        "example..com",
        ".example.com",
        "example.com.",
        "-example.com",
        "example-.com",
        "exämple.com",
        "example\x00.com",
        "example\n.com",
        "example\t.com",
        "x" * 64 + ".com",
        ".".join(["x" * 50] * 6),  # over 253 chars total
        "javascript:alert(1)",
    ],
)
def test_normalize_hostname_hint_rejects_unsafe_forms(hint):
    with pytest.raises(InvalidHostnameHintError):
        normalize_hostname_hint(hint)


def test_normalize_hostname_hint_rejects_non_string():
    with pytest.raises(InvalidHostnameHintError):
        normalize_hostname_hint(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Page-name hint normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hint,expected",
    [
        ("Aurora Map", "aurora-map.html"),
        ("aurora-map.html", "aurora-map.html"),
        ("AURORA-MAP.HTML", "aurora-map.html"),
        ("  aurora   map  ", "aurora-map.html"),
        ("aurora_map!!", "aurora-map.html"),
        ("2024 review", "2024-review.html"),
    ],
)
def test_normalize_page_name_hint_accepts_and_slugifies(hint, expected):
    assert normalize_page_name_hint(hint) == expected


def test_normalize_page_name_hint_always_ends_in_single_html_suffix():
    assert normalize_page_name_hint("report.html.html") == "report-html.html"


@pytest.mark.parametrize(
    "hint",
    [
        "../../../etc/passwd",
        "..\\..\\windows\\system32",
        "C:\\Windows\\System32\\evil",
        "/absolute/path",
        "....",
        "???",
        "!!!",
        "",
        "   ",
    ],
)
def test_normalize_page_name_hint_neutralizes_or_rejects_traversal_input(hint):
    # Dangerous constructs are never used to resolve a filesystem path (the
    # opaque uuid storage name handles that), so this function either
    # extracts a safe word-only slug or - when nothing usable remains -
    # rejects outright. Either outcome is safe; assert one of them happens
    # and that a safe result never contains a separator or "..".
    try:
        result = normalize_page_name_hint(hint)
    except InvalidPageNameHintError:
        return
    assert "/" not in result
    assert "\\" not in result
    assert ".." not in result.removesuffix(".html")
    assert result.endswith(".html")


def test_normalize_page_name_hint_rejects_non_string():
    with pytest.raises(InvalidPageNameHintError):
        normalize_page_name_hint(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Path allocation / collision resolution
# ---------------------------------------------------------------------------


def test_allocate_public_path_uses_pretty_path_when_free():
    allocated = allocate_public_path(
        "example.com", "aurora-map.html", is_public_path_taken=lambda _: False
    )
    assert allocated.public_path == "example.com/aurora-map.html"
    assert allocated.hostname == "example.com"
    assert allocated.page_name == "aurora-map.html"


def test_allocate_public_path_appends_suffix_on_collision():
    taken = {"example.com/aurora-map.html"}
    allocated = allocate_public_path(
        "example.com", "aurora-map.html", is_public_path_taken=taken.__contains__
    )
    assert allocated.public_path != "example.com/aurora-map.html"
    assert allocated.public_path.startswith("example.com/aurora-map-")
    assert allocated.public_path.endswith(".html")
    assert allocated.page_name.endswith(".html")


def test_allocate_public_path_raises_after_exhausting_attempts():
    with pytest.raises(WebsiteStorageError):
        allocate_public_path(
            "example.com",
            "aurora-map.html",
            is_public_path_taken=lambda _: True,
            max_attempts=3,
        )


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def test_store_website_writes_atomically_with_correct_size_and_hash(tmp_path):
    html = "<!doctype html><html><body>hi</body></html>"
    stored = store_website(html, tmp_path)

    assert stored.storage_path.startswith(
        "pages" + os.sep
    ) or stored.storage_path.startswith("pages/")
    assert Path(stored.storage_path).suffix == ".html"
    assert stored.byte_size == len(html.encode("utf-8"))
    assert stored.sha256 == hashlib.sha256(html.encode("utf-8")).hexdigest()

    final_file = tmp_path / stored.storage_path
    assert final_file.is_file()
    assert final_file.read_text(encoding="utf-8") == html

    # No temp file left behind.
    tmp_dir = tmp_path / "tmp"
    assert list(tmp_dir.iterdir()) == []


def test_store_website_names_are_opaque_and_unique(tmp_path):
    first = store_website("<!doctype html><html></html>", tmp_path)
    second = store_website("<!doctype html><html></html>", tmp_path)
    assert first.storage_path != second.storage_path
    # Opaque uuid4-hex names: never derived from any request input.
    stem = Path(first.storage_path).stem
    assert len(stem) == 32
    assert all(ch in "0123456789abcdef" for ch in stem)


def test_store_website_accepts_bytes(tmp_path):
    data = b"<!doctype html><html></html>"
    stored = store_website(data, tmp_path)
    assert stored.byte_size == len(data)
    assert stored.sha256 == hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Traversal-proof resolution
# ---------------------------------------------------------------------------


def test_resolve_website_path_returns_contained_file(tmp_path):
    (tmp_path / "pages").mkdir()
    target = tmp_path / "pages" / "abc.html"
    target.write_text("hi")
    resolved = resolve_website_path(tmp_path, "pages/abc.html")
    assert resolved == target


@pytest.mark.parametrize(
    "relpath",
    [
        "",
        "..",
        "../secrets",
        "../../etc/passwd",
        "/etc/passwd",
        "pages/../../etc/passwd",
        "pages/..\\..\\etc\\passwd",
        "C:\\Windows\\System32\\evil.html",
        "pages/abc.html\x00.txt",
        "\x00",
    ],
)
def test_resolve_website_path_rejects_traversal_and_escapes(tmp_path, relpath):
    with pytest.raises(WebsitePathTraversalError):
        resolve_website_path(tmp_path, relpath)


def test_resolve_website_path_rejects_root_itself(tmp_path):
    with pytest.raises(WebsitePathTraversalError):
        resolve_website_path(tmp_path, ".")


def test_resolve_website_path_does_not_follow_symlink_out_of_root(tmp_path):
    (tmp_path / "pages").mkdir()
    outside = tmp_path.parent / "outside-website-target"
    outside.write_text("secret")
    link = tmp_path / "pages" / "escape.html"
    link.symlink_to(outside)

    with pytest.raises(WebsitePathTraversalError):
        resolve_website_path(tmp_path, "pages/escape.html")


def test_resolve_website_path_rejects_non_path_like():
    with pytest.raises(WebsitePathTraversalError):
        resolve_website_path(Path("/tmp"), 12345)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


def test_delete_website_is_idempotent(tmp_path):
    stored = store_website("<!doctype html><html></html>", tmp_path)
    final_file = tmp_path / stored.storage_path
    assert final_file.is_file()

    delete_website(tmp_path, stored.storage_path)
    assert not final_file.is_file()

    # Deleting an already-gone file must not raise.
    delete_website(tmp_path, stored.storage_path)


def test_delete_website_still_rejects_traversal(tmp_path):
    with pytest.raises(WebsitePathTraversalError):
        delete_website(tmp_path, "../outside.html")


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def _row(post_id, storage_path, data):
    return SimpleNamespace(
        post_id=post_id,
        storage_path=storage_path,
        byte_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def test_reconcile_websites_dry_run_reports_without_deleting(tmp_path):
    referenced_data = b"<!doctype html><html>kept</html>"
    stored = store_website(referenced_data, tmp_path)
    row = _row(1, stored.storage_path, referenced_data)

    orphan_data = b"<!doctype html><html>orphan</html>"
    orphan = store_website(orphan_data, tmp_path)

    report = reconcile_websites(tmp_path, [row], apply=False)

    assert report.orphaned_files == [orphan.storage_path]
    assert report.missing_rows == []
    assert report.mismatched_rows == []
    # Dry run: nothing was actually removed.
    assert (tmp_path / orphan.storage_path).is_file()
    assert (tmp_path / stored.storage_path).is_file()


def test_reconcile_websites_apply_removes_only_orphaned_files(tmp_path):
    referenced_data = b"<!doctype html><html>kept</html>"
    stored = store_website(referenced_data, tmp_path)
    row = _row(1, stored.storage_path, referenced_data)

    orphan = store_website(b"<!doctype html><html>orphan</html>", tmp_path)

    report = reconcile_websites(tmp_path, [row], apply=True)

    assert report.orphaned_files == [orphan.storage_path]
    assert not (tmp_path / orphan.storage_path).is_file()
    assert (tmp_path / stored.storage_path).is_file()


def test_reconcile_websites_reports_missing_file(tmp_path):
    row = _row(1, "pages/does-not-exist.html", b"data")
    report = reconcile_websites(tmp_path, [row], apply=False)
    assert report.missing_rows == [
        {"post_id": 1, "storage_path": "pages/does-not-exist.html"}
    ]
    assert report.orphaned_files == []
    assert report.mismatched_rows == []


def test_reconcile_websites_reports_traversal_storage_path_as_missing(tmp_path):
    row = _row(1, "../outside.html", b"data")
    report = reconcile_websites(tmp_path, [row], apply=False)
    assert report.missing_rows == [{"post_id": 1, "storage_path": "../outside.html"}]


def test_reconcile_websites_reports_hash_size_mismatch(tmp_path):
    original_data = b"<!doctype html><html>original</html>"
    stored = store_website(original_data, tmp_path)
    row = _row(1, stored.storage_path, original_data)

    # Corrupt the file on disk after the row was recorded.
    (tmp_path / stored.storage_path).write_bytes(
        b"<!doctype html><html>tampered</html>"
    )

    report = reconcile_websites(tmp_path, [row], apply=False)

    assert report.missing_rows == []
    assert len(report.mismatched_rows) == 1
    mismatch = report.mismatched_rows[0]
    assert mismatch["post_id"] == 1
    assert mismatch["expected_sha256"] == hashlib.sha256(original_data).hexdigest()
    assert mismatch["actual_sha256"] != mismatch["expected_sha256"]


def test_reconcile_websites_apply_never_removes_referenced_files(tmp_path):
    data = b"<!doctype html><html>kept</html>"
    stored = store_website(data, tmp_path)
    row = _row(1, stored.storage_path, data)

    report = reconcile_websites(tmp_path, [row], apply=True)
    assert report.orphaned_files == []
    assert (tmp_path / stored.storage_path).is_file()


# ---------------------------------------------------------------------------
# Settings parsing / floor enforcement
# ---------------------------------------------------------------------------


def test_resolve_website_settings_defaults():
    settings = resolve_website_settings(lambda key, default: default)
    assert settings.max_output_tokens == WEBSITE_MAX_OUTPUT_TOKENS_FLOOR
    assert settings.generation_timeout_seconds == 300.0
    assert settings.max_html_bytes == 1_048_576


def test_resolve_website_settings_enforces_output_token_floor():
    values = {
        "WEBSITE_MAX_OUTPUT_TOKENS": "1000",
        "WEBSITE_GENERATION_TIMEOUT_SECONDS": "300",
        "WEBSITE_MAX_HTML_BYTES": "1048576",
    }
    settings = resolve_website_settings(lambda key, default: values.get(key, default))
    assert settings.max_output_tokens == WEBSITE_MAX_OUTPUT_TOKENS_FLOOR


def test_resolve_website_settings_honors_above_floor_configuration():
    values = {"WEBSITE_MAX_OUTPUT_TOKENS": "65536"}
    settings = resolve_website_settings(lambda key, default: values.get(key, default))
    assert settings.max_output_tokens == 65536


@pytest.mark.parametrize("bad_value", ["not-a-number", "", None, "-5", "0"])
def test_resolve_website_settings_falls_back_on_invalid_values(bad_value):
    values = {
        "WEBSITE_MAX_OUTPUT_TOKENS": bad_value,
        "WEBSITE_GENERATION_TIMEOUT_SECONDS": bad_value,
        "WEBSITE_MAX_HTML_BYTES": bad_value,
    }
    settings = resolve_website_settings(lambda key, default: values.get(key, default))
    assert settings.max_output_tokens == WEBSITE_MAX_OUTPUT_TOKENS_FLOOR
    assert settings.generation_timeout_seconds == 300.0
    assert settings.max_html_bytes == 1_048_576


def test_resolve_website_settings_parses_custom_timeout_and_byte_ceiling():
    values = {
        "WEBSITE_GENERATION_TIMEOUT_SECONDS": "120.5",
        "WEBSITE_MAX_HTML_BYTES": "2097152",
    }
    settings = resolve_website_settings(lambda key, default: values.get(key, default))
    assert settings.generation_timeout_seconds == 120.5
    assert settings.max_html_bytes == 2097152


# ---------------------------------------------------------------------------
# Module-level sanity: ensure_website_tree is idempotent and side-effect-free
# at import time (only invoked explicitly).
# ---------------------------------------------------------------------------


def test_ensure_website_tree_creates_pages_and_tmp(tmp_path):
    storage.ensure_website_tree(tmp_path)
    assert (tmp_path / "pages").is_dir()
    assert (tmp_path / "tmp").is_dir()
    # Calling again must not raise.
    storage.ensure_website_tree(tmp_path)
