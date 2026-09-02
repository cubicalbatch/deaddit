"""Job queue plumbing for Deaddit's BATCH_OPERATION and AGENT_RUN jobs.

Provides execute_job dispatching BATCH_OPERATION and AGENT_RUN, create_job
for batch sub-jobs, and thread-local progress updates. Job claiming and
heartbeat logic live in deaddit.runtime.
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
            elif job.type == JobType.AGENT_RUN:
                result = _execute_agent_run(job)
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
    """Execute a batch operation job, including queued persona generation."""
    params = job.parameters or {}
    if params.get("operation") == "persona_generation":
        from deaddit.services.persona_generator import generate_personas

        result = generate_personas(
            count=params.get("count", 1),
            topic_hint=params.get("topic_hint"),
            auto_create_agent=params.get("auto_create_agent", False),
            tier=params.get("tier", "regular"),
            api_url=params.get("api_url"),
            model=params.get("model"),
            troll_mode=params.get("troll_mode", "chance"),
        )
        return {"operation": "persona_generation", **result}

    operations = params.get("operations", [])
    results = []
    for i, operation in enumerate(operations):
        _update_job_progress(i)
        sub_job = create_job(
            job_type=JobType(operation["type"]),
            parameters=operation["parameters"],
            priority=job.priority,
        )
        results.append({"operation": operation, "job_id": sub_job.id})
    return {"batch_results": results, "count": len(results)}


#: Job priority for admin-initiated agent runs. The runner routes
# priorities >= 8 to its high-priority lane, so an explicit admin request
# is claimed ahead of routine batch work.
AGENT_RUN_JOB_PRIORITY = 9


def _execute_agent_run(job: Job) -> dict[str, Any]:
    """Execute one admin-requested agent visit through the normal agent loop.

    ``run_once`` owns persona reservation, visit planning, the LLM loop, and
    terminal run bookkeeping. This handler only validates the persisted
    parameters defensively, dispatches, and reports identifiers; an
    ``AgentRun(status="failed")`` is still a successfully executed queue job.
    """
    from deaddit.agents.loop import run_once
    from deaddit.models import Agent

    params = job.parameters or {}
    agent_id = params.get("agent_id")
    if not isinstance(agent_id, int) or isinstance(agent_id, bool):
        raise ValueError(f"AGENT_RUN job {job.id} has invalid agent_id {agent_id!r}")
    requested_intent = params.get("requested_intent")
    if requested_intent not in (None, "image", "website"):
        raise ValueError(
            f"AGENT_RUN job {job.id} has invalid requested_intent {requested_intent!r}"
        )

    if db.session.get(Agent, agent_id) is None:
        raise ValueError(f"AGENT_RUN job {job.id}: no agent with id {agent_id}")

    run = None
    try:
        run = run_once(agent_id, trigger="manual", requested_intent=requested_intent)
        return {
            "agent_id": agent_id,
            "run_id": run.id,
            "requested_intent": requested_intent,
            "resolved_intent": run.intent,
            "run_status": run.status,
        }
    finally:
        _release_manual_run(agent_id, job.id, run_obtained=run is not None)


def _release_manual_run(agent_id: int, job_id: int, *, run_obtained: bool) -> None:
    """Clear ``Agent.state.manual_run`` once a manual job reaches an outcome.

    Before ``run_once`` reserves an ``AgentRun``, the queue's claim is the
    only status bookkeeping, so the pre-queue status is restored. Once a run
    exists (returned, or left reserved by an unexpected exception), the
    agent loop's own terminal bookkeeping owns the agent status and only the
    manual marker is cleared. Best-effort by design: it must never mask the
    job's original outcome.
    """
    from deaddit.models import Agent, AgentRun

    db.session.rollback()  # discard partial state left by a failed attempt
    try:
        agent = db.session.get(Agent, agent_id)
        if agent is None:
            return
        state = dict(agent.state or {})
        manual = state.get("manual_run")
        if not isinstance(manual, dict) or manual.get("job_id") != job_id:
            return  # a newer manual request owns the marker now
        state.pop("manual_run", None)
        agent.state = state
        if not run_obtained and (
            AgentRun.query.filter_by(agent_id=agent_id, status="running").first()
            is None
        ):
            previous = manual.get("previous_status")
            agent.status = (
                previous if isinstance(previous, str) and previous else "idle"
            )
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Failed to clear manual-run state for agent %s", agent_id)
