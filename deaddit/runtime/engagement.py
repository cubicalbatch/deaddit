"""Simulated-voting scheduler for the dedicated worker process.

Polls the ``SIMULATED_VOTING_MODE`` setting and drives the Phase 2/3
engagement engine (:func:`deaddit.dynamics.engagement.run_active_tick`)
once per interval. ``off`` fails closed; ``shadow`` computes decisions and
hourly counters without writes; ``live`` casts canonical
``Vote(source='simulated')`` rows. There is no LLM or agent-run dependency
here: every tick is a bounded, deterministic engine call whose semantics live
beside the engine, and routine votes consume no LLM tokens.

Worker-only by law (plan invariant 2): the web process never imports or
starts this scheduler. It is registered exclusively in
:mod:`deaddit.runtime.scheduler`, started after app creation and recovery,
and stopped before worker exit.

Single simulator instance
-------------------------
The supported deployment runs exactly one ``deaddit-worker`` process, so
this scheduler takes no cross-process claim. Running horizontal workers
against one database is therefore unsupported: ticks would overlap and the
engine's insert-only, prior-voter-guarded casts would turn the overlap into
wasted duplicate work. Supporting horizontal workers requires a DB-backed
claim/lease (for example a lease row carrying the worker id with a
heartbeat expiry) taken before each tick.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime

from flask import Flask

from deaddit.dynamics.engagement import (
    ARCHIVE_CANDIDATE_LIMIT,
    ARCHIVE_ITEM_LIMIT,
    RECENT_COMMENT_LOOKBACK_MINUTES,
    REVIVAL_THREAD_LIMIT,
    REVIVAL_VISIBLE_COMMENT_LIMIT,
    TickResult,
    run_active_tick,
    upsert_hourly_summary,
)
from deaddit.extensions import db
from deaddit.models import Setting, VoteCadencePolicy

logger = logging.getLogger(__name__)

#: Setting key holding the cross-process runtime mode.
MODE_SETTING = "SIMULATED_VOTING_MODE"

#: Modes that do work; ``off`` (and anything invalid or missing) does not.
ACTIVE_MODES = frozenset({"shadow", "live"})

#: Seconds between engagement ticks.
POLL_SECONDS = 20.0

#: Operational tick shape for the worker. The engine owns the semantics
#: (Phase 2/3); these constants only wire the worker's bounded-tick
#: contract. Archive exposure bucketing itself stays inside the engine
#: (``ARCHIVE_BUCKET_MINUTES`` = 60 minutes).
CASTS_PER_TICK = 100
CASTS_PER_ITEM_PER_TICK = 2

#: Engine knobs the worker wires explicitly so its tick shape is visible
#: here without duplicating engine semantics: 60-minute archive bucket
#: candidates, bounded archive items, a 10-minute revival lookback, and at
#: most 50 revival threads per tick.
_TICK_LIMITS: dict[str, int] = {
    "global_limit": CASTS_PER_TICK,
    "per_item_limit": CASTS_PER_ITEM_PER_TICK,
    "archive_candidate_limit": ARCHIVE_CANDIDATE_LIMIT,
    "archive_item_limit": ARCHIVE_ITEM_LIMIT,
    "revival_thread_limit": REVIVAL_THREAD_LIMIT,
    "revival_visible_comment_limit": REVIVAL_VISIBLE_COMMENT_LIMIT,
    "recent_comment_lookback_minutes": RECENT_COMMENT_LOOKBACK_MINUTES,
}

#: Engine skip reasons with a dedicated hourly counter; every other skip
#: reason is a guardrail skip.
_SKIP_COUNTERS = {
    "cap": "cap_skips",
    "min_gap": "min_gap_skips",
    "no_voter": "no_voter_skips",
}


def summary_deltas(result: TickResult) -> dict[str, int]:
    """Map one engine :class:`TickResult` to hourly-summary counter deltas."""
    guardrail_skips = sum(
        count for reason, count in result.skips.items() if reason not in _SKIP_COUNTERS
    )
    guardrail_skips += sum(
        1 for cast in result.casts if cast.get("status") == "rejected"
    )
    deltas = {
        "ticks": 1,
        "active_proposals": result.active_proposals,
        "archive_proposals": result.archive_proposals,
        "revival_proposals": result.revival_proposals,
        "inserted_votes": sum(
            1 for cast in result.casts if cast.get("change_kind") == "insert"
        ),
        "switched_votes": sum(
            1 for cast in result.casts if cast.get("change_kind") == "direction_switch"
        ),
        "upvotes": sum(1 for d in result.decisions if d.direction > 0),
        "downvotes": sum(1 for d in result.decisions if d.direction < 0),
        "guardrail_skips": guardrail_skips,
    }
    for counter in _SKIP_COUNTERS.values():
        deltas[counter] = 0
    for reason, counter in _SKIP_COUNTERS.items():
        deltas[counter] += result.skips.get(reason, 0)
    return deltas


class EngagementScheduler:
    """Daemon poller that runs simulated-voting ticks under the current mode.

    Patterned after :class:`deaddit.runtime.wakes.WakeScheduler`. The mode is
    re-read from the ``Setting`` table on every tick — never through the
    process-local settings cache — so an admin change is observed by the
    next poll without a worker restart. Tick failures are rolled back,
    recorded, and logged; they never kill the poller thread or any sibling
    worker component.
    """

    def __init__(
        self,
        app: Flask,
        *,
        poll_seconds: float = POLL_SECONDS,
        clock: Callable[[], datetime] = datetime.utcnow,
    ) -> None:
        self.app = app
        self._poll_seconds = float(poll_seconds)
        self._clock = clock
        self._stop_event = threading.Event()
        self._poller_thread: threading.Thread | None = None
        self._no_policy_warned = False
        self._warned_raw_modes: set[str] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the daemon engagement-poller thread."""
        if self._poller_thread is not None:
            raise RuntimeError("EngagementScheduler already started")
        self._poller_thread = threading.Thread(
            target=self._poll_loop, name="engagement-poller", daemon=True
        )
        self._poller_thread.start()

    def stop(self, wait: bool = True) -> None:
        """Stop the poller; safe to call more than once."""
        self._stop_event.set()
        if self._poller_thread is not None:
            self._poller_thread.join(timeout=10)
            self._poller_thread = None

    # ------------------------------------------------------------------
    # Poller
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        logger.info("Engagement poller loop started (poll=%.1fs)", self._poll_seconds)
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                self._poll_once()
            except Exception:
                # _tick_once isolates its own failures; this guard exists so
                # nothing can ever kill the poller thread.
                logger.exception("Engagement poll iteration failed")
                db.session.rollback()
            elapsed = time.monotonic() - started
            self._stop_event.wait(max(self._poll_seconds - elapsed, 0.0))

    def _poll_once(self) -> None:
        with self.app.app_context():
            self._tick_once()

    def _tick_once(self) -> None:
        """Resolve the mode fresh from the database and run one bounded tick."""
        mode = self._resolve_mode()
        if mode == "off":
            return
        if not self._policy_exists():
            self._warn_no_policy(mode)
            return
        self._no_policy_warned = False
        try:
            result = run_active_tick(
                None,
                self._clock(),
                dry_run=mode == "shadow",
                **_TICK_LIMITS,
            )
        except Exception:
            db.session.rollback()
            logger.exception("Simulated-voting tick failed (mode=%s)", mode)
            self._record_error(mode)
            return
        self._record_summary(mode, result)

    # ------------------------------------------------------------------
    # Mode and policy resolution
    # ------------------------------------------------------------------

    def _resolve_mode(self) -> str:
        """Read the mode straight from the Setting table every tick.

        Missing, empty, or invalid values fail closed to ``off``. Distinct
        invalid values are warned about once each so a bad value cannot
        spam the log every 20 seconds.
        """
        raw = Setting.get_value(MODE_SETTING)
        mode = (raw or "").strip().lower()
        if mode == "off" or mode in ACTIVE_MODES:
            return mode
        if raw and raw not in self._warned_raw_modes:
            self._warned_raw_modes.add(raw)
            logger.warning(
                "Ignoring invalid %s value; failing closed to 'off'",
                MODE_SETTING,
            )
        return "off"

    def _policy_exists(self) -> bool:
        return db.session.query(VoteCadencePolicy.id).limit(1).first() is not None

    def _warn_no_policy(self, mode: str) -> None:
        if self._no_policy_warned:
            return
        self._no_policy_warned = True
        logger.warning(
            "SIMULATED_VOTING_MODE=%s requires a saved cadence policy; failing "
            "closed to no work until one exists",
            mode,
        )

    # ------------------------------------------------------------------
    # Hourly summary persistence
    # ------------------------------------------------------------------

    def _record_summary(self, mode: str, result: TickResult) -> None:
        try:
            upsert_hourly_summary(self._clock(), mode, summary_deltas(result))
        except Exception:
            db.session.rollback()
            logger.exception(
                "Failed to persist simulated-voting hourly summary (mode=%s)",
                mode,
            )

    def _record_error(self, mode: str) -> None:
        try:
            upsert_hourly_summary(self._clock(), mode, errors=1)
        except Exception:
            db.session.rollback()
            logger.exception(
                "Failed to persist simulated-voting tick error (mode=%s)",
                mode,
            )
