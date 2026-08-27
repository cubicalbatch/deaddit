"""Deterministic coverage for the WakeScheduler (AgenticCore Phase 2).

Covers boot recovery (stale-run interruption windows, flag-gated arming)
and poll-tick behavior: flag gating, global-concurrency bound from
AGENT_MAX_CONCURRENT_RUNS, and per-agent daily_request_ceiling deferral.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import deaddit.runtime.wakes as wakes
from deaddit.extensions import db
from deaddit.models import Agent, AgentRun, AgentTurn, Setting, User
from deaddit.runtime.wakes import CEILING_DEFER_SECONDS, WakeScheduler


def _make_agent(db_session, username, *, enabled=True, config=None):
    agent = Agent(
        user_username=username,
        autonomy_tier="regular",
        is_enabled=enabled,
        status="idle",
        config=config or {},
        state={},
        consecutive_failures=0,
    )
    db_session.add(agent)
    db_session.commit()
    return agent


def _set_flag(value: str) -> None:
    Setting.set_value("AGENT_RUNTIME_ENABLED", value)


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    """Busy-wait for an executor-side condition; avoids sleep-based races."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


# ---------------------------------------------------------------------------
# Boot recovery: stale-run interruption


def test_recover_interrupts_only_runs_past_budget_plus_grace(
    seeded_db, db_session, app
):
    agent = _make_agent(db_session, "alice", config={"max_run_seconds": 300})
    agent2 = _make_agent(db_session, "bob", config={"max_run_seconds": 300})
    now = datetime.utcnow()
    stale = AgentRun(
        agent_id=agent.id,
        trigger="schedule",
        status="running",
        started_at=now - timedelta(seconds=400),
    )
    # Within the 300s budget + grace window: a live run, must be left alone.
    fresh = AgentRun(
        agent_id=agent2.id,
        trigger="schedule",
        status="running",
        started_at=now - timedelta(seconds=270),
    )
    db.session.add_all([stale, fresh])
    db.session.commit()

    scheduler = WakeScheduler(app)
    interrupted, armed = scheduler.recover()

    assert (interrupted, armed) == (1, 0)
    assert stale.status == "interrupted"
    assert stale.finished_at is not None
    assert "exceeded wall-clock budget" in stale.error_message
    assert fresh.status == "running"
    assert fresh.finished_at is None


def test_recover_uses_fallback_budget_when_config_garbage(seeded_db, db_session, app):
    agent = _make_agent(db_session, "alice", config={"max_run_seconds": "not-a-number"})
    run = AgentRun(
        agent_id=agent.id,
        trigger="schedule",
        status="running",
        started_at=datetime.utcnow() - timedelta(seconds=421),
    )
    db.session.add(run)
    db.session.commit()

    scheduler = WakeScheduler(app)
    interrupted, _ = scheduler.recover()

    # Fallback budget is 300s + 60s grace; 421s exceeds it.
    assert interrupted == 1
    assert run.status == "interrupted"


def test_recover_arming_is_flag_gated(seeded_db, db_session, app):
    enabled = _make_agent(db_session, "alice")  # next_run_at None by default
    disabled = _make_agent(db_session, "bob", enabled=False)
    db.session.add(User(username="carol", bio="third persona"))
    db.session.commit()
    already_scheduled = _make_agent(db_session, "carol")
    already_scheduled.next_run_at = datetime.utcnow() + timedelta(hours=1)
    db.session.commit()

    scheduler = WakeScheduler(app)

    # Flag off (default): hygiene runs, nothing is armed.
    interrupted, armed = scheduler.recover()
    assert (interrupted, armed) == (0, 0)
    for agent in (enabled, disabled):
        db.session.refresh(agent)
        assert agent.next_run_at is None

    # Flag on: only enabled agents with no wake are armed.
    _set_flag("true")
    interrupted, armed = scheduler.recover()
    assert (interrupted, armed) == (0, 1)
    db.session.refresh(enabled)
    assert enabled.next_run_at is not None
    assert abs((datetime.utcnow() - enabled.next_run_at).total_seconds()) < 120
    db.session.refresh(disabled)
    assert disabled.next_run_at is None
    db.session.refresh(already_scheduled)
    assert already_scheduled.next_run_at > datetime.utcnow()


def test_recover_never_arms_disabled_agents_even_with_flag(seeded_db, db_session, app):
    _set_flag("true")
    disabled = _make_agent(db_session, "bob", enabled=False)

    scheduler = WakeScheduler(app)
    _, armed = scheduler.recover()

    assert armed == 0
    db.session.refresh(disabled)
    assert disabled.next_run_at is None


# ---------------------------------------------------------------------------
# Poll tick: flag gating


def test_poll_tick_is_flag_gated(seeded_db, db_session, app, monkeypatch):
    agent = _make_agent(db_session, "alice")
    agent.next_run_at = datetime.utcnow() - timedelta(seconds=10)
    db.session.commit()

    calls: list[int] = []
    monkeypatch.setattr(
        wakes,
        "run_once",
        lambda agent_id, *, trigger="schedule": calls.append(agent_id),
    )
    scheduler = WakeScheduler(app)
    scheduler._poll_once()

    assert calls == []
    # Pool is never even built when the runtime is switched off.
    assert scheduler._executor is None
    assert agent.status == "idle"


# ---------------------------------------------------------------------------
# Poll tick: global concurrency bound


