"""Job queue plumbing for Deaddit's BATCH_OPERATION jobs.

Provides execute_job dispatching BATCH_OPERATION, create_job for batch sub-jobs,
and thread-local progress updates. Job claiming and heartbeat logic live in
deaddit.runtime.
"""

import logging
import threading
from datetime import datetime
from typing import Any

from deaddit.extensions import db
from deaddit.models import Job, JobStatus, JobType

logger = logging.getLogger(__name__)

# Thread-local storage for job progress updates
_thread_local = threading.local()


def _default_app():
    """Resolve the Flask app; callers may pass their own instead."""
    from deaddit import create_app

    return create_app()


def create_job(
    job_type: JobType,
    parameters: dict[str, Any],
    priority: int = 5,
    total_items: int = 1,
    delay_seconds: int = 0,
) -> Job:
    """Create a new pending job row for the worker process to pick up."""

    # Create job record in database
    job = Job(
        type=job_type,
        status=JobStatus.PENDING,
        priority=priority,
        total_items=total_items,
        parameters=parameters,
    )

    db.session.add(job)
    db.session.commit()

    return job


def execute_job(job_id: int, app=None) -> dict[str, Any]:
    """Execute a job based on its type."""

    app = app or _default_app()

    with app.app_context():
        # Store job ID in thread-local storage for progress updates
        _thread_local.job_id = job_id

        # Get the job from database
        job = db.session.get(Job, job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        # Update job status to running
        job.status = JobStatus.RUNNING
        job.started_at = datetime.utcnow()
        db.session.commit()

        # UX-5: capture deaddit.* log lines as JobLog rows for the live
        # admin log pane. Failure-isolated: log writes never break the job.
        _job_log_handler = _attach_job_log_handler(job_id)

        try:
            logger.info(f"Executing job {job_id} ({job.type.value})")

            # Execute based on job type
            if job.type == JobType.BATCH_OPERATION:
                result = _execute_batch_operation(job)
            else:
                raise ValueError(f"Unknown job type: {job.type}")

            # Update job as completed
            job = db.session.get(Job, job_id)  # Re-fetch to avoid stale data
            job.status = JobStatus.COMPLETED
            job.completed_at = datetime.utcnow()
            job.progress = job.total_items
            job.result = result
            db.session.commit()
            logger.info(f"Job {job_id} completed successfully")

            return result

        except Exception as e:
            # Update job as failed
            job = db.session.get(Job, job_id)  # Re-fetch to avoid stale data
            job.status = JobStatus.FAILED
            job.completed_at = datetime.utcnow()
            job.error_message = str(e)
            db.session.commit()
            logger.error(f"Job {job_id} failed: {e}")
            raise
        finally:
            _job_log_handler.detach()


def _attach_job_log_handler(job_id: int):
    """Attach the UX-5 log-capture handler (best-effort, never fatal)."""
    from deaddit.runtime.joblog import capture_job_logs

    try:
        return capture_job_logs(job_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("JobLog capture unavailable for job %s: %s", job_id, exc)

        class _Null:
            def detach(self):
                pass

        return _Null()


def _update_job_progress(progress: int):
    """Update job progress in database (thread-safe)."""
    if not hasattr(_thread_local, "job_id"):
        return

    try:
        job = db.session.get(Job, _thread_local.job_id)
        if job:
            job.progress = progress
            db.session.commit()
    except Exception as e:
        logger.warning(f"Could not update job progress: {e}")


def _execute_batch_operation(job: Job) -> dict[str, Any]:
    """Execute batch operation job."""
    params = job.parameters
    operations = params.get("operations", [])

    results = []

    for i, operation in enumerate(operations):
        # Update progress
        _update_job_progress(i)

        # Create sub-job for each operation
        sub_job = create_job(
            job_type=JobType(operation["type"]),
            parameters=operation["parameters"],
            priority=job.priority,
        )

        results.append({"operation": operation, "job_id": sub_job.id})

    return {"batch_results": results, "count": len(results)}
