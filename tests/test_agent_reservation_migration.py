"""Preserve run history when upgrading the per-agent running invariant."""

from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from deaddit.extensions import db
from deaddit.models import Agent, AgentRun, User
from tests.test_random_persona_migration import _downgrade, _runner, _upgrade

PREVIOUS_HEAD = "a9b8c7d6e5f4"


def test_upgrade_interrupts_duplicate_runs_and_downgrade_removes_constraint(tmp_path):
    _, app, runner = _runner(tmp_path)
    _upgrade(runner, PREVIOUS_HEAD)
    with app.app_context():
        db.session.add_all([User(username="older"), User(username="newer")])
        agent = Agent(persona_mode="random", status="running")
        db.session.add(agent)
        db.session.flush()
        agent_id = agent.id
        runs = [
            AgentRun(
                agent_id=agent_id,
                persona_username=username,
                trigger="manual",
                started_at=datetime(2026, 9, 1),
            )
            for username in ("older", "newer")
        ]
        db.session.add_all(runs)
        db.session.commit()
        run_ids = [run.id for run in runs]

    _upgrade(runner)
    with app.app_context():
        older, newer = [db.session.get(AgentRun, run_id) for run_id in run_ids]
        assert older.status == "interrupted"
        assert older.finished_at is not None
        assert newer.status == "running"
        db.session.add(AgentRun(agent_id=agent_id, persona_username="older", trigger="manual"))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        assert AgentRun.query.count() == 2

    _downgrade(runner, PREVIOUS_HEAD)
    with app.app_context():
        db.session.add(AgentRun(agent_id=agent_id, persona_username="older", trigger="manual"))
        db.session.commit()
        assert AgentRun.query.filter_by(agent_id=agent_id, status="running").count() == 2
