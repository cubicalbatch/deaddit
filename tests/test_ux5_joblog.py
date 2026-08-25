"""Phase UX-5 slice Wave-C: broker-free streamed job logs.

Covers worker-side capture (JobLogHandler in deaddit.runtime.joblog), the
HTTP fallback endpoint, the web-side tailer emitting over the flask_socketio
test client, and the job_log migration (single head, up/down round trip).
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
from deaddit.runtime.tailer import get_tailer, reset_tailer

_UX5_REVISION = "a9c1e5f7b3d2"
_PRE_UX5_HEAD = "b8e2f4a6c9d1"


@pytest.fixture(autouse=True)
def _fresh_tailer():
    """No tailer thread leaks between tests."""
    reset_tailer()
    yield
    reset_tailer()


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
# (b) HTTP fallback endpoint
# ---------------------------------------------------------------------------


def test_job_log_api_cursor_and_404(client, app):
    with app.app_context():
        from deaddit.jobs import create_job

        job = create_job(JobType.CREATE_POST, {"count": 1})
        for i in range(1, 6):
            _db.session.add(
                JobLog(job_id=job.id, seq=i, level="INFO", message=f"line {i}")
            )
        _db.session.commit()
        job_id = job.id

    resp = client.get(f"/admin/api/jobs/{job_id}/log?after=2")
    assert resp.status_code == 200
    data = resp.get_json()
    assert [ln["seq"] for ln in data["lines"]] == [3, 4, 5]
    assert data["last_seq"] == 5
    assert data["job_id"] == job_id
    assert data["status"] == "pending"
    assert isinstance(data["progress"], int)

    # Empty tail keeps the cursor stable.
    resp2 = client.get(f"/admin/api/jobs/{job_id}/log?after=99")
    assert resp2.status_code == 200
    assert resp2.get_json()["lines"] == []
    assert resp2.get_json()["last_seq"] == 99

    assert client.get("/admin/api/jobs/999999/log").status_code == 404

def _register_ux5_handlers():
    """Bind the real websocket.py handlers onto the CURRENT SocketIO server.

    flask_socketio mints a fresh bare Server per create_app(); the import-time
    decorators only bound to the first one (same convention as
    test_llm_stream_admin.py).
    """
    from deaddit import websocket as ws_mod
    from deaddit.extensions import socketio

    socketio.on("join_job_log", namespace="/admin")(ws_mod.join_job_log)
    socketio.on("leave_job_log", namespace="/admin")(ws_mod.leave_job_log)


# ---------------------------------------------------------------------------
# (c) Web-side tailer over the socket test client
# ---------------------------------------------------------------------------


def test_tailer_emits_job_log_to_joined_room(app):
    from deaddit.extensions import socketio

    _register_ux5_handlers()

    with app.app_context():
        job = _make_job()

        client = socketio.test_client(app, namespace="/admin")
        try:
            client.emit("join_job_log", {"job_id": job.id}, namespace="/admin")

            received = client.get_received(namespace="/admin")
            ready = [m for m in received if m["name"] == "job_log_ready"]
            assert ready and ready[0]["args"][0]["job_id"] == job.id

            # New rows after the join: one manual tick must deliver them.
            _db.session.add_all(
                [
                    JobLog(job_id=job.id, seq=1, level="INFO", message="hello"),
                    JobLog(
                        job_id=job.id,
                        seq=2,
                        level="WARNING",
                        message="careful",
                    ),
                ]
            )
            _db.session.commit()

            assert get_tailer().tick() is True

            events = client.get_received(namespace="/admin")
            names = [m["name"] for m in events]
            assert "job_log" in names
            payload = next(m for m in events if m["name"] == "job_log")[
                "args"
            ][0]
            assert payload["job_id"] == job.id
            assert [ln["seq"] for ln in payload["lines"]] == [1, 2]

            # Idempotent: a second tick with no new rows emits nothing new.
            assert get_tailer().tick() is True
            fresh = client.get_received(namespace="/admin")
            assert not [m for m in fresh if m["name"] == "job_log"]
        finally:
            client.disconnect(namespace="/admin")


def test_join_job_log_starts_tailer_thread(app):
    from deaddit.extensions import socketio

    _register_ux5_handlers()

    with app.app_context():
        job = _make_job()

        client = socketio.test_client(app, namespace="/admin")
        try:
            client.emit("join_job_log", {"job_id": job.id}, namespace="/admin")
            assert get_tailer().running is True
        finally:
            client.disconnect(namespace="/admin")


# ---------------------------------------------------------------------------
# (d) Migration: single head, upgrade/downgrade on a copy DB
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


# ---------------------------------------------------------------------------
# (e) Generate page: LLM playground (legacy generation forms removed AC-P4)
# ---------------------------------------------------------------------------


def test_generate_page_is_llm_playground(client):
    resp = client.get("/admin/generate")
    assert resp.status_code == 200
    html = resp.data.decode()
    # No legacy content-generation form remains.
    assert "<form" not in html
    assert 'id="task-type"' not in html
    # LLM-4 streaming mount point and model datalist feed survive.
    assert "llm-stream-card" in html
    assert "/api/available_models" in html


