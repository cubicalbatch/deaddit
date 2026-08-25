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

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NightlyJob:
    """A declarative recurring-maintenance job registration."""

    id: str
    cron_expression: str
    func: Callable[[], None]
    description: str


#: Intentionally empty today; Dynamics / Agentic Core leads append entries here.
NIGHTLY_JOBS: tuple[NightlyJob, ...] = ()


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
