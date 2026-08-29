"""Standalone worker entrypoint: ``deaddit-worker``.

Owns ALL background job execution: crash-recovery sweep at startup, nightly
maintenance registrations, and the polling JobRunner. The web process starts
none of this.
"""

from __future__ import annotations

import logging
import signal
import threading

from apscheduler.schedulers.background import BackgroundScheduler

from deaddit import create_app
from deaddit.runtime.claim import sweep_stale_jobs
from deaddit.runtime.engagement import EngagementScheduler
from deaddit.runtime.nightly import register_nightly_jobs
from deaddit.runtime.runner import JobRunner
from deaddit.runtime.wakes import WakeScheduler

logger = logging.getLogger(__name__)


def main() -> None:
    app = create_app()

    with app.app_context():
        recovered = sweep_stale_jobs()
        logger.info("Startup sweep returned %d stale job(s) to pending", recovered)

    wakes = WakeScheduler(app)
    with app.app_context():
        interrupted_runs, armed_agents = wakes.recover()

    engagement = EngagementScheduler(app)

    scheduler = BackgroundScheduler()
    with app.app_context():
        registered = register_nightly_jobs(scheduler)

    runner = JobRunner(app)
    runner.start()
    wakes.start()
    scheduler.start()
    engagement.start()

    logger.info(
        "deaddit worker started: worker_id=%s recovered=%d nightly_jobs=%d "
        "stale_agent_runs_interrupted=%d agents_armed=%d",
        runner.worker_id,
        recovered,
        len(registered),
        interrupted_runs,
        armed_agents,
    )

    shutdown = threading.Event()

    def _handle_signal(signum, _frame) -> None:
        logger.info("Received %s, shutting down worker", signal.Signals(signum).name)
        shutdown.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        while not shutdown.wait(timeout=60):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        wakes.stop(wait=True)
        engagement.stop(wait=True)
        runner.stop(wait=True)
        scheduler.shutdown(wait=True)

    logger.info("deaddit worker stopped cleanly")


if __name__ == "__main__":
    main()
