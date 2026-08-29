"""Phase 4: EngagementScheduler lifecycle, modes, and recovery contracts.

Covers the worker-side simulated-voting scheduler only: mode resolution
straight from the Setting table, shadow/live tick parity, hourly summary
persistence, failure isolation, worker-entrypoint wiring, restart safety,
and bounded downtime catch-up. The web process never schedules any of it.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event

from deaddit import db as _db
from deaddit.dynamics.engagement import TickResult, preset_config
from deaddit.extensions import db
from deaddit.models import (
    Job,
    JobStatus,
    JobType,
    Post,
    Setting,
    Subdeaddit,
    User,
    Vote,
    VoteCadencePolicy,
    VoteSimulationHourly,
)
from deaddit.runtime import engagement as runtime_engagement
from deaddit.runtime.engagement import (
    CASTS_PER_ITEM_PER_TICK,
    CASTS_PER_TICK,
    MODE_SETTING,
    POLL_SECONDS,
    EngagementScheduler,
    summary_deltas,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: Fixed tick clock so every decision is deterministic.
NOW = datetime(2026, 5, 1, 12, 0, 0)
NOW_HOUR = NOW.replace(minute=0, second=0, microsecond=0)

_MISSING = object()


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Busy-wait for a condition; avoids sleep-based races."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _db.session.expire_all()
        if predicate():
            return True
        time.sleep(0.02)
    _db.session.expire_all()
    return bool(predicate())


def _set_mode(value: object) -> None:
    """Write (or delete) the mode setting the way the admin process would."""
    if value is _MISSING:
        db.session.query(Setting).filter(Setting.key == MODE_SETTING).delete()
    else:
        Setting.set_value(MODE_SETTING, str(value))
    db.session.commit()


def _add_policy(preset: str = "busy") -> None:
    db.session.add(
        VoteCadencePolicy(
            preset=preset,
            algorithm_version=1,
            config=preset_config(preset),
            effective_at=NOW - timedelta(days=1),
        )
    )
    db.session.commit()


def _seed_world(
    *,
    voters: int = 6,
    posts: int = 1,
    created_minutes_ago: int = 30,
    preset: str = "busy",
    with_policy: bool = True,
    silenced: bool = False,
) -> list[Post]:
    """Deterministic world: one author, N fresh voters, N posts, one policy."""
    db.session.add_all([Subdeaddit(name="simsub"), User(username="author")])
    users = [
        User(username=f"voter-{i:03d}", agent_state={"subscriptions": ["simsub"]})
        for i in range(voters)
    ]
    if silenced:
        users.append(
            User(
                username="silenced",
                agent_state={"subscriptions": ["simsub"], "rate_caps": {"vote": 0}},
            )
        )
    db.session.add_all(users)
    db.session.commit()
    fresh = []
    for index in range(posts):
        post = Post(
            title=f"target {index}",
            created_at=NOW - timedelta(minutes=created_minutes_ago),
            user="author",
            subdeaddit_name="simsub",
        )
        db.session.add(post)
        db.session.commit()
        fresh.append(post)
    if with_policy:
        _add_policy(preset)
    return fresh


def _scheduler(app, *, poll_seconds: float = POLL_SECONDS, clock=lambda: NOW):
    return EngagementScheduler(app, poll_seconds=poll_seconds, clock=clock)


def _hourly(mode: str, hour: datetime = NOW_HOUR) -> VoteSimulationHourly | None:
    _db.session.expire_all()
    return db.session.get(VoteSimulationHourly, (hour, mode))


def _simulated_votes() -> list[Vote]:
    _db.session.expire_all()
    return list(db.session.query(Vote).filter(Vote.source == "simulated").all())


@contextmanager
def _captured_statements():
    """Collect every SQL statement hitting this app's engine."""
    statements: list[str] = []

    def _record(_conn, _cursor, statement, _params, _context, _executemany):
        statements.append(statement)

    engine = db.session.get_bind()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", _record)


# ---------------------------------------------------------------------------
# Web process never schedules simulator activity
# ---------------------------------------------------------------------------


def test_create_app_leaves_no_engagement_thread(app):
    """The web app factory must not start (or even know) the scheduler."""
    assert not [t for t in threading.enumerate() if t.name == "engagement-poller"]


