"""Poller and lane executors for the dedicated worker process."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Flask
from sqlalchemy import select

from deaddit.extensions import db
from deaddit.models import Job, JobStatus
from deaddit.runtime.claim import claim_job, heartbeat, write_worker_liveness

logger = logging.getLogger(__name__)

#: Minimum interval between worker-liveness writes (seconds).
LIVENESS_WRITE_INTERVAL_SECONDS = 30


class JobRunner:
    """Polls the Job table, claims pending jobs, and executes them in lanes."""

    def __init__(self, app: Flask) -> None:
        self.app = app
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self._poll_seconds = float(os.environ.get("DEADDIT_WORKER_POLL_SECONDS", "2.0"))
        self._heartbeat_seconds = float(
            os.environ.get("DEADDIT_WORKER_HEARTBEAT_SECONDS", "30")
        )
        self._lane_size = int(os.environ.get("DEADDIT_WORKER_LANE_SIZE", "2"))
        self._lanes: dict[str, ThreadPoolExecutor] = {}
        self._stop_event = threading.Event()
        self._poller_thread: threading.Thread | None = None
        self._heartbeat_threads: list[threading.Thread] = []
        self._last_liveness = 0.0

    def start(self) -> None:
        """Spawn the daemon poller thread."""
        if self._poller_thread is not None:
            raise RuntimeError("JobRunner already started")
        self._poller_thread = threading.Thread(
            target=self._poll_loop, name="job-poller", daemon=True
        )
        self._poller_thread.start()

    def stop(self, wait: bool = True) -> None:
        """Stop the poller, shut down lane executors, and join heartbeat threads."""
        self._stop_event.set()
        if self._poller_thread is not None:
            self._poller_thread.join(timeout=10)
            self._poller_thread = None
        for name, executor in self._lanes.items():
            executor.shutdown(wait=wait)
            logger.debug("Shut down %s lane", name)
        for thread in list(self._heartbeat_threads):
            thread.join(timeout=10)

    # ------------------------------------------------------------------
    # Poller
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        logger.info(
            "Poller loop started (poll=%.1fs, heartbeat=%.1fs, lane_size=%d)",
            self._poll_seconds,
            self._heartbeat_seconds,
            self._lane_size,
        )
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                self._poll_once()
            except Exception:
                logger.exception("Poll iteration failed")
            elapsed = time.monotonic() - started
            self._stop_event.wait(max(self._poll_seconds - elapsed, 0.0))

    def _poll_once(self) -> None:
        with self.app.app_context():
            now = time.monotonic()
            if now - self._last_liveness >= LIVENESS_WRITE_INTERVAL_SECONDS:
                write_worker_liveness(self.worker_id)
                self._last_liveness = now

            rows = db.session.execute(
                select(Job.id, Job.priority)
                .where(Job.status == JobStatus.PENDING)
                .order_by(Job.priority.desc(), Job.id.asc())
            ).all()

            for job_id, priority in rows:
                if self._stop_event.is_set():
                    break
                if claim_job(job_id, self.worker_id):
                    future = self._lane_for(priority).submit(self._run_job, job_id)
                    future.add_done_callback(self._log_future_error)

    def _lane_for(self, priority: int) -> ThreadPoolExecutor:
        name = (
            "high_priority"
            if priority >= 8
            else "low_priority"
            if priority <= 3
            else "default"
        )
        return self._lanes.setdefault(
            name,
            ThreadPoolExecutor(
                max_workers=self._lane_size, thread_name_prefix=f"job-{name}"
            ),
        )

    @staticmethod
    def _log_future_error(future) -> None:
        error = future.exception()
        if error is not None:
            logger.error("Job crashed in worker lane", exc_info=error)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _run_job(self, job_id: int) -> None:
        from deaddit import jobs

        stop_heartbeat = threading.Event()
        thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(job_id, stop_heartbeat),
            name=f"heartbeat-{job_id}",
            daemon=True,
        )
        thread.start()
        self._heartbeat_threads.append(thread)
        try:
            jobs.execute_job(job_id, app=self.app)
        finally:
            stop_heartbeat.set()

    def _heartbeat_loop(self, job_id: int, stop_event: threading.Event) -> None:
        while not stop_event.wait(self._heartbeat_seconds):
            try:
                with self.app.app_context():
                    heartbeat(job_id)
            except Exception:
                logger.exception("Heartbeat failed for job %s", job_id)
