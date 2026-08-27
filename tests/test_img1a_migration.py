"""Migration round-trip coverage for the Phase 1 image tables."""

import sqlite3

from deaddit import create_app

_PRE_IMAGE_HEAD = "323c82c6f88c"
_IMAGE_TABLES = {"image_provider", "image_model", "post_image"}


def _tables(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()


def _columns(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _foreign_keys(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    finally:
        conn.close()


def test_image_tables_migration_round_trip(tmp_path):
    db_path = tmp_path / "mig.db"
    app = create_app(
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "TESTING": True}
    )
    runner = app.test_cli_runner()

    upgraded = runner.invoke(args=["db", "upgrade"])
    assert upgraded.exit_code == 0, upgraded.output
    assert _IMAGE_TABLES <= _tables(db_path)
    assert {
        "post_id",
        "original_path",
        "thumbnail_path",
        "provider_id",
        "source_prompt",
    } <= _columns(db_path, "post_image")
    assert {"id", "provider_id", "model_identifier", "is_active"} <= _columns(
        db_path, "image_model"
    )

    post_image_fks = _foreign_keys(db_path, "post_image")
    model_fks = _foreign_keys(db_path, "image_model")
    assert any(
        row[3] == "provider_id" and row[2] == "image_provider" and row[6] == "SET NULL"
        for row in post_image_fks
    )
    assert any(row[3] == "post_id" and row[2] == "post" for row in post_image_fks)
    assert any(
        row[3] == "provider_id" and row[2] == "image_provider" and row[6] == "CASCADE"
        for row in model_fks
    )

    down = runner.invoke(args=["db", "downgrade", _PRE_IMAGE_HEAD])
    assert down.exit_code == 0, down.output
    assert not (_IMAGE_TABLES & _tables(db_path))
    assert "post" in _tables(db_path)

    upgraded_again = runner.invoke(args=["db", "upgrade"])
    assert upgraded_again.exit_code == 0, upgraded_again.output
    assert _IMAGE_TABLES <= _tables(db_path)
