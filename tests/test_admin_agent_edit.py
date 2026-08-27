"""Tests for Admin Agent Edit & Management endpoints and UI."""

from __future__ import annotations

from datetime import datetime

import pytest

import deaddit.llm.capabilities as capabilities
from deaddit.extensions import db
from deaddit.llm.errors import CapabilityError
from deaddit.models import Agent, AgentRun


@pytest.fixture()
def admin_client(client):
    """Client that passes the admin_required gate even if API_TOKEN is set."""
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


def _make_agent(
    db_session,
    username,
    *,
    enabled=False,
    tier="regular",
    config=None,
    status="idle",
    failures=0,
):
    agent = Agent(
        user_username=username,
        autonomy_tier=tier,
        is_enabled=enabled,
        status=status,
        config=config
        or {
            "min_delay": 60,
            "max_delay": 900,
            "max_actions_per_run": 30,
            "max_run_seconds": 300,
        },
        state={},
        consecutive_failures=failures,
        next_run_at=datetime.utcnow() if enabled else None,
    )
    db_session.add(agent)
    db_session.commit()
    return agent


def _noop_tools_allowed(api_url, model_name, **kwargs):
    return None


def test_update_agent_tier_and_delays(seeded_db, admin_client, db_session, monkeypatch):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)
    agent = _make_agent(db_session, "alice")

    resp = admin_client.put(
        f"/admin/api/agents/{agent.id}",
        json={
            "autonomy_tier": "power_user",
            "min_delay": 300,
            "max_delay": 1200,
            "max_actions_per_run": 50,
            "max_run_seconds": 600,
            "daily_request_ceiling": 500,
            "model": "qwen-2.5-72b",
            "api_url": "http://llm.local/v1",
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    agent_data = data["agent"]
    assert agent_data["autonomy_tier"] == "power_user"
    assert agent_data["config"]["min_delay"] == 300
    assert agent_data["config"]["max_delay"] == 1200
    assert agent_data["config"]["max_actions_per_run"] == 50
    assert agent_data["config"]["max_run_seconds"] == 600
    assert agent_data["config"]["daily_request_ceiling"] == 500
    assert agent_data["config"]["model"] == "qwen-2.5-72b"
    assert agent_data["config"]["api_url"] == "http://llm.local/v1"

    db.session.refresh(agent)
    assert agent.autonomy_tier == "power_user"
    assert agent.config["min_delay"] == 300
    assert agent.config["max_delay"] == 1200
    assert agent.config["daily_request_ceiling"] == 500


def test_update_agent_via_nested_config_and_post_update_url(
    seeded_db, admin_client, db_session, monkeypatch
):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)
    agent = _make_agent(db_session, "alice")

    resp = admin_client.post(
        f"/admin/api/agents/{agent.id}/update",
        json={
            "autonomy_tier": "lurker",
            "config": {
                "min_delay": 120,
                "max_delay": 600,
                "max_actions_per_run": 15,
                "max_run_seconds": 180,
                "daily_request_ceiling": 100,
                "model": "mistral-7b",
            },
        },
    )
    assert resp.status_code == 200
    db.session.refresh(agent)
    assert agent.autonomy_tier == "lurker"
    assert agent.config["min_delay"] == 120
    assert agent.config["max_delay"] == 600
    assert agent.config["max_actions_per_run"] == 15
    assert agent.config["max_run_seconds"] == 180
    assert agent.config["daily_request_ceiling"] == 100
    assert agent.config["model"] == "mistral-7b"


def test_update_agent_clear_daily_request_ceiling(seeded_db, admin_client, db_session):
    agent = _make_agent(
        db_session,
        "alice",
        config={"daily_request_ceiling": 1000, "min_delay": 60, "max_delay": 300},
    )

    # Clear with null / empty string
    resp = admin_client.put(
        f"/admin/api/agents/{agent.id}",
        json={"daily_request_ceiling": None},
    )
    assert resp.status_code == 200
    db.session.refresh(agent)
    assert "daily_request_ceiling" not in agent.config

    # Re-add then clear with empty string
    agent.config = {"daily_request_ceiling": 500, "min_delay": 60, "max_delay": 300}
    db.session.commit()

    resp2 = admin_client.put(
        f"/admin/api/agents/{agent.id}",
        json={"daily_request_ceiling": ""},
    )
    assert resp2.status_code == 200
    db.session.refresh(agent)
    assert "daily_request_ceiling" not in agent.config


def test_update_agent_toggle_enable_status(seeded_db, admin_client, db_session):
    agent = _make_agent(db_session, "alice", enabled=False, failures=3)

    # Enable
    resp = admin_client.put(f"/admin/api/agents/{agent.id}", json={"is_enabled": True})
    assert resp.status_code == 200
    db.session.refresh(agent)
    assert agent.is_enabled is True
    assert agent.consecutive_failures == 0
    assert agent.next_run_at is not None

    # Disable
    resp2 = admin_client.put(
        f"/admin/api/agents/{agent.id}", json={"is_enabled": False}
    )
    assert resp2.status_code == 200
    db.session.refresh(agent)
    assert agent.is_enabled is False
    assert agent.next_run_at is None
    assert agent.status == "idle"


def test_update_agent_reset_status_and_errors(seeded_db, admin_client, db_session):
    agent = _make_agent(db_session, "alice", enabled=True, status="failed", failures=5)
    agent.next_run_at = None
    db.session.commit()

    resp = admin_client.put(
        f"/admin/api/agents/{agent.id}", json={"reset_status": True}
    )
    assert resp.status_code == 200
    db.session.refresh(agent)
    assert agent.consecutive_failures == 0
    assert agent.status == "idle"
    assert agent.next_run_at is not None


def test_update_agent_validation_errors(
    seeded_db, admin_client, db_session, monkeypatch
):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)
    agent = _make_agent(
        db_session, "alice", config={"min_delay": 100, "max_delay": 500}
    )

    # Invalid tier
    resp = admin_client.put(
        f"/admin/api/agents/{agent.id}", json={"autonomy_tier": "godmode"}
    )
    assert resp.status_code == 400
    assert "Unknown tier" in resp.get_json()["error"]

    # min_delay > max_delay
    resp = admin_client.put(
        f"/admin/api/agents/{agent.id}", json={"min_delay": 600, "max_delay": 300}
    )
    assert resp.status_code == 400
    assert "max_delay must be >= min_delay" in resp.get_json()["error"]

    # min_delay < 0
    resp = admin_client.put(
        f"/admin/api/agents/{agent.id}", json={"min_delay": -10, "max_delay": 300}
    )
    assert resp.status_code == 400

    # Non-integer delay
    resp = admin_client.put(f"/admin/api/agents/{agent.id}", json={"min_delay": "abc"})
    assert resp.status_code == 400

    # Invalid max_actions_per_run <= 0
    resp = admin_client.put(
        f"/admin/api/agents/{agent.id}", json={"max_actions_per_run": 0}
    )
    assert resp.status_code == 400

    # Invalid max_run_seconds <= 0
    resp = admin_client.put(
        f"/admin/api/agents/{agent.id}", json={"max_run_seconds": -5}
    )
    assert resp.status_code == 400

    # Invalid daily_request_ceiling <= 0
    resp = admin_client.put(
        f"/admin/api/agents/{agent.id}", json={"daily_request_ceiling": 0}
    )
    assert resp.status_code == 400

    # Nonexistent agent
    resp = admin_client.put("/admin/api/agents/99999", json={"min_delay": 10})
    assert resp.status_code == 404


