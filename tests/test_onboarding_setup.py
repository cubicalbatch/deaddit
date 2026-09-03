"""Regression tests for the first-startup setup wizard and admin controls."""

from datetime import datetime

import pytest

from deaddit.config import Config
from deaddit.models import Agent, LLMProvider, Setting, Subdeaddit, User
from deaddit.settings.service import DeployFlagNotPersistable


@pytest.fixture()
def admin_client(client):
    """Authenticate the test client for admin routes."""
    with client.session_transaction() as session:
        session["admin_authenticated"] = True
    return client


def _assert_setup_wizard(response):
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'class="setup-wizard"' in html
    assert "Connect your LLM" in html
    assert "Dismiss this guide" in html


def test_fresh_empty_db_renders_setup_wizard(client):
    _assert_setup_wizard(client.get("/"))


def test_setup_wizard_remains_after_saving_only_llm_url(admin_client):
    response = admin_client.post(
        "/admin/api/save-config",
        json={"openai_api_url": "http://llm.example/v1"},
    )
    assert response.status_code == 200

    _assert_setup_wizard(admin_client.get("/"))


def test_loading_default_data_keeps_wizard_visible(admin_client):
    response = admin_client.post("/admin/api/load-default-data")
    assert response.status_code == 200

    response = admin_client.get("/")
    assert response.status_code == 200
    assert 'class="setup-wizard"' in response.get_data(as_text=True)


def test_setup_status_contract_and_enabled_agent_count(admin_client, db_session):
    db_session.add(
        Agent(
            persona_mode="random",
            user_username=None,
            autonomy_tier="regular",
            is_enabled=True,
            status="idle",
            config={},
            state={},
            consecutive_failures=0,
            next_run_at=datetime(2026, 1, 1),
        )
    )
    db_session.commit()

    response = admin_client.get("/admin/api/setup/status")
    assert response.status_code == 200
    status = response.get_json()
    assert set(status) == {
        "configured",
        "api_url",
        "model",
        "api_key_set",
        "has_content",
        "subdeaddit_count",
        "user_count",
        "post_count",
        "agent_count",
        "enabled_agent_count",
        "runtime_enabled",
        "voting_live",
        "worker_last_seen_iso",
        "worker_alive",
        "worker_start_command",
        "worker_logs_command",
        "next_agent_wake",
        "setup_complete",
    }
    assert status["agent_count"] == 1
    assert status["enabled_agent_count"] == 1
    assert status["next_agent_wake"] == "2026-01-01T00:00:00+00:00"


def test_runtime_toggle_validates_boolean_and_persists_setting(admin_client):
    response = admin_client.post("/admin/api/agents/runtime", json={"enabled": True})
    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert Setting.get_value("AGENT_RUNTIME_ENABLED") == "true"

    assert admin_client.post("/admin/api/agents/runtime", json={}).status_code == 400
    assert (
        admin_client.post(
            "/admin/api/agents/runtime", json={"enabled": "maybe"}
        ).status_code
        == 400
    )


@pytest.mark.parametrize("configured", [False, True])
def test_admin_setup_always_renders_wizard(admin_client, configured):
    if configured:
        response = admin_client.post(
            "/admin/api/save-config",
            json={"openai_api_url": "http://llm.example/v1"},
        )
        assert response.status_code == 200

    _assert_setup_wizard(admin_client.get("/admin/setup"))


