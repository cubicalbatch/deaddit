"""Operator reconciliation CLI for generated websites."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from click.testing import CliRunner

from deaddit.cli import cli
from deaddit.websites import cli as websites_cli


@dataclass
class _Row:
    post_id: int
    storage_path: str
    byte_size: int
    sha256: str


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _App:
    config = {"SQLALCHEMY_DATABASE_URI": "sqlite://"}
    instance_path = "/tmp/test-instance"

    def app_context(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _install(monkeypatch, rows):
    app = _App()
    monkeypatch.setattr(websites_cli, "create_app", lambda: app)
    monkeypatch.setattr(
        websites_cli, "GeneratedWebsite", SimpleNamespace(query=_Query(rows))
    )
    return app


def test_dry_run_reports_without_deleting(tmp_path, monkeypatch):
    pages = tmp_path / "pages"
    pages.mkdir()
    kept = pages / "kept.html"
    kept.write_bytes(b"kept")
    orphan = pages / "orphan.html"
    orphan.write_bytes(b"orphan")
    missing = "pages/missing.html"
    changed = pages / "changed.html"
    changed.write_bytes(b"changed")
    rows = [
        _Row(1, "pages/kept.html", 4, ""),
        _Row(2, missing, 1, ""),
        _Row(3, "pages/changed.html", 99, "wrong"),
    ]
    _install(monkeypatch, rows)

    result = CliRunner().invoke(
        cli, ["websites", "reconcile-websites", "--root", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert "orphan.html" in result.output
    assert "post_id=2 storage_path=pages/missing.html" in result.output
    assert "post_id=3 storage_path=pages/changed.html" in result.output
    assert orphan.exists() and kept.exists() and changed.read_bytes() == b"changed"


def test_apply_removes_orphan_and_symlink_target_stays(tmp_path, monkeypatch):
    pages = tmp_path / "pages"
    pages.mkdir()
    orphan = pages / "orphan.html"
    orphan.write_text("orphan")
    outside = tmp_path / "outside.html"
    outside.write_text("outside")
    link = pages / "evil.html"
    link.symlink_to(outside)
    _install(monkeypatch, [])

    result = CliRunner().invoke(
        cli, ["websites", "reconcile-websites", "--root", str(tmp_path), "--apply"]
    )

    assert result.exit_code == 0, result.output
    assert not orphan.exists() and not link.exists() and outside.exists()


def test_command_is_registered():
    result = CliRunner().invoke(cli, ["websites", "--help"])
    assert result.exit_code == 0
    assert "reconcile-websites" in result.output
