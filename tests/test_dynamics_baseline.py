"""Phase 0 baseline report contracts and read-only CLI behavior."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from click.testing import CliRunner

from deaddit.cli import cli
from deaddit.dynamics.baseline import compute_report

_SCHEMA = """
CREATE TABLE agent_run (
    id INTEGER PRIMARY KEY, status TEXT, persona_username TEXT, token_usage TEXT,
    started_at TEXT, finished_at TEXT
);
CREATE TABLE tool_call (
    id INTEGER PRIMARY KEY, run_id INTEGER, name TEXT, arguments TEXT,
    ok INTEGER, created_at TEXT
);
CREATE TABLE llm_usage (id INTEGER PRIMARY KEY, created_at TEXT);
CREATE TABLE post (
    id INTEGER PRIMARY KEY, title TEXT, user TEXT, subdeaddit_name TEXT,
    score INTEGER, vote_count INTEGER, created_at TEXT, removed INTEGER
);
CREATE TABLE comment (
    id INTEGER PRIMARY KEY, post_id INTEGER, user TEXT, score INTEGER,
    vote_count INTEGER, created_at TEXT, removed INTEGER
);
CREATE TABLE vote (
    id INTEGER PRIMARY KEY, voter TEXT, post_id INTEGER, comment_id INTEGER,
    value INTEGER, source TEXT, created_at TEXT
);
"""


def _build_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO post VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "old", "bob", "python", 2, 1, "2026-08-28 09:00:00", 0),
            (2, "new", "carol", "python", 0, 0, "2026-08-28 12:00:00", 0),
            (3, "removed", "carol", "other", 99, 1, "2026-08-28 13:00:00", 1),
        ],
    )
    conn.executemany(
        "INSERT INTO comment VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, 1, "carol", -1, 1, "2026-08-28 10:00:00", 0),
            (2, 2, "bob", 0, 0, "2026-08-28 12:30:00", 0),
        ],
    )
    conn.executemany(
        "INSERT INTO agent_run VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                1,
                "completed",
                "alice",
                '{"prompt_tokens": 8, "completion_tokens": 2}',
                "2026-08-28 09:00:00",
                "2026-08-28 09:02:00",
            ),
            (
                2,
                "completed",
                "alice",
                '{"total_tokens": 20}',
                "2026-08-28 10:00:00",
                "2026-08-28 10:03:00",
            ),
            (
                3,
                "completed",
                "bob",
                '{"prompt": 30}',
                "2026-08-28 11:00:00",
                "2026-08-28 11:01:00",
            ),
            (
                4,
                "failed",
                "carol",
                '{"total_tokens": 40}',
                "2026-08-28 12:00:00",
                "2026-08-28 12:01:00",
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO tool_call VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, 1, "create_post", "{}", 1, "2026-08-28 09:01:00"),
            (
                2,
                2,
                "vote",
                '{"target_type":"post","target_id":1,"direction":1}',
                1,
                "2026-08-28 10:01:00",
            ),
            (
                3,
                2,
                "vote",
                '{"target_type":"post","target_id":1,"direction":1}',
                1,
                "2026-08-28 10:02:00",
            ),
            (4, 3, "browse_feed", "{}", 1, "2026-08-28 11:01:00"),
            (
                5,
                4,
                "vote",
                '{"target_type":"comment","target_id":1,"direction":-1}',
                0,
                "2026-08-28 12:01:00",
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO vote VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "alice", 1, None, 1, "agent", "2026-08-28 10:01:00"),
            (2, "bob", None, 1, -1, "human", "2026-08-28 11:00:00"),
        ],
    )
    conn.commit()
    conn.close()


def test_report_classifies_runs_and_exposes_vote_baseline(tmp_path):
    path = tmp_path / "baseline.db"
    _build_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    report = compute_report(conn, top_k=2)
    conn.close()

    buckets = report["runs"]["buckets"]
    assert buckets["content-producing"]["runs"] == 1
    assert buckets["content-producing"]["total_tokens"] == 10
    assert buckets["vote-only"]["runs"] == 1
    assert buckets["browse-only"]["runs"] == 1
    assert buckets["failed"]["runs"] == 1
    assert report["votes"]["tool_attempts"] == 3
    assert report["votes"]["successful_tool_calls"] == 2
    assert report["votes"]["durable_vote_rows"] == 2
    assert report["votes"]["durable_voters"] == 2
    assert report["votes"]["direction_split"] == {"up": 1, "down": 1}
    assert report["votes"]["source_split"] == {"agent": 1, "human": 1}
    assert report["votes"]["collision_call_count"] == 1
    assert report["distributions"]["posts"]["zero_vote_fraction"] == 0.666667
    assert report["distributions"]["comments"]["zero_vote_fraction"] == 0.5
    assert report["distributions"]["vote_arrival_latency_seconds"]["median"] == 3630
    assert [item["post_id"] for item in report["hot_feed"]] == [1, 2]
    assert (
        report["distributions"]["community_concentration"]["top"][0]["key"] == "python"
    )


def test_baseline_cli_is_identical_and_does_not_write(tmp_path):
    path = tmp_path / "baseline.db"
    _build_db(path)
    before = hashlib.sha256(path.read_bytes()).digest()
    runner = CliRunner()
    args = ["dynamics", "baseline-report", "--db", str(path), "--json"]
    first = runner.invoke(cli, args)
    second = runner.invoke(cli, args)
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert first.output == second.output
    assert json.loads(first.output)["schema_version"] == 1
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_baseline_cli_rejects_invalid_as_of(tmp_path):
    path = tmp_path / "baseline.db"
    _build_db(path)
    result = CliRunner().invoke(
        cli,
        [
            "dynamics",
            "baseline-report",
            "--db",
            str(path),
            "--as-of",
            "not-a-date",
        ],
    )
    assert result.exit_code != 0
    assert "ISO 8601" in result.output
