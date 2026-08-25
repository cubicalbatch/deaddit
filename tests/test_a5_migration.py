"""Phase A5 slice S4: job lease columns migration test (tmp sqlite only)."""

from __future__ import annotations

import sqlite3

from deaddit import create_app

_LEASE_COLUMNS = {"claimed_at", "worker_id", "heartbeat_at"}

# down_revision of the A5 lease-columns revision.
_PRE_A5_HEAD = "c8f2a4e61b9d"


def _job_columns(db_path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("PRAGMA table_info(job)").fetchall()
    finally:
        conn.close()
    return {r[1] for r in rows}


def test_job_lease_columns_round_trip(tmp_path):
    db_path = tmp_path / "mig.db"
    app = create_app(
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "TESTING": True}
    )
    runner = app.test_cli_runner()

    result = runner.invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output
    assert _LEASE_COLUMNS <= _job_columns(db_path)

    # One step back to the pre-A5 head removes the lease columns.
    down = runner.invoke(args=["db", "downgrade", _PRE_A5_HEAD])
    assert down.exit_code == 0, down.output
    assert not (_LEASE_COLUMNS & _job_columns(db_path))

    # Forward again restores them.
    up = runner.invoke(args=["db", "upgrade"])
    assert up.exit_code == 0, up.output
    assert _LEASE_COLUMNS <= _job_columns(db_path)