_WEB_NO_ENGAGEMENT_SCRIPT = """
import sys
import threading


def main():
    import deaddit.wsgi  # noqa: F401  web entrypoint under test

    for module in ("deaddit.runtime.engagement", "deaddit.runtime.scheduler"):
        assert module not in sys.modules, f"web process imported {module}"
    names = [t.name for t in threading.enumerate()]
    assert "engagement-poller" not in names, names
    print("OK")


main()
"""


def test_web_entrypoint_imports_no_engagement(tmp_path):
    """Importing the WSGI entrypoint neither imports nor starts the simulator."""
    proc = subprocess.run(
        [sys.executable, "-c", _WEB_NO_ENGAGEMENT_SCRIPT],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            # Keep the real instance database untouched by create_app().
            "DEADDIT_DB_PATH": str(tmp_path / "web-check.db"),
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


# ---------------------------------------------------------------------------
# Modes: off / missing / invalid fail closed with no work
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["off", "OFF", " off ", _MISSING, "", "sometimes", None],
    ids=["off", "uppercase", "padded", "missing", "empty", "invalid", "null"],
)
def test_non_live_modes_do_no_work_beyond_mode_resolution(app, db_session, raw):
    _seed_world()  # candidates and a policy exist; they must not be touched
    _set_mode(raw)
    scheduler = _scheduler(app)

    with _captured_statements() as statements:
        scheduler._tick_once()

    assert statements, "mode resolution must hit the database each tick"
    assert all("FROM setting" in s for s in statements), statements
    assert _simulated_votes() == []
    assert _hourly("live") is None and _hourly("shadow") is None


