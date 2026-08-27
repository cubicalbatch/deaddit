from __future__ import annotations

import logging
import pathlib
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import or_, update

from deaddit.extensions import db
from deaddit.models import Job, JobStatus, Setting

logger = logging.getLogger(__name__)

#: Sweep threshold: running jobs whose heartbeat is older than this are stale.
HEARTBEAT_STALE_MINUTES = 5

#: Freshness window for the worker-liveness timestamp.
LIVENESS_MAX_AGE_SECONDS = 90

#: Setting key holding an ISO ``utcnow`` timestamp of the last worker ping.
WORKER_LIVENESS_SETTING_KEY = "WORKER_HEARTBEAT_AT"

#: Filename inside ``app.instance_path`` touched on every worker liveness write.
WORKER_HEARTBEAT_FILE = "worker-heartbeat"


def claim_job(job_id: int, worker_id: str) -> bool:
    """Atomically claim a pending job for ``worker_id``.

    Performs a single conditional UPDATE so that competing workers cannot both
    win: only a row still in PENDING transitions to RUNNING.

    Returns True if this caller won the claim.
    """
    now = datetime.utcnow()
    result = db.session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.PENDING)
        .values(
            status=JobStatus.RUNNING,
            started_at=now,
            claimed_at=now,
            worker_id=worker_id,
            heartbeat_at=now,
        )
    )
    db.session.commit()
    won = result.rowcount == 1
    if won:
        logger.info("Worker %s claimed job %s", worker_id, job_id)
    return won


def heartbeat(job_id: int) -> None:
    """Bump the heartbeat timestamp of a claimed job."""
    db.session.execute(
        update(Job).where(Job.id == job_id).values(heartbeat_at=datetime.utcnow())
    )
    db.session.commit()


def sweep_stale_jobs(stale_minutes: int = HEARTBEAT_STALE_MINUTES) -> int:
    """Return stale RUNNING jobs to PENDING and return how many were swept.

    A job is stale when its ``heartbeat_at`` is older than the cutoff, or when
    it has no heartbeat timestamp recorded.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=stale_minutes)
    result = db.session.execute(
        update(Job)
        .where(
            Job.status == JobStatus.RUNNING,
            or_(Job.heartbeat_at.is_(None), Job.heartbeat_at < cutoff),
        )
        .values(
            status=JobStatus.PENDING, claimed_at=None, worker_id=None, heartbeat_at=None
        )
    )
    db.session.commit()
    count = result.rowcount or 0
    if count:
        logger.warning("Swept %d stale running job(s) back to pending", count)
    return count


def write_worker_liveness(worker_id: str) -> None:
    """Record worker liveness in the Setting store and on the filesystem."""
    now_iso = datetime.utcnow().isoformat()
    Setting.set_value(WORKER_LIVENESS_SETTING_KEY, now_iso)

    instance_path = pathlib.Path(current_app.instance_path)
    try:
        (instance_path / WORKER_HEARTBEAT_FILE).touch()
    except OSError as exc:
        logger.warning("Could not touch %s: %s", WORKER_HEARTBEAT_FILE, exc)


def liveness_is_fresh(max_age_seconds: int = LIVENESS_MAX_AGE_SECONDS) -> bool:
    """True iff a worker wrote its liveness timestamp within the window."""
    raw = Setting.get_value(WORKER_LIVENESS_SETTING_KEY)
    if not raw:
        return False
    try:
        seen = datetime.fromisoformat(raw)
    except ValueError:
        logger.warning("Unparseable %s value: %r", WORKER_LIVENESS_SETTING_KEY, raw)
        return False
    age = datetime.utcnow() - seen.replace(tzinfo=None)
    return timedelta(0) <= age <= timedelta(seconds=max_age_seconds)
