"""Worker-side job log capture (Phase UX-5).

``JobLogHandler`` is attached to the ``deaddit`` logger for the duration of
``jobs.execute_job``. Every record logged through the ``deaddit.*`` hierarchy
while the handler is live becomes a ``JobLog`` row, batched (>= 10 lines or
>= 1 s since the last flush) through its OWN SQLAlchemy connection so log
writes can never corrupt or roll back the job's session.

The web process never runs this handler: it reads the same rows via the
tailer (deaddit.runtime.tailer) and the HTTP fallback endpoint.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import UTC, datetime

from sqlalchemy import delete, insert, select

from deaddit.extensions import db
from deaddit.models import JobLog

# Attach point: the 'deaddit' logger captures every deaddit.* child logger
# (jobs, llm client, services) without pulling in third-party noise that goes
# to root.
ATTACH_LOGGER_NAME = "deaddit"

FLUSH_BATCH_LINES = 10
FLUSH_INTERVAL_SECONDS = 1.0
MAX_JOB_LOG_LINES = 500


class JobLogHandler(logging.Handler):
    """Buffer log records and persist them as JobLog rows for one job."""

    def __init__(self, job_id: int):
        super().__init__(level=logging.INFO)
        self.job_id = job_id
        self._buffer: list[dict] = []
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()
        self._in_flush = False  # recursion guard: our own errors must not loop
        self._attached_logger: logging.Logger | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def attach(self) -> JobLogHandler:
        target = logging.getLogger(ATTACH_LOGGER_NAME)
        target.addHandler(self)
        self._attached_logger = target
        return self

    def detach(self) -> None:
        if self._attached_logger is not None:
            self._attached_logger.removeHandler(self)
            self._attached_logger = None
        self.flush()

    # ------------------------------------------------------------------
    # logging.Handler API
    # ------------------------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        if self._in_flush:
            return
        entry = {
            "level": record.levelname,
            "message": record.getMessage(),
            "created_at": datetime.fromtimestamp(
                record.created, tz=UTC
            ).replace(tzinfo=None),
        }
        should_flush = False
        with self._lock:
            self._buffer.append(entry)
            if len(self._buffer) >= FLUSH_BATCH_LINES:
                should_flush = True
        if not should_flush and (
            time.monotonic() - self._last_flush >= FLUSH_INTERVAL_SECONDS
        ):
            should_flush = True
        if should_flush:
            self.flush()

    def flush(self) -> None:  # type: ignore[override]
        """Persist buffered lines; NEVER raises into the calling job."""
        with self._lock:
            batch, self._buffer = self._buffer, []
        if not batch:
            return
        self._in_flush = True
        self._last_flush = time.monotonic()
        try:
            self._write_batch(batch)
        except Exception as exc:  # noqa: BLE001 - failure isolation is the point
            print(
                f"[joblog] failed to persist {len(batch)} log lines for job "
                f"{self.job_id}: {exc!r}",
                file=sys.stderr,
            )
        finally:
            self._in_flush = False

    # ------------------------------------------------------------------
    # Persistence (own connection, own transaction)
    # ------------------------------------------------------------------

    def _write_batch(self, batch: list[dict]) -> None:
        rows = [
            {
                "job_id": self.job_id,
                "seq": 0,  # filled below
                "created_at": entry["created_at"],
                "level": entry["level"],
                "message": entry["message"],
            }
            for entry in batch
        ]
        conn = db.engine.connect()
        try:
            next_seq = (
                conn.execute(
                    select(JobLog.seq)
                    .where(JobLog.job_id == self.job_id)
                    .order_by(JobLog.seq.desc())
                    .limit(1)
                ).scalar()
                or 0
            )
            for offset, row in enumerate(rows, start=1):
                row["seq"] = next_seq + offset
            conn.execute(insert(JobLog), rows)

            # Cap: keep at most MAX_JOB_LOG_LINES per job (trim oldest).
            max_seq = rows[-1]["seq"]
            conn.execute(
                delete(JobLog).where(
                    JobLog.job_id == self.job_id,
                    JobLog.seq <= max_seq - MAX_JOB_LOG_LINES,
                )
            )
            conn.commit()
        finally:
            conn.close()


def capture_job_logs(job_id: int) -> JobLogHandler:
    """Attach a handler for ``job_id``; caller MUST detach (use try/finally)."""
    return JobLogHandler(job_id).attach()
