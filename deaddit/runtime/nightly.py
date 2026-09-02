"""Single registration home for nightly / recurring maintenance jobs.

Roadmap Resolution 10: every recurring maintenance job is declared here as a
:data:`NIGHTLY_JOBS` entry and registered onto the worker's APScheduler by
:func:`register_nightly_jobs`. The web process never registers recurring jobs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from apscheduler.schedulers.base import BaseScheduler
from flask import current_app

from deaddit.dynamics.degeneracy import run_nightly_scans
from deaddit.dynamics.inbox import purge_read_notifications
from deaddit.dynamics.karma import recompute_scores_and_karma
from deaddit.dynamics.metrics import run_nightly_rollup

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NightlyJob:
    """A declarative recurring-maintenance job registration."""

    id: str
    cron_expression: str
    func: Callable[[], None]
    description: str


#: Recurring maintenance jobs (Dynamics D1 Wave B appends here).
NIGHTLY_JOBS: tuple[NightlyJob, ...] = (
    NightlyJob(
        id="dynamics-recompute",
        cron_expression="30 3 * * *",
        func=recompute_scores_and_karma,
        description=(
            "Repair vote-authoritative post/comment scores and rebuild user karma"
        ),
    ),
    NightlyJob(
        id="dynamics-notification-purge",
        cron_expression="45 3 * * *",
        func=purge_read_notifications,
        description="Purge read notifications older than 90 days",
    ),
    NightlyJob(
        id="dynamics-platform-rollup",
        cron_expression="55 3 * * *",
        func=run_nightly_rollup,
        description=(
            "Fold yesterday's ActivityEvents, LLM spend, and health trio "
            "into the PlatformDaily rollup row"
        ),
    ),
    NightlyJob(
        id="dynamics-degeneracy-scan",
        cron_expression="05 4 * * *",
        func=run_nightly_scans,
        description=(
            "Nightly echo-chamber (participation Gini) and brigading "
            "(voter-overlap) detection into the degeneracy watchlist"
        ),
    ),
)


def register_nightly_jobs(scheduler: BaseScheduler) -> list[str]:
    """Register every :data:`NIGHTLY_JOBS` entry on ``scheduler``.

    Each job function is wrapped so it runs inside the current application
    context. Requires an active app context. Returns registered job ids.
    """
    app = current_app._get_current_object()
    registered: list[str] = []
    for nightly in NIGHTLY_JOBS:

        def _run(nightly=nightly, app=app) -> None:
            with app.app_context():
                nightly.func()

        scheduler.add_job(
            _run,
            "cron",
            id=nightly.id,
            replace_existing=True,
            **_cron_kwargs(nightly.cron_expression),
        )
        logger.info(
            "Registered nightly job %s (%s): %s",
            nightly.id,
            nightly.cron_expression,
            nightly.description,
        )
        registered.append(nightly.id)
    return registered


def _cron_kwargs(cron_expression: str) -> dict[str, Any]:
    """Parse a basic 5-field cron expression into APScheduler kwargs."""
    parts = cron_expression.split()
    if len(parts) != 5:
        raise ValueError(
            "Cron expression must have 5 parts: minute hour day month day_of_week"
        )

    minute, hour, day, month, day_of_week = parts

    kwargs = {}
    if minute != "*":
        kwargs["minute"] = minute
    if hour != "*":
        kwargs["hour"] = hour
    if day != "*":
        kwargs["day"] = day
    if month != "*":
        kwargs["month"] = month
    if day_of_week != "*":
        kwargs["day_of_week"] = day_of_week

    return kwargs