def test_invalid_mode_value_warned_once_per_value(app, db_session, caplog):
    _seed_world()
    _set_mode("sometimes")
    scheduler = _scheduler(app)
    with caplog.at_level(logging.WARNING, logger="deaddit.runtime.engagement"):
        scheduler._tick_once()
        scheduler._tick_once()
        _set_mode("nope")
        scheduler._tick_once()
        scheduler._tick_once()
    warnings = [
        r
        for r in caplog.records
        if r.name == "deaddit.runtime.engagement" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 2, [r.message for r in warnings]
    assert all("SIMULATED_VOTING_MODE" in r.message for r in warnings)
    assert _simulated_votes() == []


def test_mode_is_reread_from_database_every_tick(app, db_session):
    """An admin mode flip applies on the next tick — no restart, no cache."""
    _seed_world()
    _set_mode("off")
    scheduler = _scheduler(app)
    scheduler._tick_once()
    assert _simulated_votes() == []

    _set_mode("live")
    scheduler._tick_once()
    assert _simulated_votes(), "live mode flip was not observed without restart"

    _set_mode("off")
    votes_before = len(_simulated_votes())
    scheduler._tick_once()
    assert len(_simulated_votes()) == votes_before
    assert _hourly("live").ticks == 1



def test_summary_deltas_expects_tail_probability_non_conversions():
    """Failed exposure rolls are ordinary non-events, not guardrail skips."""
    result = TickResult()
    for _ in range(160):
        result.skip("tail_probability")
    result.skip("prior_voter")
    result.skip("global_limit")
    result.skip("cap")
    result.skip("no_voter")
    result.casts.append({"status": "rejected"})

    deltas = summary_deltas(result)

    assert deltas["guardrail_skips"] == 3  # prior_voter + global_limit + rejected cast
    assert deltas["cap_skips"] == 1
    assert deltas["no_voter_skips"] == 1
    assert deltas["min_gap_skips"] == 0

# ---------------------------------------------------------------------------
# Shadow vs live: identical decisions, only live mutates votes
# ---------------------------------------------------------------------------


def test_shadow_and_live_decisions_match_only_live_writes(app, db_session, monkeypatch):
    _seed_world(voters=6, posts=1, created_minutes_ago=360)
    _set_mode("live")
    real_tick = runtime_engagement.run_active_tick
    ticks: list[tuple[bool, object]] = []

    def recording_tick(*args, **kwargs):
        result = real_tick(*args, **kwargs)
        ticks.append((kwargs.get("dry_run", False), result))
        return result

    monkeypatch.setattr(runtime_engagement, "run_active_tick", recording_tick)

    _set_mode("shadow")
    shadow_scheduler = _scheduler(app)
    shadow_scheduler._tick_once()
    shadow_dry, shadow_result = ticks[-1]
    assert shadow_dry is True
    assert shadow_result.decisions, "fixture should yield at least one decision"
    assert _simulated_votes() == [], "shadow mode must not cast votes"
    assert summary_deltas(shadow_result)["inserted_votes"] == 0

    # Same state, same clock: live must reach the identical decisions.
    _set_mode("live")
    _scheduler(app)._tick_once()
    live_dry, live_result = ticks[-1]
    assert live_dry is False
    assert [
        (d.target_type, d.target_id, d.voter, d.direction)
        for d in shadow_result.decisions
    ] == [
        (d.target_type, d.target_id, d.voter, d.direction)
        for d in live_result.decisions
    ]
    votes = _simulated_votes()
    assert len(votes) == len(live_result.decisions)
    assert {v.voter for v in votes} == {d.voter for d in live_result.decisions}


def test_hourly_summary_visible_from_separate_context_without_payload(
    app, db_session, monkeypatch
):
    _seed_world(voters=6, posts=1, created_minutes_ago=360)
    _set_mode("live")
    _scheduler(app)._tick_once()
    votes = _simulated_votes()
    assert votes

    # The summary carries counters only — no per-decision or persona data.
    columns = set(VoteSimulationHourly.__table__.columns.keys())
    assert columns == {
        "hour",
        "mode",
        "updated_at",
        "ticks",
        "errors",
        "active_proposals",
        "archive_proposals",
        "revival_proposals",
        "inserted_votes",
        "switched_votes",
        "upvotes",
        "downvotes",
        "cap_skips",
        "min_gap_skips",
        "no_voter_skips",
        "guardrail_skips",
    }

    # A fresh app context (the web process reader) sees the row.
    with app.app_context():
        row = db.session.get(VoteSimulationHourly, (NOW_HOUR, "live"))
        assert row is not None
        assert row.ticks == 1
        assert row.errors == 0
        assert row.inserted_votes == len(votes)
        assert row.upvotes + row.downvotes == len(votes)
        assert row.active_proposals >= len(votes)


# ---------------------------------------------------------------------------
# Fail-closed: shadow/live without a saved cadence policy
# ---------------------------------------------------------------------------


def test_active_mode_without_policy_fails_closed_and_logs_once(app, db_session, caplog):
    _seed_world(with_policy=False)
    _set_mode("live")
    scheduler = _scheduler(app)
    with caplog.at_level(logging.WARNING, logger="deaddit.runtime.engagement"):
        for _ in range(3):
            with _captured_statements() as statements:
                scheduler._tick_once()
    warnings = [r for r in caplog.records if "cadence policy" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    # Queries stay bounded to mode resolution plus the policy probe.
    assert all(
        "FROM setting" in s or "FROM vote_cadence_policy" in s for s in statements
    ), statements
    assert _simulated_votes() == []
    assert _hourly("live") is None

    # Saving a policy unblocks the very next tick.
    _add_policy()
    scheduler._tick_once()
    assert _hourly("live") is not None
    assert _hourly("live").ticks == 1


# ---------------------------------------------------------------------------
# Lifecycle: start/stop exactly once
# ---------------------------------------------------------------------------


def test_start_stop_lifecycle(app, db_session):
    _set_mode("off")
    scheduler = _scheduler(app)
    scheduler.start()
    threads = [t for t in threading.enumerate() if t.name == "engagement-poller"]
    assert len(threads) == 1
    with pytest.raises(RuntimeError):
        scheduler.start()
    scheduler.stop(wait=True)
    assert scheduler._poller_thread is None
    scheduler.stop(wait=True)  # idempotent
    assert not [t for t in threading.enumerate() if t.name == "engagement-poller"]


def test_worker_main_starts_and_stops_engagement_exactly_once(app, monkeypatch):
    """The worker entrypoint owns the simulator: start once, stop once."""
    from deaddit.runtime import scheduler as worker

    calls: list[str] = []
    started = threading.Event()

    class RecordingEngagement(EngagementScheduler):
        def start(self) -> None:
            calls.append("engagement.start")
            super().start()
            started.set()

        def stop(self, wait: bool = True) -> None:
            calls.append("engagement.stop")
            super().stop(wait=wait)

    class StubWake:
        def __init__(self, app): ...

        def recover(self):
            return 0, 0

        def start(self):
            calls.append("wakes.start")

        def stop(self, wait=True):
            calls.append("wakes.stop")

    class StubRunner:
        def __init__(self, app):
            self.worker_id = "test-worker"

        def start(self):
            calls.append("runner.start")

        def stop(self, wait=True):
            calls.append("runner.stop")

    class StubApscheduler:
        def start(self):
            calls.append("aps.start")

        def shutdown(self, wait=True):
            calls.append("aps.shutdown")

    monkeypatch.setattr(worker, "create_app", lambda: app)
    monkeypatch.setattr(worker, "sweep_stale_jobs", lambda: 0)
    monkeypatch.setattr(worker, "WakeScheduler", StubWake)
    monkeypatch.setattr(worker, "register_nightly_jobs", lambda s: [])
    monkeypatch.setattr(worker, "JobRunner", StubRunner)
    monkeypatch.setattr(worker, "BackgroundScheduler", StubApscheduler)
    monkeypatch.setattr(worker, "EngagementScheduler", RecordingEngagement)

    handlers: dict[int, object] = {}

    def fake_signal(signum, handler):
        handlers[signum] = handler
        return None

    monkeypatch.setattr(signal, "signal", fake_signal)

    main_thread = threading.Thread(target=worker.main, name="worker-main")
    main_thread.start()
    try:
        assert started.wait(timeout=5.0), "engagement scheduler never started"
        assert (
            len([t for t in threading.enumerate() if t.name == "engagement-poller"])
            == 1
        )
        deadline = time.monotonic() + 5.0
        while signal.SIGTERM not in handlers and time.monotonic() < deadline:
            time.sleep(0.02)
        assert signal.SIGTERM in handlers, "signal handlers were never installed"
        handlers[signal.SIGTERM](signal.SIGTERM, None)
    finally:
        main_thread.join(timeout=10.0)
    assert not main_thread.is_alive(), "worker main() did not shut down"

    assert calls == [
        "runner.start",
        "wakes.start",
        "aps.start",
        "engagement.start",
        "wakes.stop",
        "engagement.stop",
        "runner.stop",
        "aps.shutdown",
    ]
    assert calls.count("engagement.start") == 1
    assert calls.count("engagement.stop") == 1
    assert not [t for t in threading.enumerate() if t.name == "engagement-poller"]


# ---------------------------------------------------------------------------
# Failure isolation and recovery
# ---------------------------------------------------------------------------


def test_forced_tick_exception_isolates_and_later_ticks_run(tmp_path, monkeypatch):
    """A crashing tick spares JobRunner, WakeScheduler, APScheduler, and itself.

    Runs against a file-backed database: unlike the shared in-memory
    connection, it gives each component session its own connection, which is
    what the real multi-threaded worker uses.
    """
    from apscheduler.schedulers.background import BackgroundScheduler

    from deaddit import create_app as _create_app
    from deaddit import jobs
    from deaddit.runtime.runner import JobRunner
    from deaddit.runtime.wakes import WakeScheduler

    isolation_app = _create_app(
        {
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path}/isolation.db",
            "TESTING": True,
        }
    )
    with isolation_app.app_context():
        _db.create_all()
        _seed_world(voters=6, posts=1)
        _set_mode("live")

        job = Job(
            type=JobType.BATCH_OPERATION,
            status=JobStatus.PENDING,
            priority=5,
            total_items=1,
            parameters={"count": 1},
        )
        db.session.add(job)
        db.session.commit()
        job_id = job.id
        executions: list[int] = []

        def counting_batch(job_row):
            executions.append(job_row.id)
            return {"batch_results": [], "count": 0}

        monkeypatch.setattr(jobs, "_execute_batch_operation", counting_batch)

        real_tick = runtime_engagement.run_active_tick
        failures = {"remaining": 3}

        def exploding_tick(*args, **kwargs):
            if failures["remaining"] > 0:
                failures["remaining"] -= 1
                raise RuntimeError("forced tick failure")
            return real_tick(*args, **kwargs)

        monkeypatch.setattr(runtime_engagement, "run_active_tick", exploding_tick)
        monkeypatch.setenv("DEADDIT_WORKER_POLL_SECONDS", "0.05")

        runner = JobRunner(isolation_app)
        wakes = WakeScheduler(isolation_app)
        aps = BackgroundScheduler()
        scheduler = _scheduler(isolation_app, poll_seconds=0.02)
        runner.start()
        wakes.start()
        aps.start()
        scheduler.start()
        try:
            # The forced exceptions must not kill any sibling component.
            assert _wait_until(
                lambda: _hourly("live") is not None and _hourly("live").errors == 3
            ), "failing ticks did not record errors"
            assert scheduler._poller_thread.is_alive()
            assert wakes._poller_thread.is_alive()
            assert runner._poller_thread.is_alive()
            assert aps.running

            # Recovery: once the engine works again, ticks resume in-process.
            assert _wait_until(
                lambda: _hourly("live").ticks >= 1 and _simulated_votes()
            ), "engagement ticks did not resume after failures"
            assert _wait_until(lambda: executions == [job_id]), "JobRunner stalled"
        finally:
            scheduler.stop(wait=True)
            wakes.stop(wait=True)
            runner.stop(wait=True)
            aps.shutdown(wait=True)

        assert db.session.get(Job, job_id).status == JobStatus.COMPLETED
        assert _hourly("live").errors == 3
        assert _hourly("live").ticks >= 1
        _db.session.remove()
        _db.drop_all()


# ---------------------------------------------------------------------------
# Restart safety and bounded downtime catch-up
# ---------------------------------------------------------------------------


def test_restart_after_partial_progress_duplicates_nothing(app, db_session):
    _seed_world(voters=8, posts=1, created_minutes_ago=360)
    _set_mode("live")
    seen: list[tuple[str, int]] = []
    for tick in range(6):
        scheduler = _scheduler(app)  # fresh instance: a simulated restart
        scheduler._tick_once()
        votes = _simulated_votes()
        pairs = [(v.voter, v.post_id) for v in votes]
        assert len(pairs) == len(set(pairs)), f"duplicate vote after tick {tick}"
        assert set(pairs) >= set(seen)
        seen = pairs
    assert seen, "fixture should have produced votes"
    assert _hourly("live").ticks == 6


def test_downtime_catchup_bounded_by_all_limit_classes(app, db_session):
    _seed_world(
        voters=110,
        posts=110,
        created_minutes_ago=360,
        preset="quiet",
        silenced=True,
    )
    _set_mode("live")
    scheduler = _scheduler(app)

    # First tick after "downtime": a large backlog exists.
    scheduler._tick_once()
    votes = _simulated_votes()
    assert len(votes) == CASTS_PER_TICK, "global per-tick cast limit was exceeded"
    row = _hourly("live")
    assert row.ticks == 1
    assert row.guardrail_skips >= 1, "global limit skips were not recorded"

    per_post: dict[int, int] = {}
    per_voter: dict[str, int] = {}
    for vote in votes:
        per_post[vote.post_id] = per_post.get(vote.post_id, 0) + 1
        per_voter[vote.voter] = per_voter.get(vote.voter, 0) + 1
    # Per-item limit: at most two new votes per item per tick.
    assert per_post and max(per_post.values()) <= CASTS_PER_ITEM_PER_TICK
    # Per-persona limits: hourly caps and minimum gaps bound each persona.
    assert max(per_voter.values()) == 1, "a persona voted twice within one tick"
    assert "author" not in per_voter
    assert "silenced" not in per_voter, "rate-capped persona was not excluded"

    # Catch-up continues on later ticks without ever exceeding the bounds.
    for _ in range(2):
        _scheduler(app, clock=lambda: NOW + timedelta(minutes=5))._tick_once()
    votes = _simulated_votes()
    assert len(votes) <= 3 * CASTS_PER_TICK
    per_post = {}
    per_voter = {}
    for vote in votes:
        per_post[vote.post_id] = per_post.get(vote.post_id, 0) + 1
        per_voter[vote.voter] = per_voter.get(vote.voter, 0) + 1
    assert max(per_post.values()) <= 3 * CASTS_PER_ITEM_PER_TICK
    assert max(per_voter.values()) <= 3, "persona hourly cap was exceeded"
    pairs = [(v.voter, v.post_id) for v in votes]
    assert len(pairs) == len(set(pairs)), "catch-up duplicated a voter/target pair"
