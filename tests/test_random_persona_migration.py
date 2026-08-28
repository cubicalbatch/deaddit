"""Model and migration coverage for random-persona agent storage.

The migration tests deliberately seed the pre-feature schema with sqlite3 because
its rows no longer match the current ORM models. The downgrade is documented as
*destructive*: random-only agents, their runs/turns/tool calls, memory with no
fixed owner, and numeric pins for those random agents are deleted because the
pre-feature schema cannot represent them.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError

import deaddit
from deaddit import create_app
from deaddit.agents.memory import backfill_persona_history
from deaddit.models import Agent, AgentMemory, AgentRun, User

_REVISION = "f4a8c2d6b901"
_PRE_FEATURE_HEAD = "e8b1f4c7a2d9"
_STAMP = "2026-08-27 12:00:00"


def _connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _columns(db_path, table: str) -> set[str]:
    conn = _connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _index_names(db_path, table: str) -> set[str]:
    conn = _connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}
    finally:
        conn.close()


def _column_not_null(db_path, table: str, column: str) -> bool:
    conn = _connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return next(item[3] for item in rows if item[1] == column) == 1
    finally:
        conn.close()


def _table_sql(db_path, table: str) -> str:
    conn = _connect(db_path)
    try:
        return conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()[0]
    finally:
        conn.close()


def _runner(tmp_path):
    db_path = tmp_path / "random-persona.db"
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


def _script_heads() -> list[str]:
    return _script_directory().get_heads()


def _seed_pre_feature_rows(db_path) -> None:
    """Seed representative rows while the database is still at the old head."""
    conn = _connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO user (username, bio, interests, created_at) VALUES (?, ?, ?, ?)",
            [
                ("alice", "Alice bio", '["history"]', _STAMP),
                ("bob", "Bob bio", '["science"]', _STAMP),
            ],
        )
        conn.execute(
            "INSERT INTO subdeaddit (name, description) VALUES (?, ?)", ("ask", "Ask")
        )
        conn.executemany(
            """
            INSERT INTO post
                (id, title, score, vote_count, content, subdeaddit_name, user,
                 created_at, model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    11,
                    "Alice post",
                    3,
                    1,
                    "History is fascinating.",
                    "ask",
                    "alice",
                    _STAMP,
                    "seed",
                ),
                (
                    12,
                    "Bob post",
                    2,
                    1,
                    "Science is useful.",
                    "ask",
                    "bob",
                    _STAMP,
                    "seed",
                ),
            ],
        )
        conn.execute(
            """
            INSERT INTO comment
                (id, post_id, content, score, vote_count, user, created_at, model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (21, 11, "Alice comment", 1, 1, "alice", _STAMP, "seed"),
        )
        conn.executemany(
            """
            INSERT INTO agent
                (id, user_username, autonomy_tier, is_enabled, status, config, state,
                 last_run_at, next_run_at, consecutive_failures)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    101,
                    "alice",
                    "regular",
                    1,
                    "idle",
                    "{}",
                    json.dumps(
                        {"subscriptions": ["sub1", "sub2"], "backoff_nudged": 1}
                    ),
                    None,
                    None,
                    0,
                ),
                (102, "bob", "lurker", 0, "idle", "{}", "{}", None, None, 0),
            ],
        )
        conn.executemany(
            """
            INSERT INTO agent_run
                (id, agent_id, trigger, status, started_at, finished_at, turn_count,
                 action_count, token_usage, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    201,
                    101,
                    "manual",
                    "completed",
                    _STAMP,
                    _STAMP,
                    1,
                    2,
                    '{"total": 4}',
                    None,
                ),
                (
                    202,
                    102,
                    "schedule",
                    "running",
                    _STAMP,
                    None,
                    0,
                    0,
                    None,
                    None,
                ),
            ],
        )
        conn.executemany(
            """
            INSERT INTO agent_memory (id, agent_id, kind, content, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (301, 101, "episode", "episode content", _STAMP),
                (302, 102, "backfill", "backfill content", _STAMP),
            ],
        )
        conn.execute(
            """
            INSERT INTO prompt_template (id, name, description, created_at)
            VALUES (1, 'daily', 'Daily prompt', ?)
            """,
            (_STAMP,),
        )
        conn.execute(
            """
            INSERT INTO prompt_template_version
                (id, template_id, version, body, created_by, created_at)
            VALUES (1, 1, 7, 'body-v7', 'test', ?)
            """,
            (_STAMP,),
        )
        conn.executemany(
            """
            INSERT INTO prompt_pin
                (id, target_kind, target_key, template_id, version_number, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (401, "agent", "alice", 1, 7, _STAMP),
                (402, "cohort", "quiet", 1, 7, _STAMP),
            ],
        )
        conn.execute(
            """
            INSERT INTO prompt_render_audit
                (id, created_at, template_id, template_version_id, subject_kind,
                 subject_key, rendered_sha256, variables_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (501, _STAMP, 1, 1, "agent", "alice", "a" * 64, '{"x":1}'),
        )
        conn.commit()
    finally:
        conn.close()


def test_upgrade_preserves_identity_and_enforces_schema(tmp_path):
    db_path, _app, runner = _runner(tmp_path)
    _upgrade(runner, _PRE_FEATURE_HEAD)
    _seed_pre_feature_rows(db_path)
    _upgrade(runner)

    conn = _connect(db_path)
    try:
        agents = conn.execute(
            "SELECT id, user_username, persona_mode, state FROM agent ORDER BY id"
        ).fetchall()
        assert [(row[0], row[1], row[2]) for row in agents] == [
            (101, "alice", "fixed"),
            (102, "bob", "fixed"),
        ]
        assert json.loads(agents[0][3]) == {"backoff_nudged": 1}
        assert json.loads(agents[1][3]) == {}

        users = conn.execute(
            "SELECT username, agent_state FROM user WHERE username IN ('alice', 'bob') "
            "ORDER BY username"
        ).fetchall()
        assert json.loads(users[0][1]) == {"subscriptions": ["sub1", "sub2"]}
        assert json.loads(users[1][1]) == {}

        runs = conn.execute(
            "SELECT id, agent_id, persona_username, status, trigger FROM agent_run ORDER BY id"
        ).fetchall()
        assert [(r[0], r[1], r[2], r[3], r[4]) for r in runs] == [
            (201, 101, "alice", "completed", "manual"),
            (202, 102, "bob", "running", "schedule"),
        ]
        memories = conn.execute(
            "SELECT id, user_username, kind, content FROM agent_memory ORDER BY id"
        ).fetchall()
        assert [(r[0], r[1], r[2], r[3]) for r in memories] == [
            (301, "alice", "episode", "episode content"),
            (302, "bob", "backfill", "backfill content"),
        ]
        assert tuple(
            conn.execute(
                "SELECT target_kind, target_key, template_id, version_number FROM prompt_pin "
                "WHERE id = 401"
            ).fetchone()
        ) == ("agent", "101", 1, 7)
        assert tuple(
            conn.execute(
                "SELECT target_kind, target_key FROM prompt_pin WHERE id = 402"
            ).fetchone()
        ) == ("cohort", "quiet")
        assert tuple(
            conn.execute("SELECT * FROM prompt_render_audit WHERE id = 501").fetchone()
        ) == (
            501,
            _STAMP,
            1,
            1,
            "agent",
            "alice",
            "a" * 64,
            '{"x":1}',
        )
    finally:
        conn.close()

    assert "agent_id" not in _columns(db_path, "agent_memory")
    assert "user_username" in _columns(db_path, "agent_memory")
    assert "persona_username" in _columns(db_path, "agent_run")
    assert {
        "ix_agent_run_persona_username",
        "uq_agent_run_running_persona",
    } <= _index_names(db_path, "agent_run")
    assert "ix_agent_memory_user_kind_created" in _index_names(db_path, "agent_memory")
    assert _column_not_null(db_path, "agent_run", "persona_username")
    assert _column_not_null(db_path, "agent_memory", "user_username")
    assert "ck_agent_persona_mode_user" in _table_sql(db_path, "agent")

    conn = _connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO agent
                    (id, persona_mode, user_username, autonomy_tier, is_enabled, status,
                     config, state, consecutive_failures)
                VALUES (601, 'fixed', NULL, 'regular', 0, 'idle', '{}', '{}', 0)
                """
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO agent
                    (id, persona_mode, user_username, autonomy_tier, is_enabled, status,
                     config, state, consecutive_failures)
                VALUES (602, 'random', 'alice', 'regular', 0, 'idle', '{}', '{}', 0)
                """
            )
        conn.rollback()
        conn.executemany(
            """
            INSERT INTO agent
                (id, persona_mode, user_username, autonomy_tier, is_enabled, status,
                 config, state, consecutive_failures)
            VALUES (?, 'random', NULL, 'regular', 0, 'idle', '{}', '{}', 0)
            """,
            [(603,), (604,)],
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO agent_run
                    (id, agent_id, persona_username, trigger, status, started_at,
                     turn_count, action_count)
                VALUES (605, 101, 'ghost', 'manual', 'completed', ?, 0, 0)
                """,
                (_STAMP,),
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO agent_run
                    (id, agent_id, persona_username, trigger, status, started_at,
                     turn_count, action_count)
                VALUES (606, 101, 'bob', 'manual', 'running', ?, 0, 0)
                """,
                (_STAMP,),
            )
        conn.rollback()
        conn.execute(
            """
            INSERT INTO agent_run
                (id, agent_id, persona_username, trigger, status, started_at,
                 turn_count, action_count)
            VALUES (607, 101, 'bob', 'manual', 'completed', ?, 0, 0)
            """,
            (_STAMP,),
        )
        conn.execute(
            """
            INSERT INTO agent_run
                (id, agent_id, persona_username, trigger, status, started_at,
                 turn_count, action_count)
            VALUES (608, 101, 'alice', 'manual', 'running', ?, 0, 0)
            """,
            (_STAMP,),
        )
        conn.commit()
    finally:
        conn.close()


