"""Forced post mix: agent_run.intent schema migration test (tmp sqlite only)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

import deaddit
from deaddit import create_app

_MIGRATION_REVISION = "e9a3b1c4d7f2"
_PRE_MIGRATION_HEAD = "d6a4f8c2b901"


def _columns(db_path, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return {r[1] for r in rows}


def _heads() -> list[str]:
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        str(Path(deaddit.__file__).resolve().parent.parent / "migrations"),
    )
    script = ScriptDirectory.from_config(cfg)
    return script.get_heads()


def _revision_in_chain() -> bool:
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        str(Path(deaddit.__file__).resolve().parent.parent / "migrations"),
    )
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    if len(heads) != 1:
        return False
    return any(rev.revision == _MIGRATION_REVISION for rev in script.walk_revisions())


def test_single_head_and_linear_ancestry(tmp_path):
    db_path = tmp_path / "head.db"
    app = create_app(
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "TESTING": True}
    )
    runner = app.test_cli_runner()

    result = runner.invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output

    heads = _heads()
    assert len(heads) == 1, f"branched alembic heads: {heads}"
    assert _revision_in_chain(), (
        f"{_MIGRATION_REVISION} not in the ancestry of sole head {heads}"
    )


def test_upgrade_downgrade_round_trip(tmp_path):
    db_path = tmp_path / "mig.db"
    app = create_app(
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "TESTING": True}
    )
    runner = app.test_cli_runner()

    up = runner.invoke(args=["db", "upgrade"])
    assert up.exit_code == 0, up.output
    assert "intent" in _columns(db_path, "agent_run")

    down = runner.invoke(args=["db", "downgrade", _PRE_MIGRATION_HEAD])
    assert down.exit_code == 0, down.output
    assert "intent" not in _columns(db_path, "agent_run")

    up2 = runner.invoke(args=["db", "upgrade"])
    assert up2.exit_code == 0, up2.output
    assert "intent" in _columns(db_path, "agent_run")