def test_global_semaphore_bounds_concurrent_wakes(
    seeded_db, db_session, app, monkeypatch
):
    _set_flag("true")
    Setting.set_value("AGENT_MAX_CONCURRENT_RUNS", "2")
    db.session.add(User(username="carol", bio="third persona"))
    db.session.commit()
    now = datetime.utcnow()
    staggered = {"alice": 30, "bob": 20, "carol": 10}
    agents = {}
    for name, seconds_ago in staggered.items():
        agents[name] = _make_agent(db_session, name)
        agents[name].next_run_at = now - timedelta(seconds=seconds_ago)
    db.session.commit()

    gate = threading.Event()
    lock = threading.Lock()
    state = {"active": 0, "peak": 0, "ran": []}

    def fake_run_once(agent_id, *, trigger="schedule"):
        assert trigger == "schedule"
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            state["ran"].append(agent_id)
        gate.wait(timeout=10)
        with lock:
            state["active"] -= 1

    monkeypatch.setattr(wakes, "run_once", fake_run_once)
    scheduler = WakeScheduler(app)

    # First tick fills both slots; both runs block inside the fake.
    scheduler._poll_once()
    assert _wait_until(
        lambda: sorted(state["ran"]) == [agents["alice"].id, agents["bob"].id]
    )
    assert state["peak"] == 2

    # While both slots are held, further ticks submit nothing new.
    scheduler._poll_once()
    assert sorted(state["ran"]) == [agents["alice"].id, agents["bob"].id]

    gate.set()
    scheduler._executor.shutdown(wait=True)
    # Let the next tick rebuild a fresh pool (shutdown executors reject work).
    scheduler._executor = None
    scheduler._pool_size = 0
    # Retire the two finished wakes so carol becomes the only candidate.
    for name in ("alice", "bob"):
        row = Agent.query.filter_by(user_username=name).first()
        row.next_run_at = datetime.utcnow() + timedelta(hours=1)
    db.session.commit()
    scheduler._poll_once()
    scheduler._executor.shutdown(wait=True)

    assert sorted(state["ran"]) == [
        agents["alice"].id,
        agents["bob"].id,
        agents["carol"].id,
    ]
    assert state["peak"] == 2
    scheduler.stop(wait=False)


# ---------------------------------------------------------------------------
# Poll tick: per-agent daily request ceiling


def _seed_today_turns(db_session, agent, count):
    run = AgentRun(
        agent_id=agent.id,
        trigger="schedule",
        status="completed",
        started_at=datetime.utcnow(),
    )
    db_session.add(run)
    db.session.flush()
    for seq in range(count):
        db.session.add(
            AgentTurn(run_id=run.id, seq=seq, request_messages=[], response_message={})
        )
    db.session.commit()


def test_daily_ceiling_defers_next_wake_by_thirty_minutes(
    seeded_db, db_session, app, monkeypatch
):
    _set_flag("true")
    agent = _make_agent(db_session, "alice", config={"daily_request_ceiling": 2})
    agent.next_run_at = datetime.utcnow() - timedelta(seconds=5)
    db.session.commit()
    _seed_today_turns(db_session, agent, 2)

    calls: list[int] = []
    monkeypatch.setattr(
        wakes,
        "run_once",
        lambda agent_id, *, trigger="schedule": calls.append(agent_id),
    )
    scheduler = WakeScheduler(app)
    scheduler._poll_once()
    scheduler._executor.shutdown(wait=True)

    # Ceiling hit: no launch, wake pushed out by CEILING_DEFER_SECONDS.
    assert calls == []
    db.session.refresh(agent)
    delta = (agent.next_run_at - datetime.utcnow()).total_seconds()
    assert CEILING_DEFER_SECONDS - 30 <= delta <= CEILING_DEFER_SECONDS


def test_under_ceiling_agent_launches_normally(seeded_db, db_session, app, monkeypatch):
    _set_flag("true")
    agent = _make_agent(db_session, "alice", config={"daily_request_ceiling": 3})
    agent.next_run_at = datetime.utcnow() - timedelta(seconds=5)
    db.session.commit()
    _seed_today_turns(db_session, agent, 1)

    calls: list[int] = []
    monkeypatch.setattr(
        wakes,
        "run_once",
        lambda agent_id, *, trigger="schedule": calls.append(agent_id),
    )
    scheduler = WakeScheduler(app)
    scheduler._poll_once()
    scheduler._executor.shutdown(wait=True)

    assert calls == [agent.id]
    scheduler.stop(wait=False)


def test_zero_ceiling_means_unlimited(seeded_db, db_session, app, monkeypatch):
    _set_flag("true")
    agent = _make_agent(db_session, "alice", config={})  # no ceiling configured
    agent.next_run_at = datetime.utcnow() - timedelta(seconds=5)
    db.session.commit()
    _seed_today_turns(db_session, agent, 50)

    calls: list[int] = []
    monkeypatch.setattr(
        wakes,
        "run_once",
        lambda agent_id, *, trigger="schedule": calls.append(agent_id),
    )
    scheduler = WakeScheduler(app)
    scheduler._poll_once()
    scheduler._executor.shutdown(wait=True)

    assert calls == [agent.id]
    scheduler.stop(wait=False)


# ---------------------------------------------------------------------------
# Per-tick self-heal: a worker killed mid-run parks the agent in
# status='running'; once the grace window passes, the poll tick itself must
# interrupt the run and free the agent — no restart required.


def test_poll_tick_self_heals_killed_run_after_grace(seeded_db, db_session, app):
    _set_flag("true")
    agent = _make_agent(db_session, "alice", config={"max_run_seconds": 300})
    agent.status = "running"  # parked by the killed run
    agent.next_run_at = None
    run = AgentRun(
        agent_id=agent.id,
        trigger="schedule",
        status="running",
        started_at=datetime.utcnow() - timedelta(seconds=361),  # past 300+60 grace
    )
    db.session.add(run)
    db.session.commit()

    scheduler = WakeScheduler(app)
    scheduler._poll_once()

    assert run.status == "interrupted"
    assert agent.status == "idle"
