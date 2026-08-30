"""Regression tests for the first-startup setup wizard and admin controls."""

import pytest

from deaddit.models import Agent, Setting


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


def test_fresh_empty_db_renders_setup_wizard(client):
    _assert_setup_wizard(client.get("/"))


def test_setup_wizard_remains_after_saving_only_llm_url(admin_client):
    response = admin_client.post(
        "/admin/api/save-config",
        json={"openai_api_url": "http://llm.example/v1"},
    )
    assert response.status_code == 200

    _assert_setup_wizard(admin_client.get("/"))


def test_loading_default_data_replaces_wizard_with_feed(admin_client):
    response = admin_client.post("/admin/api/load-default-data")
    assert response.status_code == 200

    response = admin_client.get("/")
    assert response.status_code == 200
    assert 'class="setup-wizard"' not in response.get_data(as_text=True)


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
        "worker_last_seen_iso",
        "worker_alive",
    }
    assert status["agent_count"] == 1
    assert status["enabled_agent_count"] == 1


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
