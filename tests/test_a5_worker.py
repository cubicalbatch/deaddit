"""Phase A5 slice S4: dedicated worker process contract tests.

Covers the claim/heartbeat/sweep contract (deaddit.runtime.claim), the
JobRunner execution loop (deaddit.runtime.runner), the nightly registry
(deaddit.runtime.nightly), and the web-process-zero-scheduler invariant.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from deaddit import db as _db
from deaddit.models import Job, JobStatus, JobType

_REPO_ROOT = Path(__file__).resolve().parents[1]

# A minimal user payload accepted by services.content.create_user; gender and
# education are overwritten by _generate_user_data from its own prompt choices.
_USER_PAYLOAD = {
    "username": "a5_integration_user",
    "age": 34,
    "bio": "External observer description of a quiet cartographer.",
    "interests": ["cartography", "chess"],
    "occupation": "cartographer",
    "writing_style": "Plain and precise.",
    "personality_traits": ["curious", "stubborn"],
}


def _make_job(**overrides) -> Job:
    """Build an in-memory Job row with sensible defaults."""
    fields = {
        "type": JobType.CREATE_USER,
        "status": JobStatus.PENDING,
        "priority": 5,
        "total_items": 1,
        "parameters": {"count": 1},
    }
    fields.update(overrides)
    return Job(**fields)


# ---------------------------------------------------------------------------
# Web process never starts a scheduler
# ---------------------------------------------------------------------------


def test_create_job_does_not_start_scheduler(app, db_session, monkeypatch):
    """create_app()+create_job() must not instantiate or start APScheduler."""
    import apscheduler.schedulers.background as aps_bg

    started = []

    def _init_boom(self, *args, **kwargs):
        raise AssertionError(
            "BackgroundScheduler was instantiated on the web/job-creation path"
        )

    real_start = aps_bg.BackgroundScheduler.start

    def _spy_start(self, *args, **kwargs):
        started.append("start")
        return real_start(self, *args, **kwargs)

    monkeypatch.setattr(aps_bg.BackgroundScheduler, "__init__", _init_boom)
    monkeypatch.setattr(aps_bg.BackgroundScheduler, "start", _spy_start)

    # Imported *after* the traps are armed: any module-level scheduler
    # instantiation trips immediately.
    from deaddit import jobs

    job = jobs.create_job(JobType.CREATE_USER, {"count": 0}, priority=5)

    row = _db.session.get(Job, job.id)
    assert row is not None
    assert row.status == JobStatus.PENDING
    assert started == []


# ---------------------------------------------------------------------------
# Claim / heartbeat / sweep contract
# ---------------------------------------------------------------------------


def test_claim_job_atomic_single_winner(db_session):
    from deaddit.runtime import claim

    job = _make_job()
    db_session.add(job)
    db_session.commit()
    job_id = job.id

    first = claim.claim_job(job_id, "worker-a")
    second = claim.claim_job(job_id, "worker-b")

    assert sorted([first, second]) == [False, True], (
        "exactly one worker may win a claim"
    )
    winner = "worker-a" if first else "worker-b"

    _db.session.expire_all()
    row = _db.session.get(Job, job_id)
    assert row.status == JobStatus.RUNNING
    assert row.worker_id == winner
    assert row.claimed_at is not None
    assert row.heartbeat_at is not None


def test_sweep_requeues_stale_running(db_session):
    from deaddit.runtime import claim

    now = datetime.utcnow()
    stale = _make_job(
        status=JobStatus.RUNNING,
        worker_id="dead-worker",
        claimed_at=now - timedelta(minutes=10),
        heartbeat_at=now - timedelta(minutes=10),
    )
    fresh = _make_job(
        status=JobStatus.RUNNING,
        worker_id="live-worker",
        claimed_at=now,
        heartbeat_at=now,
    )
    legacy_null = _make_job(
        status=JobStatus.RUNNING,
        worker_id="pre-a5-crash",
        claimed_at=now - timedelta(minutes=30),
        heartbeat_at=None,
    )
    db_session.add_all([stale, fresh, legacy_null])
    db_session.commit()

    swept = claim.sweep_stale_jobs()

    assert swept == 2
    _db.session.expire_all()

    stale_row = _db.session.get(Job, stale.id)
    assert stale_row.status == JobStatus.PENDING
    assert stale_row.worker_id is None
    assert stale_row.claimed_at is None
    assert stale_row.heartbeat_at is None

    fresh_row = _db.session.get(Job, fresh.id)
    assert fresh_row.status == JobStatus.RUNNING
    assert fresh_row.worker_id == "live-worker"

    legacy_row = _db.session.get(Job, legacy_null.id)
    assert legacy_row.status == JobStatus.PENDING


# ---------------------------------------------------------------------------
# Boot sweep + exactly-once execution through JobRunner
# ---------------------------------------------------------------------------


def test_boot_sweep_then_execute_once(app, fake_llm, db_session, monkeypatch):
    """Crashed RUNNING job -> boot sweep -> JobRunner executes exactly once."""
    from deaddit.runtime import claim
    from deaddit.runtime.runner import JobRunner

    now = datetime.utcnow()
    job = _make_job(
        status=JobStatus.RUNNING,
        worker_id="crashed-worker",
        claimed_at=now - timedelta(minutes=10),
        heartbeat_at=now - timedelta(minutes=10),
    )
    db_session.add(job)
    db_session.commit()
    job_id = job.id

    # Worker boot: sweep requeues the crashed lease.
    assert claim.sweep_stale_jobs() == 1
    _db.session.expire_all()
    assert _db.session.get(Job, job_id).status == JobStatus.PENDING

    # One LLM round-trip generates the persona; the executor consumes it.
    fake_llm.enqueue_content(json.dumps(_USER_PAYLOAD))

    monkeypatch.setenv("DEADDIT_WORKER_POLL_SECONDS", "0.05")
    runner = JobRunner(app)
    row = None
    try:
        runner.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            _db.session.expire_all()
            row = _db.session.get(Job, job_id)
            if row.status == JobStatus.COMPLETED:
                break
            time.sleep(0.05)
    finally:
        runner.stop(wait=True)

    assert row is not None and row.status == JobStatus.COMPLETED, (
        f"job did not complete within deadline: status="
        f"{getattr(row, 'status', None)} error={getattr(row, 'error_message', None)}"
    )
    assert len(fake_llm.requests) == 1, "executor hit the LLM more than once"
    assert row.result["users"] == [_USER_PAYLOAD["username"]]


# ---------------------------------------------------------------------------
# Web process proof: zero APScheduler presence after import
# ---------------------------------------------------------------------------

_ZERO_SCHEDULER_SCRIPT = """
import threading