def test_downgrade_destructively_removes_random_only_rows_and_round_trips(tmp_path):
    """Rollback deletes random agents and dependent data, then upgrades cleanly.

    This is intentionally destructive: random-agent runs/turns/tool calls,
    ownerless persona memory, and numeric pins for random-only personas cannot
    be represented by the old schema and are deleted during downgrade.
    """
    db_path, _app, runner = _runner(tmp_path)
    _upgrade(runner, _PRE_FEATURE_HEAD)
    _seed_pre_feature_rows(db_path)
    _upgrade(runner)

    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO user (username, bio) VALUES ('orphan', 'Orphan persona')"
        )
        conn.execute(
            """
            INSERT INTO agent
                (id, persona_mode, user_username, autonomy_tier, is_enabled, status,
                 config, state, consecutive_failures)
            VALUES (701, 'random', NULL, 'regular', 1, 'idle', '{}', '{}', 0)
            """
        )
        conn.execute(
            """
            INSERT INTO agent_run
                (id, agent_id, persona_username, trigger, status, started_at,
                 turn_count, action_count)
            VALUES (702, 701, 'orphan', 'manual', 'completed', ?, 1, 1)
            """,
            (_STAMP,),
        )
        conn.execute(
            """
            INSERT INTO agent_turn
                (id, run_id, seq, request_messages, response_message, model)
            VALUES (703, 702, 0, '[]', '{}', 'test')
            """
        )
        conn.execute(
            """
            INSERT INTO tool_call
                (id, turn_id, run_id, name, arguments, result, ok, created_at)
            VALUES (704, 703, 702, 'finish', '{}', '{}', 1, ?)
            """,
            (_STAMP,),
        )
        conn.execute(
            """
            INSERT INTO agent_memory (id, user_username, kind, content, created_at)
            VALUES (705, 'orphan', 'episode', 'orphan memory', ?)
            """,
            (_STAMP,),
        )
        conn.execute(
            """
            INSERT INTO prompt_pin
                (id, target_kind, target_key, template_id, version_number, updated_at)
            VALUES (706, 'agent', '701', 1, 7, ?)
            """,
            (_STAMP,),
        )
        conn.commit()
    finally:
        conn.close()

    _downgrade(runner, _PRE_FEATURE_HEAD)
    conn = _connect(db_path)
    try:
        assert [
            tuple(row)
            for row in conn.execute("SELECT id FROM agent ORDER BY id").fetchall()
        ] == [(101,), (102,)]
        assert [
            tuple(row)
            for row in conn.execute("SELECT id FROM agent_run ORDER BY id").fetchall()
        ] == [(201,), (202,)]
        assert conn.execute("SELECT id FROM agent_turn").fetchall() == []
        assert conn.execute("SELECT id FROM tool_call").fetchall() == []
        assert [
            tuple(row)
            for row in conn.execute(
                "SELECT id FROM agent_memory ORDER BY id"
            ).fetchall()
        ] == [(301,), (302,)]
        assert tuple(
            conn.execute(
                "SELECT target_kind, target_key FROM prompt_pin WHERE id = 401"
            ).fetchone()
        ) == ("agent", "alice")
        assert (
            conn.execute(
                "SELECT id FROM prompt_pin WHERE target_kind = 'agent' AND target_key GLOB '[0-9]*'"
            ).fetchall()
            == []
        )
        assert json.loads(
            conn.execute("SELECT state FROM agent WHERE id = 101").fetchone()[0]
        ) == {
            "backoff_nudged": 1,
            "subscriptions": ["sub1", "sub2"],
        }
    finally:
        conn.close()

    assert "persona_mode" not in _columns(db_path, "agent")
    assert "agent_state" not in _columns(db_path, "user")
    assert "user_username" not in _columns(db_path, "agent_memory")
    assert "agent_id" in _columns(db_path, "agent_memory")
    assert "ix_agent_memory_agent_id" in _index_names(db_path, "agent_memory")

    _upgrade(runner)
    conn = _connect(db_path)
    try:
        assert tuple(
            conn.execute("SELECT persona_mode FROM agent WHERE id = 101").fetchone()
        ) == ("fixed",)
        assert json.loads(
            conn.execute(
                "SELECT agent_state FROM user WHERE username = 'alice'"
            ).fetchone()[0]
        ) == {"subscriptions": ["sub1", "sub2"]}
        assert tuple(
            conn.execute(
                "SELECT persona_username FROM agent_run WHERE id = 201"
            ).fetchone()
        ) == ("alice",)
    finally:
        conn.close()