def test_token_protects_incomplete_homepage(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "setup-token")
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin/login?next=/admin/setup")

    response = client.post(
        "/admin/login?next=/admin/setup",
        data={"api_token": "setup-token"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin/setup")


def test_setup_provider_upsert_stores_key_and_reports_presence(
    admin_client, db_session
):
    payload = {
        "openai_api_url": "http://llm.example/v1",
        "openai_model": "starter-model",
        "openai_key": "sk-setup-secret",
    }
    assert admin_client.post("/admin/api/save-config", json=payload).status_code == 200
    assert admin_client.post("/admin/api/save-config", json=payload).status_code == 200

    providers = db_session.query(LLMProvider).all()
    assert len(providers) == 1
    assert providers[0].is_default is True
    assert providers[0].api_key == "sk-setup-secret"
    status = admin_client.get("/admin/api/setup/status").get_json()
    assert status["api_key_set"] is True


def test_starter_agents_are_free_and_idempotent(admin_client, db_session, fake_llm):
    db_session.add_all(
        [
            User(username="starter_a", bio="A", interests="[]"),
            User(username="starter_b", bio="B", interests="[]"),
        ]
    )
    db_session.commit()

    first = admin_client.post(
        "/admin/api/setup/agents-from-personas", json={"count": 2}
    )
    second = admin_client.post(
        "/admin/api/setup/agents-from-personas", json={"count": 2}
    )
    assert first.status_code == second.status_code == 200
    assert Agent.query.count() == 2
    assert all(agent.is_enabled and agent.next_run_at for agent in Agent.query.all())
    assert fake_llm.requests == []


def test_starter_agents_are_random_personas_with_cadence(
    admin_client, db_session, fake_llm
):
    db_session.add_all(
        [
            User(username="starter_a", bio="A", interests="[]"),
            User(username="starter_b", bio="B", interests="[]"),
        ]
    )
    db_session.commit()

    response = admin_client.post(
        "/admin/api/setup/agents-from-personas",
        json={"count": 1, "min_delay": 10, "max_delay": 3},
    )
    assert response.status_code == 200
    agent = Agent.query.one()
    assert agent.persona_mode == "random"
    assert agent.user_username is None
    assert agent.is_enabled and agent.next_run_at is not None
    assert agent.config["min_delay"] == 10
    assert agent.config["max_delay"] == 10
    assert fake_llm.requests == []


def test_setup_voting_is_live_and_idempotent(admin_client, db_session):
    first = admin_client.post("/admin/api/setup/voting")
    second = admin_client.post("/admin/api/setup/voting")
    assert first.status_code == second.status_code == 200
    assert (
        db_session.query(Setting).filter_by(key="SIMULATED_VOTING_MODE").one().value
        == "live"
    )
    assert Setting.get_value("AGENT_RUNTIME_ENABLED") == "true"
    assert db_session.query(Setting).filter_by(key="SIMULATED_VOTING_MODE").count() == 1


def test_setup_completion_persists_without_worker_heartbeat(admin_client, db_session):
    db_session.add_all(
        [
            LLMProvider(
                name="default",
                api_url="http://llm.example/v1",
                default_model="starter",
                is_default=True,
            ),
            User(username="complete_user", bio="A", interests="[]"),
            Subdeaddit(name="complete_sub", description="A"),
        ]
    )
    db_session.flush()
    db_session.add(
        Agent(
            persona_mode="fixed",
            user_username="complete_user",
            autonomy_tier="regular",
            is_enabled=True,
            status="idle",
            config={},
            state={},
            consecutive_failures=0,
        )
    )
    db_session.commit()
    Setting.set_value("AGENT_RUNTIME_ENABLED", "true")
    Setting.set_value("SIMULATED_VOTING_MODE", "live")

    status = admin_client.get("/admin/api/setup/status").get_json()
    assert status["setup_complete"] is True
    assert Setting.get_value("SETUP_COMPLETED_AT")
    assert status["worker_alive"] is False
    home = admin_client.get("/")
    assert home.status_code == 200
    assert 'class="setup-wizard"' not in home.get_data(as_text=True)


def test_setup_status_exposes_environment_worker_commands(admin_client, monkeypatch):
    monkeypatch.setenv("DEADDIT_DOCKER", "true")
    status = admin_client.get("/admin/api/setup/status").get_json()
    assert status["worker_start_command"] == "docker compose up -d worker"
    assert status["worker_logs_command"] == "docker compose logs -f worker"


def test_setup_dismiss_is_idempotent(admin_client):
    first = admin_client.post("/admin/api/setup/dismiss").get_json()
    second = admin_client.post("/admin/api/setup/dismiss").get_json()
    assert first["success"] is True
    assert second["setup_complete"] is True
    assert second["setup_completed_at"] == first["setup_completed_at"]


def test_production_still_renders_wizard_on_empty_homepage(client, monkeypatch):
    # PRODUCTION is a deploy flag: the environment is the only way to set it,
    # which is also the only way a real deployment can turn it on. It no
    # longer suppresses the wizard — it only hides the Admin nav link.
    monkeypatch.setenv("PRODUCTION", "true")

    _assert_setup_wizard(client.get("/"))


def test_production_hides_admin_nav_link_until_logged_in(client, monkeypatch):
    monkeypatch.setenv("PRODUCTION", "true")

    anon = client.get("/").get_data(as_text=True)
    assert 'href="/admin/dashboard">Admin</a>' not in anon

    with client.session_transaction() as session:
        session["admin_authenticated"] = True
    authed = client.get("/").get_data(as_text=True)
    assert 'href="/admin/dashboard">Admin</a>' in authed


def test_production_flag_is_refused_by_the_database(client):
    with pytest.raises(DeployFlagNotPersistable):
        Config.set("PRODUCTION", "true")
    with pytest.raises(DeployFlagNotPersistable):
        Config.set_many({"PRODUCTION": "true"})


def test_production_row_cannot_shadow_the_environment(client, monkeypatch):
    """The bug this guards: a seeded PRODUCTION=false row made the env inert."""
    Setting.set_value("PRODUCTION", "false")
    monkeypatch.setenv("API_TOKEN", "token")
    monkeypatch.setenv("PRODUCTION", "true")

    assert Config.get("PRODUCTION") == "true"
    # Admin routes stay reachable in production; the token gate still applies.
    assert client.get("/admin/setup").status_code == 302


def test_initialize_defaults_seeds_no_production_row_and_prunes_stale_ones(
    db_session,
):
    Setting.set_value("PRODUCTION", "false")

    Config.initialize_defaults()

    assert Setting.get_value("PRODUCTION") is None
