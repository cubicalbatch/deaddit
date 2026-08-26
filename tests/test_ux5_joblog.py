"""Phase UX-5 slice Wave-C: worker-side job log capture.

Covers the JobLogHandler in deaddit.runtime.joblog and the job_log
migration (single head, up/down round trip). The former admin consumers
(socket tailer, HTTP fallback endpoint) went with the /admin/jobs UI.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory

import deaddit
from deaddit import db as _db
from deaddit.models import JobLog, JobStatus, JobType

_UX5_REVISION = "a9c1e5f7b3d2"
_PRE_UX5_HEAD = "b8e2f4a6c9d1"


def _make_job():
    from deaddit.jobs import create_job

    return create_job(
        job_type=JobType.BATCH_OPERATION,
        parameters={"operations": []},
        priority=5,
        total_items=1,
    )


# ---------------------------------------------------------------------------
# (a) Worker-side capture inside execute_job
# ---------------------------------------------------------------------------


def test_execute_job_captures_log_lines(app, monkeypatch):
    """Lines logged via the 'deaddit' hierarchy become JobLog rows."""
    from deaddit import jobs

    def fake_batch(job):
        log = logging.getLogger("deaddit.jobs")
        for i in range(12):
            log.info("synthetic line %d", i)
            log.warning("synthetic warning %d", i)

        return {}

    monkeypatch.setattr(jobs, "_execute_batch_operation", fake_batch)

    with app.app_context():
        job = _make_job()
        jobs.execute_job(job.id, app=app)
        # execute_job runs in its own nested app context => its own
        # scoped session; refresh our outer-context objects from the DB.
        _db.session.expire_all()

        assert job.status == JobStatus.COMPLETED
        rows = (
            JobLog.query.filter_by(job_id=job.id).order_by(JobLog.seq).all()
        )
        # 24 synthetic lines + execute_job's own bookkeeping lines.
        assert len(rows) >= 24
        assert any("synthetic line 0" in r.message for r in rows)
        seqs = [r.seq for r in rows]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_job_log_capped_at_500_per_job(app, monkeypatch):
    """Flooded jobs keep at most MAX_JOB_LOG_LINES rows (oldest trimmed)."""
    from deaddit import jobs
    from deaddit.runtime.joblog import MAX_JOB_LOG_LINES

    def flooding_batch(job):
        log = logging.getLogger("deaddit.jobs")
        for i in range(600):
            log.info("flood %04d", i)

        return {}

    monkeypatch.setattr(jobs, "_execute_batch_operation", flooding_batch)

    with app.app_context():
        job = _make_job()
        jobs.execute_job(job.id, app=app)

        rows = JobLog.query.filter_by(job_id=job.id).order_by(JobLog.seq).all()
        assert len(rows) == MAX_JOB_LOG_LINES
        seqs = [r.seq for r in rows]
        assert seqs == list(range(seqs[0], seqs[0] + MAX_JOB_LOG_LINES))


def test_log_write_failure_never_breaks_job(app, monkeypatch):
    """If JobLog persistence fails the job still completes."""
    from unittest.mock import patch

    from deaddit import jobs

    def working_batch(job):
        logging.getLogger("deaddit.jobs").info("before broken flush")

        return {}

    monkeypatch.setattr(jobs, "_execute_batch_operation", working_batch)

    def boom_write(self, batch):
        raise RuntimeError("disk full")

    with app.app_context():
        job = _make_job()

        with patch(
            "deaddit.runtime.joblog.JobLogHandler._write_batch", boom_write
        ):
            jobs.execute_job(job.id, app=app)
            _db.session.expire_all()

            assert job.status == JobStatus.COMPLETED


# ---------------------------------------------------------------------------
# (b) Migration: single head, upgrade/downgrade on a copy DB
# ---------------------------------------------------------------------------


def _script_heads() -> list[str]:
    cfg = AlembicConfig()
    cfg.set_main_option(
        "script_location",
        str(Path(deaddit.__file__).resolve().parent.parent / "migrations"),
    )
    return ScriptDirectory.from_config(cfg).get_heads()


def _table_names(db_path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _index_names(db_path, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
            (table,),
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def test_single_alembic_head():
    # UX-5 pins the DISCIPLINE (exactly one head), not the tip identity —
    # later lanes legitimately chain on top of a9c1e5f7b3d2.
    assert len(_script_heads()) == 1


def test_migration_upgrade_downgrade_round_trip(tmp_path):
    from deaddit import create_app

    db_path = tmp_path / "ux5mig.db"

    # Upgrade only up to the PRE-UX-5 head first (simulates an existing DB).
    pre_app = create_app(
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "TESTING": True}
    )
    runner = pre_app.test_cli_runner()
    up_to_pre = runner.invoke(args=["db", "upgrade", _PRE_UX5_HEAD])
    assert up_to_pre.exit_code == 0, up_to_pre.output
    assert "job_log" not in _table_names(db_path)

    # Forward across UX-5: table + index present at our revision.
    up = runner.invoke(args=["db", "upgrade", _UX5_REVISION])
    assert up.exit_code == 0, up.output
    assert "job_log" in _table_names(db_path)
    assert {"ix_job_log_job_seq"} <= _index_names(db_path, "job_log")

    # One step back removes exactly what UX-5 added.
    down = runner.invoke(args=["db", "downgrade", _PRE_UX5_HEAD])
    assert down.exit_code == 0, down.output
    assert "job_log" not in _table_names(db_path)


