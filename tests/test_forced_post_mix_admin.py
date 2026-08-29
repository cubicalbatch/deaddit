"""Forced post mix: Admin API and Config.set_many tests."""

from __future__ import annotations

import pytest

from deaddit import Config
from deaddit.admin import _run_json
from deaddit.models import AgentRun
from deaddit.settings.service import SecretNotPersistable


def test_config_set_many_atomic_success_and_cache_invalidation(app, db_session):
    """Config.set_many atomically sets multiple keys in one transaction and invalidates cache."""
    mapping = {
        "AGENT_POST_INTENT_CHANCE": "0.45",
        "AGENT_FORCED_IMAGE_CHANCE": "0.15",
        "AGENT_FORCED_WEBSITE_CHANCE": "0.25",
    }
    Config.set_many(mapping)

    assert Config.get("AGENT_POST_INTENT_CHANCE") == "0.45"
    assert Config.get("AGENT_FORCED_IMAGE_CHANCE") == "0.15"
    assert Config.get("AGENT_FORCED_WEBSITE_CHANCE") == "0.25"


def test_config_set_many_refuses_secret_keys(app, db_session):
    """Config.set_many raises SecretNotPersistable if any key in the mapping is a secret."""
    with pytest.raises(SecretNotPersistable):
        Config.set_many(
            {
                "AGENT_POST_INTENT_CHANCE": "0.50",
                "API_TOKEN": "forbidden_token",
            }
        )


def test_admin_api_save_deaddit_config_content_mix(client, db_session):
    """Test /admin/api/save-deaddit-config validation and fraction conversion."""
    # 1. Successful submission of percentages
    res = client.post(
        "/admin/api/save-deaddit-config",
        json={
            "agent_post_intent_pct": 35.0,
            "agent_forced_image_pct": 20.0,
            "agent_forced_website_pct": 25.0,
        },
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert Config.get("AGENT_POST_INTENT_CHANCE") == "0.35"
    assert Config.get("AGENT_FORCED_IMAGE_CHANCE") == "0.2"
    assert Config.get("AGENT_FORCED_WEBSITE_CHANCE") == "0.25"

    # 2. Rejection when sum of forced image and website exceeds 100%
    res_bad_sum = client.post(
        "/admin/api/save-deaddit-config",
        json={
            "agent_post_intent_pct": 50.0,
            "agent_forced_image_pct": 60.0,
            "agent_forced_website_pct": 50.0,
        },
    )
    assert res_bad_sum.status_code == 200
    data_bad = res_bad_sum.get_json()
    assert data_bad["success"] is False
    assert "exceed 100%" in data_bad["message"]

    # 3. Rejection of negative numbers
    res_neg = client.post(
        "/admin/api/save-deaddit-config",
        json={
            "agent_post_intent_pct": -10.0,
            "agent_forced_image_pct": 0.0,
            "agent_forced_website_pct": 0.0,
        },
    )
    assert res_neg.status_code == 200
    data_neg = res_neg.get_json()
    assert data_neg["success"] is False
    assert "negative" in data_neg["message"].lower()


def test_run_json_serialization_includes_intent(db_session):
    """_run_json returns the intent field."""
    run = AgentRun(
        id=123,
        agent_id=1,
        persona_username="alice",
        trigger="manual",
        intent="image",
        status="completed",
    )
    payload = _run_json(run)
    assert payload["id"] == 123
    assert payload["intent"] == "image"