def test_update_agent_capability_check(
    seeded_db, admin_client, db_session, monkeypatch
):
    def deny(api_url, model_name, **kwargs):
        raise CapabilityError(f"Model '{model_name}' cannot do tools")

    monkeypatch.setattr(capabilities, "ensure_tools_allowed", deny)
    agent = _make_agent(db_session, "alice")

    resp = admin_client.put(
        f"/admin/api/agents/{agent.id}", json={"model": "no-tools-model"}
    )
    assert resp.status_code == 400
    assert "cannot do tools" in resp.get_json()["error"]


def test_update_agent_auth_gating(app, client, seeded_db, db_session, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "sekrit-token")
    agent = _make_agent(db_session, "alice")

    resp = client.put(f"/admin/api/agents/{agent.id}", json={"autonomy_tier": "lurker"})
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]

    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    resp2 = client.put(
        f"/admin/api/agents/{agent.id}", json={"autonomy_tier": "lurker"}
    )
    assert resp2.status_code == 200


def test_admin_agent_detail_page_renders_form(seeded_db, admin_client, db_session):
    agent = _make_agent(db_session, "alice")
    db.session.add(
        AgentRun(
            agent_id=agent.id,
            persona_username="alice",
            trigger="manual",
            status="completed",
            started_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
        )
    )
    db.session.commit()
    resp = admin_client.get(f"/admin/agents/{agent.id}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert f"const AGENT_ID = {agent.id};" in html
    assert 'api("/admin/api/agents/" + AGENT_ID)' in html
    assert '"Mode", personaMode' in html
    assert '"Fixed persona",' in html
    assert "run.persona_username" in html
    assert '"as " + (run.persona_username' in html
    assert "Edit Agent Configuration" in html
    assert "edit-agent-form" in html
    assert "edit-tier-select" in html
    assert "edit-enabled-switch" in html
    assert "edit-min-delay" in html
    assert "edit-max-delay" in html
    assert "edit-ceiling" in html
    assert "edit-max-actions" in html
    assert "edit-max-seconds" in html
    assert "reset-status-btn" in html
    assert "save-agent-btn" in html
    run_body = admin_client.get(f"/admin/api/agents/{agent.id}/runs").get_json()
    assert run_body["runs"][0]["persona_username"] == "alice"


def test_admin_agents_page_renders_edit_action(seeded_db, admin_client, db_session):
    _make_agent(db_session, "alice")
    resp = admin_client.get("/admin/agents")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Registered Agents" in html
    assert "Edit" in html
    assert "/admin/agents/__ID__" in html
