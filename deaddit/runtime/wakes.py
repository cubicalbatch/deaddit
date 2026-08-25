"""Agent wake scheduling for the dedicated worker process.

Polls the ``agent`` table for due agents and launches
:func:`deaddit.agents.loop.run_once` under global-concurrency and
per-agent daily-request budgets. Also performs boot recovery: stale
``running`` runs are marked interrupted and enabled agents with no
scheduled wake are armed.

Worker-only by law (A5): nothing here ever runs in the web process.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from flask import Flask

from deaddit.agents.loop import is_runtime_enabled, run_once
from deaddit.extensions import db
from deaddit.models import Agent, AgentRun, AgentTurn, Setting

logger = logging.getLogger(__name__)

#: Seconds between wake-poll ticks.
POLL_SECONDS = 20.0

#: Fallback for ``max_run_seconds`` when absent/invalid in agent.config.
FALLBACK_MAX_RUN_SECONDS = 300

#: Grace beyond max_run_seconds before a running run counts as stale.
RUN_GRACE_SECONDS = 60

#: When the daily request ceiling is hit, defer the agent this many seconds.
CEILING_DEFER_SECONDS = 1800

#: Backoff applied after a crashed wake so failures do not hot-loop.
FAILURE_BACKOFF_SECONDS = 300

#: Default global concurrency when AGENT_MAX_CONCURRENT_RUNS is unset.
DEFAULT_MAX_CONCURRENT_RUNS = 2


def _int_config(config: dict | None, key: str, fallback: int) -> int:
    try:
        return int((config or {}).get(key, fallback))
    except (TypeError, ValueError):
        return fallback


class WakeScheduler:
    """Daemon poller that launches due agents within their budgets."""

    def __init__(self, app: Flask) -> None:
        self.app = app
        self._poll_seconds = POLL_SECONDS
        self._stop_event = threading.Event()
        self._poller_thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._semaphore: threading.BoundedSemaphore | None = None
        self._pool_size = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the daemon wake-poller thread."""
        if self._poller_thread is not None:
            raise RuntimeError("WakeScheduler already started")
        self._poller_thread = threading.Thread(
            target=self._poll_loop, name="agent-wake-poller", daemon=True
        )
        self._poller_thread.start()

    def stop(self, wait: bool = True) -> None:
        """Stop the poller and shut down the wake executor."""
        self._stop_event.set()
        if self._poller_thread is not None:
            self._poller_thread.join(timeout=10)
            self._poller_thread = None
        if self._executor is not None:
            self._executor.shutdown(wait=wait)
            self._executor = None
            self._semaphore = None
            self._pool_size = 0

    # ------------------------------------------------------------------
    # Boot recovery (call once inside app_context, before start())
    # ------------------------------------------------------------------

    def recover(self) -> tuple[int, int]:
        """Crash-recover stale runs; arm enabled agents when the flag allows.

        Stale-run hygiene always runs regardless of AGENT_RUNTIME_ENABLED;
        arming only happens with the flag on.

        Returns ``(interrupted_runs, armed_agents)``.
        """
        now = datetime.utcnow()
        interrupted = self._interrupt_stale_runs(now)

        armed = 0
        if is_runtime_enabled():
            due = Agent.query.filter(
                Agent.is_enabled.is_(True), Agent.next_run_at.is_(None)
            ).all()
            for agent in due:
                agent.next_run_at = now
                armed += 1
            if armed:
                db.session.commit()
                logger.info("Armed %d enabled agent(s) with no scheduled wake", armed)

        return interrupted, armed

    def _interrupt_stale_runs(self, now: datetime) -> int:
        """Interrupt runs past their wall-clock budget + grace; free their agents.

        Runs after a hard kill leave ``AgentRun.status='running'`` and the
        agent parked in ``status='running'``; without this sweep such an
        agent would stall until a restart. Runs every poll tick, so a killed
        run self-heals within one cycle of the grace window elapsing.
        """
        rows = (
            db.session.query(AgentRun, Agent)
            .join(Agent, AgentRun.agent_id == Agent.id)
            .filter(AgentRun.status == "running")
            .all()
        )
        interrupted = 0
        for run, agent in rows:
            budget = (
                _int_config(agent.config, "max_run_seconds", FALLBACK_MAX_RUN_SECONDS)
                + RUN_GRACE_SECONDS
            )
            if run.started_at is not None and run.started_at < now - timedelta(
                seconds=budget
            ):
                run.status = "interrupted"
                run.finished_at = now
                run.error_message = (
                    "Recovered: run exceeded wall-clock budget plus grace."
                )
                if agent.status == "running":
                    agent.status = "idle"
                interrupted += 1
        if interrupted:
            db.session.commit()
            logger.info("Interrupted %d stale agent run(s)", interrupted)
        return interrupted


    # ------------------------------------------------------------------
    # Poller
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        logger.info("Wake poller loop started (poll=%.1fs)", self._poll_seconds)
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                self._poll_once()
            except Exception:
                logger.exception("Wake poll iteration failed")
            elapsed = time.monotonic() - started
            self._stop_event.wait(max(self._poll_seconds - elapsed, 0.0))

    def _poll_once(self) -> None:
        with self.app.app_context():
            # Crash hygiene runs regardless of the runtime flag.
            self._interrupt_stale_runs(datetime.utcnow())

            if not is_runtime_enabled():
                return

            self._ensure_pool()

            now = datetime.utcnow()
            candidates = Agent.query.filter(
                Agent.is_enabled.is_(True),
                Agent.status != "running",
                Agent.next_run_at.isnot(None),
                Agent.next_run_at <= now,
            ).order_by(Agent.next_run_at.asc())
            if candidates.count() == 0:
                return

            logger.debug("Wake tick: %d due agent(s)", candidates.count())

            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            for agent in candidates.all():
                if self._stop_event.is_set():
                    break

                ceiling = _int_config(agent.config, "daily_request_ceiling", 0)
                if ceiling > 0:
                    used = (
                        db.session.query(AgentTurn.id)
                        .join(AgentRun, AgentTurn.run_id == AgentRun.id)
                        .filter(
                            AgentRun.agent_id == agent.id,
                            AgentRun.started_at >= start_of_day,
                        )
                        .count()
                    )
                    if used >= ceiling:
                        agent.next_run_at = now + timedelta(
                            seconds=CEILING_DEFER_SECONDS
                        )
                        db.session.commit()
                        logger.info(
                            "Agent %s hit daily ceiling (%d requests); "
                            "deferred %ds",
                            agent.user_username,
                            used,
                            CEILING_DEFER_SECONDS,
                        )
                        continue

                assert self._semaphore is not None
                if not self._semaphore.acquire(blocking=False):
                    # At capacity: remaining candidates stay due and get
                    # picked up next tick.
                    break
                self._executor.submit(self._run_agent, agent.user_username)  # type: ignore[union-attr]

    def _ensure_pool(self) -> None:
        """(Re)build the semaphore + executor when concurrency changes."""
        try:
            size = int(
                Setting.get_value(
                    "AGENT_MAX_CONCURRENT_RUNS", DEFAULT_MAX_CONCURRENT_RUNS
                )
                or DEFAULT_MAX_CONCURRENT_RUNS
            )
        except (TypeError, ValueError):
            size = DEFAULT_MAX_CONCURRENT_RUNS
        size = max(size, 1)

        if self._executor is not None and self._pool_size == size:
            return

        if self._executor is not None:
            self._executor.shutdown(wait=True)
        self._executor = ThreadPoolExecutor(
            max_workers=size, thread_name_prefix="agent-wake"
        )
        self._semaphore = threading.BoundedSemaphore(size)
        self._pool_size = size
        logger.info("Wake pool sized to %d concurrent agent run(s)", size)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _run_agent(self, username: str) -> None:
        """Run one scheduled visit; release the slot afterwards."""
        try:
            with self.app.app_context():
                run_once(username, trigger="schedule")
        except Exception:
            logger.exception("Scheduled wake for agent %s failed", username)
            try:
                with self.app.app_context():
                    agent = Agent.query.filter_by(user_username=username).first()
                    if agent is not None:
                        agent.next_run_at = datetime.utcnow() + timedelta(
                            seconds=FAILURE_BACKOFF_SECONDS
                        )
                        db.session.commit()
            except Exception:
                logger.exception(
                    "Failed to back off agent %s after failed wake", username
                )
                db.session.rollback()
        finally:
            semaphore = self._semaphore
            if semaphore is not None:
                semaphore.release()
