"""Web-process log tailer (Phase UX-5).

A single lazy daemon thread inside the WEB process polls the ``job_log``
table for rooms with joined socket clients and pushes:

* ``job_log`` events to room ``job_log:<id>`` (live streamed lines), and
* ``job_update`` status/progress deltas to the classic ``job_updates`` room
  for every job it is already watching plus all non-terminal jobs (repairs
  the post-A5 dead live updates: gunicorn sync workers cannot push sockets
  worker -> web, so the DB rows are the transport and this thread is the pump).

No broker involved. The thread starts on the first room join and exits by
itself once no client remains anywhere.
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy import select

from deaddit.extensions import db, socketio
from deaddit.models import Job, JobLog, JobStatus

logger = logging.getLogger(__name__)

NAMESPACE = "/admin"
LOG_ROOM_PREFIX = "job_log:"
UPDATES_ROOM = "job_updates"

# Injected per tick so tests can drive cycles synchronously.
TICK_INTERVAL_SECONDS = 1.0

_TERMINAL_STATUSES = {
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
}


def _participants(room: str) -> int:
    """Count joined clients in a room (in-memory manager, threading mode)."""
    server = getattr(socketio, "server", None)
    if server is None:
        return 0
    try:
        return sum(1 for _ in server.manager.get_participants(NAMESPACE, room))
    except Exception:  # pragma: no cover - manager edge cases
        return 0


class JobLogTailer:
    """Poll JobLog/Job rows and emit deltas to joined socket rooms."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # App captured at start so the daemon thread can open app contexts
        # (db.engine/socketio resolves need one; thread itself has none).
        self._app = None
        # job_id -> last seq pushed to the job's log room
        self._log_cursors: dict[int, int] = {}
        # job_id -> last (status, progress, error_message) emitted to job_updates
        self._update_snapshots: dict[str, tuple] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ensure_started(self) -> None:
        """Start the daemon thread if it is not running (idempotent)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._stop.clear()
                return
            if self._app is None:
                from flask import current_app

                self._app = current_app._get_current_object()
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._run, name="admin-log-tailer", daemon=True
            )
            self._thread.start()
            logger.debug("Admin log tailer started")

    def stop(self) -> None:
        self._stop.set()

    def note_join(self, job_id: int) -> None:
        """Register interest in a job's log room before starting the pump."""
        with self._lock:
            self._log_cursors.setdefault(job_id, 0)
            self._update_snapshots.setdefault(str(job_id), None)
        self.ensure_started()

    def note_leave(self, job_id: int) -> None:
        with self._lock:
            self._log_cursors.pop(job_id, None)

    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive() and not self._stop.is_set()

    def _run(self) -> None:
        idle_cycles = 0
        while not self._stop.wait(TICK_INTERVAL_SECONDS):
            try:
                with self._app.app_context():
                    active = self.tick()
            except Exception:
                logger.exception("Admin log tailer cycle failed")
                active = True
            idle_cycles = 0 if active else idle_cycles + 1
            if idle_cycles >= 2:
                logger.debug("No tailer clients remain; stopping")
                return

    # ------------------------------------------------------------------
    # One synchronous pass
    # ------------------------------------------------------------------

    def tick(self) -> bool:
        """Run one poll/emit pass. Returns True if any room had clients."""
        any_clients = False

        with self._lock:
            watched_ids = list(self._log_cursors.keys())

        # Prune rooms whose last client left.
        live_ids = []
        for job_id in watched_ids:
            if _participants(f"{LOG_ROOM_PREFIX}{job_id}") > 0:
                live_ids.append(job_id)
            else:
                self.note_leave(job_id)
        any_clients |= bool(live_ids)

        for job_id in live_ids:
            any_clients |= self._push_job_logs(job_id)

        # Status/progress repair for the updates room.
        any_clients |= self._push_job_updates(live_ids)
        return any_clients

    def _push_job_logs(self, job_id: int) -> bool:
        with self._lock:
            cursor = self._log_cursors.get(job_id, 0)
        conn = db.engine.connect()
        try:
            cursor_rows = conn.execute(
                select(
                    JobLog.seq,
                    JobLog.created_at,
                    JobLog.level,
                    JobLog.message,
                )
                .where(JobLog.job_id == job_id, JobLog.seq > cursor)
                .order_by(JobLog.seq.asc())
                .limit(500)
            ).all()
        finally:
            conn.close()
        if not cursor_rows:
            return False
        payload_lines = [
            {
                "seq": r.seq,
                "ts": r.created_at.isoformat() if r.created_at else None,
                "level": r.level,
                "message": r.message,
            }
            for r in cursor_rows
        ]
        with self._lock:
            self._log_cursors[job_id] = max(
                cursor, max(r.seq for r in cursor_rows)
            )
        socketio.emit(
            "job_log",
            {"job_id": job_id, "lines": payload_lines},
            room=f"{LOG_ROOM_PREFIX}{job_id}",
            namespace=NAMESPACE,
        )
        return True

    def _push_job_updates(self, watched_ids: list[int]) -> bool:
        if _participants(UPDATES_ROOM) == 0:
            return False

        # Watched jobs + everything still in flight.
        query = select(Job).where(
            (Job.status.notin_(_TERMINAL_STATUSES)) | (Job.id.in_(watched_ids))
        ).limit(200)
        # Session (not bare-connection) execution: Core rows would be raw
        # column tuples, so .scalars() would yield the id int instead of
        # the Job entity.
        jobs = db.session.execute(query).scalars().all()

        with self._lock:
            snapshots = self._update_snapshots
            current_ids = {str(j.id) for j in jobs}
            for stale in list(snapshots):
                if stale not in current_ids:
                    del snapshots[stale]

        for job in jobs:
            state = (
                job.status.value if job.status else None,
                job.progress or 0,
                job.error_message,
            )
            with self._lock:
                unchanged = snapshots.get(str(job.id)) == state
                snapshots[str(job.id)] = state
            if unchanged:
                continue
            socketio.emit(
                "job_update",
                {
                    "job_id": job.id,
                    "status": state[0],
                    "progress": state[1],
                    "total_items": job.total_items,
                    "error_message": job.error_message,
                    "completed_at": job.completed_at.isoformat()
                    if job.completed_at
                    else None,
                    "started_at": job.started_at.isoformat()
                    if job.started_at
                    else None,
                },
                room=UPDATES_ROOM,
                namespace=NAMESPACE,
            )
        return True


_tailer: JobLogTailer | None = None
_tailer_lock = threading.Lock()


def get_tailer() -> JobLogTailer:
    """Process-wide singleton."""
    global _tailer
    with _tailer_lock:
        if _tailer is None:
            _tailer = JobLogTailer()
        return _tailer


def reset_tailer() -> None:
    """Test seam: drop the singleton (stops any running thread)."""
    global _tailer
    with _tailer_lock:
        if _tailer is not None:
            _tailer.stop()
        _tailer = None