def test_model_constraints_and_persona_run_indexes(app, db_session, seeded_db):
    alice = seeded_db["users"][0]
    fixed = Agent(user_username=alice.username)
    db_session.add(fixed)
    db_session.commit()

    bad_fixed = Agent(persona_mode="fixed", user_username=None)
    db_session.add(bad_fixed)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()

    bad_random = Agent(persona_mode="random", user_username=alice.username)
    db_session.add(bad_random)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()

    db_session.add_all([Agent(persona_mode="random"), Agent(persona_mode="random")])
    db_session.flush()

    first = AgentRun(
        agent_id=fixed.id,
        persona_username=alice.username,
        trigger="manual",
        status="running",
        started_at=datetime(2026, 1, 1, 12),
    )
    db_session.add(first)
    db_session.flush()
    duplicate = AgentRun(
        agent_id=fixed.id,
        persona_username=alice.username,
        trigger="schedule",
        status="running",
        started_at=datetime(2026, 1, 1, 13),
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()

    completed = AgentRun(
        agent_id=fixed.id,
        persona_username=alice.username,
        trigger="schedule",
        status="completed",
        started_at=datetime(2026, 1, 1, 14),
    )
    different_persona = AgentRun(
        agent_id=fixed.id,
        persona_username=seeded_db["users"][1].username,
        trigger="manual",
        status="running",
        started_at=datetime(2026, 1, 1, 15),
    )
    db_session.add_all([completed, different_persona])
    db_session.flush()
    assert completed.id and different_persona.id
    assert db_session.get(User, different_persona.persona_username).username == "bob"
    db_session.rollback()


def test_backfill_history_uses_user_key_and_extractive_fallback(
    app, db_session, seeded_db
):
    with pytest.raises(ValueError, match="No such user 'missing'"):
        backfill_persona_history("missing")

    inserted = backfill_persona_history("alice", api_url=None, model=None)
    assert inserted == 1
    rows = AgentMemory.query.filter_by(user_username="alice", kind="backfill").all()
    assert len(rows) == 1
    assert rows[0].content.startswith("History (before becoming an agent): [1/1] ")
    assert "Extracted summary: wrote 2 post(s) and 1 comment(s)" in rows[0].content
    assert "Hello World" in rows[0].content
    assert backfill_persona_history("alice", api_url=None, model=None) == 0
    assert (
        AgentMemory.query.filter_by(user_username="alice", kind="backfill").count() == 1
    )


def test_random_persona_revision_is_single_head():
    """``_REVISION`` sits in the ancestry of the sole head.

    Later migrations (e.g. the 2.1 ``generated_website`` table) chain past
    this revision rather than branch from it, so this asserts ancestry
    membership instead of exact head equality - see the same pattern in
    test_d4_migration.py's ``_d4_in_chain``.
    """
    heads = _script_heads()
    assert len(heads) == 1, f"branched alembic heads: {heads}"
    ancestry = {rev.revision for rev in _script_directory().walk_revisions()}
    assert _REVISION in ancestry, f"{_REVISION} not in ancestry of sole head {heads}"
