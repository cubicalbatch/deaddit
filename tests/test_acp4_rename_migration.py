"""Resolution 4 slice 2: upvote_count -> score collapse migration test.

Builds a sqlite file DB at the pre-rename schema (alembic upgrade to
b2d4f6a8c0e1), seeds rows covering every displayed-value branch of the old
``CASE WHEN vote_count > 0 THEN score ELSE COALESCE(upvote_count, 0) END``,
then upgrades to head and asserts the single ``score`` column carries the
old displayed value exactly.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

import deaddit
from deaddit import create_app
from deaddit.dynamics.ranking import HOT_SQL_FRAGMENT

_RENAME_REVISION = "c7e2a9b4d1f6"
_PRE_RENAME_HEAD = "b2d4f6a8c0e1"


def _connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(db_path, table: str) -> set[str]:
    conn = _connect(db_path)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _index_names(db_path, table: str) -> set[str]:
    conn = _connect(db_path)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA index_list({table})")}
    finally:
        conn.close()


def _runner(tmp_path):
    db_path = tmp_path / "rename.db"
    app = create_app(
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "TESTING": True}
    )
    return db_path, app.test_cli_runner()


def _upgrade(runner, revision: str | None = None):
    args = ["db", "upgrade"] + ([revision] if revision else [])
    result = runner.invoke(args=args)
    assert result.exit_code == 0, result.output


def _downgrade(runner, revision: str):
    result = runner.invoke(args=["db", "downgrade", revision])
    assert result.exit_code == 0, result.output


# (title/content marker, vote_count, score, upvote_count, expected display).
# Row (a) is vote-backed with a synced alias; (b) is the fabricated
# protected-item shape; (c) exercises COALESCE over a genuine NULL;
# (d) is a model='seed' row.
_POST_ROWS = [
    ("votedown", 3, -2, -2, -2),
    ("fabricated", 0, 0, 47, 47),
    ("zeroed", 0, 0, None, 0),
    ("seedfab", 0, 0, 12, 12),
]

_COMMENT_ROWS = [
    ("cvotedown", 2, -1, -1, -1),
    ("cfabricated", 0, 0, 9, 9),
]


def _seed(db_path) -> None:
    stamp = datetime(2026, 1, 1, 12, 0, 0).isoformat(sep=" ")
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO user (username) VALUES ('u0'), ('u1'), ('u2'), ('u3')"
        )
        conn.execute(
            "INSERT INTO subdeaddit (name, description) VALUES ('s', 'd')"
        )
        for _i, (marker, vote_count, score, upvote_count, _exp) in enumerate(
            _POST_ROWS
        ):
            conn.execute(
                "INSERT INTO post (title, content, subdeaddit_name, user,"
                " created_at, model, vote_count, score, upvote_count)"
                " VALUES (?, 'c', 's', 'u0', ?, ?, ?, ?, ?)",
                (
                    marker,
                    stamp,
                    "seed" if marker == "seedfab" else "legacy-model",
                    vote_count,
                    score,
                    upvote_count,
                ),
            )
        post_ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM post ORDER BY id"
            ).fetchall()
        ]
        for i, (marker, vote_count, score, upvote_count, _exp) in enumerate(
            _COMMENT_ROWS
        ):
            conn.execute(
                "INSERT INTO comment (post_id, content, user, created_at,"
                " model, vote_count, score, upvote_count)"
                " VALUES (?, ?, 'u1', ?, 'legacy-model', ?, ?, ?)",
                (post_ids[i], marker, stamp, vote_count, score, upvote_count),
            )
        conn.commit()
    finally:
        conn.close()


def _scores_by_marker(db_path, table: str) -> dict[str, int]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT {'content' if table == 'comment' else 'title'} AS marker,"
            f" score FROM {table}"
        ).fetchall()
        return {r["marker"]: r["score"] for r in rows}
    finally:
        conn.close()


def test_upgrade_preserves_displayed_values_exactly(tmp_path):
    db_path, runner = _runner(tmp_path)
    _upgrade(runner, _PRE_RENAME_HEAD)
    _seed(db_path)

    _upgrade(runner)

    expected_posts = {m: exp for m, _, _, _, exp in _POST_ROWS}
    expected_comments = {m: exp for m, _, _, _, exp in _COMMENT_ROWS}
    assert _scores_by_marker(db_path, "post") == expected_posts
    assert _scores_by_marker(db_path, "comment") == expected_comments

    for table in ("post", "comment"):
        assert "upvote_count" not in _columns(db_path, table), table
    assert "ix_comment_upvote_count" not in _index_names(db_path, "comment")
    assert "ix_comment_score" in _index_names(db_path, "comment")


def test_hot_query_plan_uses_expression_index_after_upgrade(tmp_path):
    db_path, runner = _runner(tmp_path)
    _upgrade(runner, _PRE_RENAME_HEAD)
    _seed(db_path)
    _upgrade(runner)

    # batch_alter_table recreates the table on SQLite; recreate the D2
    # expression index byte-identically if the batch dropped it (mirrors
    # tests/test_d2_feeds.py::ranking_indexes).
    conn = _connect(db_path)
    try:
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        if "ix_post_hot_expr" not in names:
            conn.execute(
                f"CREATE INDEX ix_post_hot_expr ON post (({HOT_SQL_FRAGMENT}))"
            )
            conn.commit()
        plan = conn.execute(
            f"EXPLAIN QUERY PLAN SELECT title FROM post"
            f" ORDER BY {HOT_SQL_FRAGMENT} DESC"
        ).fetchall()
    finally:
        conn.close()

    detail = " ".join(str(tuple(r)) for r in plan)
    assert "USING INDEX ix_post_hot_expr" in detail


def test_downgrade_restores_display_truth_not_provenance(tmp_path):
    db_path, runner = _runner(tmp_path)
    _upgrade(runner, _PRE_RENAME_HEAD)
    _seed(db_path)
    _upgrade(runner)
    _downgrade(runner, _PRE_RENAME_HEAD)

    for table in ("post", "comment"):
        assert "upvote_count" in _columns(db_path, table), table
    assert "ix_comment_upvote_count" in _index_names(db_path, "comment")
    assert "ix_comment_score" not in _index_names(db_path, "comment")

    # Downgrade restores the display value into upvote_count everywhere --
    # but provenance is lost: fabricated and vote-backed numbers are now
    # indistinguishable (both equal score).
    conn = _connect(db_path)
    try:
        for table, key in (("post", "title"), ("comment", "content")):
            rows = conn.execute(
                f"SELECT {key} AS marker, score, upvote_count FROM {table}"
            ).fetchall()
            for row in rows:
                assert row["upvote_count"] == row["score"], (
                    table,
                    row["marker"],
                )
    finally:
        conn.close()


def test_single_linear_head_includes_rename():
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        str(Path(deaddit.__file__).resolve().parent.parent / "migrations"),
    )
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert heads == [_RENAME_REVISION], heads
