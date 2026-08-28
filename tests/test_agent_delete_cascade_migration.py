"""Upgrade and downgrade coverage for agent runtime delete cascades."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

import deaddit
from deaddit import create_app

_REVISION = "2b7c9e4d1f06"
_PRE_FEATURE_HEAD = "01fe7be10643"


def _connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _runner(tmp_path):
    db_path = tmp_path / "agent-cascades.db"
    app = create_app(
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "TESTING": True}
    )
    return db_path, app, app.test_cli_runner()


def _upgrade(runner, revision: str | None = None) -> None:
    args = ["db", "upgrade"] + ([revision] if revision else [])
    result = runner.invoke(args=args)
    assert result.exit_code == 0, result.output


def _downgrade(runner, revision: str) -> None:
    result = runner.invoke(args=["db", "downgrade", revision])
    assert result.exit_code == 0, result.output


def _script_directory() -> ScriptDirectory:
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(deaddit.__file__).resolve().parent.parent / "migrations"),
    )
    return ScriptDirectory.from_config(config)


def _seed_rows(db_path) -> None:
    conn = _connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO user (username, bio, interests, created_at) VALUES (?, ?, ?, ?)",
            [
                ("owner-one", "", "[]", "2026-01-01"),
                ("persona-one", "", "[]", "2026-01-01"),
                ("owner-two", "", "[]", "2026-01-01"),
                ("persona-two", "", "[]", "2026-01-01"),
            ],
        )
        conn.execute(
            "INSERT INTO subdeaddit (name, description) VALUES (?, ?)",
            ("cascade", "Cascade tests"),
        )
        conn.executemany(
            """
            INSERT INTO post
                (id, title, score, vote_count, content, subdeaddit_name, user,
                 created_at, model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "First", 0, 0, "one", "cascade", "owner-one", "2026-01-01", "test"),
                (
                    2,
                    "Second",
                    0,
                    0,
                    "two",
                    "cascade",
                    "owner-two",
                    "2026-01-01",
                    "test",
                ),
            ],
        )
        conn.executemany(
            """
            INSERT INTO agent
                (id, persona_mode, user_username, autonomy_tier, is_enabled, status,
                 config, state, consecutive_failures)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "fixed", "owner-one", "regular", 0, "idle", "{}", "{}", 0),
                (10, "fixed", "owner-two", "regular", 0, "idle", "{}", "{}", 0),
            ],
        )
        conn.executemany(
            """
            INSERT INTO agent_run
                (id, agent_id, persona_username, trigger, status, started_at,
                 turn_count, action_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (2, 1, "persona-one", "manual", "completed", "2026-01-01", 1, 1),
                (20, 10, "persona-two", "manual", "completed", "2026-01-01", 1, 1),
            ],
        )
        conn.execute(
            """
            INSERT INTO agent_turn
                (id, run_id, seq, request_messages, response_message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (3, 2, 0, "{}", "{}"),
        )
        conn.execute(
            """
            INSERT INTO agent_turn
                (id, run_id, seq, request_messages, response_message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (30, 20, 0, "{}", "{}"),
        )
        conn.executemany(
            """
            INSERT INTO tool_call (id, turn_id, run_id, name, ok)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(4, 3, 2, "finish", 1), (40, 30, 20, "finish", 1)],
        )
        conn.executemany(
            """
            INSERT INTO agent_memory (id, user_username, kind, content, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (5, "persona-one", "episode", "one", "2026-01-01"),
                (50, "persona-two", "episode", "two", "2026-01-01"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO generated_website
                (id, post_id, public_path, storage_path, hostname, page_name,
                 source_description, byte_size, sha256, agent_id,
                 creator_username_snapshot, agent_run_id, api_url_snapshot,
                 model_snapshot)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    6,
                    1,
                    "one.example.test/one.html",
                    "pages/one.html",
                    "one.example.test",
                    "one.html",
                    "one",
                    1,
                    "a" * 64,
                    1,
                    "persona-one",
                    2,
                    "https://example.test/v1",
                    "test-model",
                ),
                (
                    60,
                    2,
                    "two.example.test/two.html",
                    "pages/two.html",
                    "two.example.test",
                    "two.html",
                    "two",
                    1,
                    "b" * 64,
                    10,
                    "persona-two",
                    20,
                    "https://example.test/v1",
                    "test-model",
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _foreign_key_actions(db_path, table: str) -> dict[tuple[str, str], str]:
    conn = _connect(db_path)
    try:
        return {
            (row["from"], row["table"]): row["on_delete"]
            for row in conn.execute(f"PRAGMA foreign_key_list({table})")
        }
    finally:
        conn.close()


def test_upgrade_cascades_runtime_rows_and_downgrade_restores_schema(tmp_path):
    db_path, _app, runner = _runner(tmp_path)
    _upgrade(runner, _PRE_FEATURE_HEAD)
    _seed_rows(db_path)
    _upgrade(runner)

    assert _foreign_key_actions(db_path, "agent") == {
        ("user_username", "user"): "CASCADE"
    }
    assert _foreign_key_actions(db_path, "agent_run") == {
        ("agent_id", "agent"): "CASCADE",
        ("persona_username", "user"): "CASCADE",
    }
    assert _foreign_key_actions(db_path, "agent_turn") == {
        ("run_id", "agent_run"): "CASCADE"
    }
    assert _foreign_key_actions(db_path, "tool_call") == {
        ("turn_id", "agent_turn"): "CASCADE",
        ("run_id", "agent_run"): "CASCADE",
    }
    assert _foreign_key_actions(db_path, "agent_memory") == {
        ("user_username", "user"): "CASCADE"
    }

    conn = _connect(db_path)
    try:
        index_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'uq_agent_run_running_persona'"
        ).fetchone()[0]
        assert "UNIQUE INDEX" in index_sql
        assert "WHERE status = 'running'" in index_sql

        conn.execute(
            "INSERT INTO agent_run "
            "(id, agent_id, persona_username, trigger, status, started_at, "
            "turn_count, action_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (21, 10, "persona-two", "manual", "running", "2026-01-01", 0, 0),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO agent_run "
                "(id, agent_id, persona_username, trigger, status, started_at, "
                "turn_count, action_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (22, 10, "persona-two", "manual", "running", "2026-01-01", 0, 0),
            )
        conn.rollback()
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM agent WHERE id = 1")
        assert (
            conn.execute("SELECT count(*) FROM agent_run WHERE id = 2").fetchone()[0]
            == 0
        )

        assert (
            conn.execute("SELECT count(*) FROM agent_turn WHERE id = 3").fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT count(*) FROM tool_call WHERE id = 4").fetchone()[0]
            == 0
        )
        assert tuple(
            conn.execute(
                "SELECT agent_id, agent_run_id FROM generated_website WHERE id = 6"
            ).fetchone()
        ) == (None, None)
        assert (
            conn.execute("SELECT count(*) FROM agent_memory WHERE id = 5").fetchone()[0]
            == 1
        )
        conn.execute("DELETE FROM user WHERE username = 'persona-one'")
        assert (
            conn.execute("SELECT count(*) FROM agent_memory WHERE id = 5").fetchone()[0]
            == 0
        )
        conn.commit()
    finally:
        conn.close()

    _downgrade(runner, _PRE_FEATURE_HEAD)
    assert _foreign_key_actions(db_path, "agent") == {
        ("user_username", "user"): "NO ACTION"
    }
    assert _foreign_key_actions(db_path, "agent_run") == {
        ("agent_id", "agent"): "NO ACTION",
        ("persona_username", "user"): "NO ACTION",
    }
    assert _foreign_key_actions(db_path, "agent_turn") == {
        ("run_id", "agent_run"): "NO ACTION"
    }
    assert _foreign_key_actions(db_path, "tool_call") == {
        ("turn_id", "agent_turn"): "NO ACTION",
        ("run_id", "agent_run"): "NO ACTION",
    }
    assert _foreign_key_actions(db_path, "agent_memory") == {
        ("user_username", "user"): "NO ACTION"
    }

    conn = _connect(db_path)
    try:
        assert (
            conn.execute("SELECT count(*) FROM agent WHERE id = 10").fetchone()[0] == 1
        )
        assert (
            conn.execute("SELECT count(*) FROM agent_run WHERE id = 20").fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT count(*) FROM agent_run WHERE id = 21").fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT count(*) FROM agent_turn WHERE id = 30").fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT count(*) FROM tool_call WHERE id = 40").fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT count(*) FROM agent_memory WHERE id = 50").fetchone()[
                0
            ]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM generated_website WHERE id = 60"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_agent_cascade_revision_is_sole_head():
    script = _script_directory()
    heads = script.get_heads()
    assert heads == [_REVISION]
    ancestry = {revision.revision for revision in script.walk_revisions()}
    assert _PRE_FEATURE_HEAD in ancestry