def main():
    import deaddit.wsgi  # noqa: F401  web entrypoint under test

    bad_threads = [
        t.name for t in threading.enumerate() if t.name.startswith("APScheduler")
    ]
    assert not bad_threads, f"scheduler threads in web process: {bad_threads}"

    import deaddit.jobs as jobs

    assert not hasattr(jobs, "scheduler"), (
        "deaddit.jobs.scheduler global still exists"
    )
    print("OK")


main()
"""


def test_web_process_zero_scheduler_on_import():
    proc = subprocess.run(
        [sys.executable, "-c", _ZERO_SCHEDULER_SCRIPT],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "OK" in proc.stdout


# ---------------------------------------------------------------------------
# Queue stats shape + liveness wiring
# ---------------------------------------------------------------------------


def test_get_queue_stats_shape_and_liveness(app, db_session):
    from deaddit import jobs
    from deaddit.runtime import claim

    stats = jobs.get_queue_stats()
    assert stats["scheduler_running"] is False
    assert claim.liveness_is_fresh() is False

    claim.write_worker_liveness("w1")

    assert claim.liveness_is_fresh() is True
    stats = jobs.get_queue_stats()
    assert stats["scheduler_running"] is True

    db_session.add_all(
        [
            _make_job(status=JobStatus.PENDING),
            _make_job(status=JobStatus.RUNNING, worker_id="w1"),
        ]
    )
    db_session.commit()

    stats = jobs.get_queue_stats()
    assert stats["pending_jobs"] == 1
    assert stats["running_jobs"] == 1


def test_nightly_registry_registers_into_scheduler(app, monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler

    from deaddit.runtime import nightly as nightly_mod
    from deaddit.runtime.nightly import NightlyJob

    def _noop():
        return None

    temp = NightlyJob(
        id="temp_test_nightly",
        cron_expression="* * * * *",
        func=_noop,
        description="temporary registry probe",
    )
    monkeypatch.setattr(
        nightly_mod, "NIGHTLY_JOBS", tuple(nightly_mod.NIGHTLY_JOBS) + (temp,)
    )

    scheduler = BackgroundScheduler()
    # A never-started scheduler keeps jobs as pending entries;
    # get_jobs() would raise SchedulerNotRunningError here.
    registered_ids = nightly_mod.register_nightly_jobs(scheduler)
    assert "temp_test_nightly" in registered_ids
    assert any(entry[0].id == "temp_test_nightly" for entry in scheduler._pending_jobs)


