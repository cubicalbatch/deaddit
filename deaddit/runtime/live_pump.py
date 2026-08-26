"""Public live-activity ticker pump (Phase UX-6, Slice A).

A single lazy daemon thread inside the WEB process polls the three activity
sources (via the shared predicates in :mod:`deaddit.live`) and pushes
``live_count {"count": N}`` to room ``activity`` on namespace ``/live``
whenever rows exist past the room's watermark. The count is cumulative since
the last ``activity_loaded`` ack from the client; the watermark only advances
on that ack (or is re-initialised on join). Item content NEVER travels over
the socket.

Singleton pattern: lazy start on first room join, idle auto-exit after 2
empty cycles.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from deaddit.extensions import socketio
from deaddit.live import count_events_after, max_event_ts

logger = logging.getLogger(__name__)

NAMESPACE = "/live"
ROOM = "activity"

# Injected per tick so tests can drive cycles synchronously.
TICK_INTERVAL_SECONDS = 1.0


def _participants(room: str) -> int:
    """Count joined clients in a room (in-memory manager, threading mode)."""
    server = getattr(socketio, "server", None)
    if server is None:
        return 0
    try:
        return sum(1 for _ in server.manager.get_participants(NAMESPACE, room))
    except Exception:  # pragma: no cover - manager edge cases
        return 0


class ActivityPump:
    """Poll activity sources and emit live_count deltas to the activity room."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # App captured at start so the daemon thread can open app contexts
        # (db.engine/socketio resolves need one; thread itself has none).
        self._app = None
        # room -> watermark datetime (max event ts at last join/ack)
        self._watermarks: dict[str, datetime | None] = {}
        # room -> pending count of events past the watermark
        self._pending: dict[str, int] = {}

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
                target=self._run, name="live-activity-pump", daemon=True
            )
            self._thread.start()
            logger.debug("Live activity pump started")

    def stop(self) -> None:
        self._stop.set()

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
                logger.exception("Live activity pump cycle failed")
                active = True
            idle_cycles = 0 if active else idle_cycles + 1
            if idle_cycles >= 2:
                logger.debug("No live activity clients remain; stopping")
                return

    # ------------------------------------------------------------------
    # Room bookkeeping (lock-guarded dicts keyed by room)
    # ------------------------------------------------------------------

    def note_join(self, room: str = ROOM) -> None:
        """Register a join: initialise the watermark to the current max event ts."""
        with self._lock:
            known = room in self._watermarks
        if not known:
            try:
                watermark = max_event_ts()
            except Exception:
                logger.exception("Failed to read activity watermark")
                watermark = None
            with self._lock:
                self._watermarks.setdefault(room, watermark)
                self._pending.setdefault(room, 0)
        self.ensure_started()

    def note_leave(self, room: str = ROOM) -> None:
        """Drop room state so a later join re-initialises its watermark."""
        with self._lock:
            self._watermarks.pop(room, None)
            self._pending.pop(room, None)

    def note_activity_loaded(self, ts_iso, room: str = ROOM) -> None:
        """Client ack after an explicit load: reset pending, advance watermark.

        A missing/invalid payload ts keeps the current watermark but still
        clears the pending count.
        """
        ts: datetime | None = None
        if isinstance(ts_iso, str):
            try:
                ts = datetime.fromisoformat(ts_iso)
            except ValueError:
                ts = None
        with self._lock:
            self._pending[room] = 0
            current = self._watermarks.get(room)
            if ts is not None and (current is None or ts > current):
                self._watermarks[room] = ts

    # ------------------------------------------------------------------
    # One synchronous pass
    # ------------------------------------------------------------------

    def tick(self) -> bool:
        """Run one poll/emit pass. Returns True if any room had clients."""
        any_clients = False
        with self._lock:
            rooms = list(self._watermarks.keys())

        for room in rooms:
            if _participants(room) == 0:
                continue
            any_clients = True
            with self._lock:
                watermark = self._watermarks.get(room)
            try:
                pending = count_events_after(watermark)
            except Exception:
                logger.exception("Activity count query failed")
                continue
            with self._lock:
                self._pending[room] = pending
            # Re-emitted each tick while pending > 0 so late joiners and
            # reconnects converge on the same cumulative count.
            if pending > 0:
                socketio.emit(
                    "live_count",
                    {"count": pending},
                    room=room,
                    namespace=NAMESPACE,
                )
        return any_clients


_live_pump: ActivityPump | None = None
_live_pump_lock = threading.Lock()


def get_live_pump() -> ActivityPump:
    """Process-wide singleton."""
    global _live_pump
    with _live_pump_lock:
        if _live_pump is None:
            _live_pump = ActivityPump()
        return _live_pump


def reset_live_pump() -> None:
    """Test seam: drop the singleton (stops any running thread)."""
    global _live_pump
    with _live_pump_lock:
        if _live_pump is not None:
            _live_pump.stop()
        _live_pump = None
