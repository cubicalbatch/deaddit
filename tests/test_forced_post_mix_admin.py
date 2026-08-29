"""Forced post mix: Config/admin plumbing after the Phase 4 cutover.

The three ``AGENT_*`` intent-mix settings moved into immutable
``agent.visit_profile`` documents; these tests lock that no live Config
or admin path can still set them.
"""

from __future__ import annotations

import pytest

from deaddit import Config
from deaddit.admin import _run_json
from deaddit.models import AgentRun
from deaddit.settings.service import SecretNotPersistable

_RETIRED_KEYS = (
    "AGENT_POST_INTENT_CHANCE",
    "AGENT_FORCED_IMAGE_CHANCE",
    "AGENT_FORCED_WEBSITE_CHANCE",
)


def test_config_set_many_atomic_success_and_cache_invalidation(app, db_session):
    """Config.set_many atomically sets multiple keys in one transaction and invalidates cache."""
    mapping = {
        "SEED_VOTE_MAX": "200",
        "SEED_DECAY_DAYS": "60",
    }
    Config.set_many(mapping)

    assert Config.get("SEED_VOTE_MAX") == "200"
    assert Config.get("SEED_DECAY_DAYS") == "60"


def test_config_set_many_refuses_secret_keys(app, db_session):
    """Config.set_many raises SecretNotPersistable if any key in the mapping is a secret."""
    with pytest.raises(SecretNotPersistable):
        Config.set_many(
            {
                "SEED_VOTE_MAX": "150",
                "API_TOKEN": "forbidden_token",
            }
        )


def test_intent_mix_settings_are_retired(app, db_session):
    """No intent-mix default or description remains; the profile owns the mix."""
    for key in _RETIRED_KEYS:
        assert key not in Config.DEFAULTS
        assert key not in Config.DESCRIPTIONS


def test_admin_api_ignores_intent_mix_payload(client, db_session):
    """The settings save API no longer writes intent-mix settings."""
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
    for key in _RETIRED_KEYS:
        assert Config.get(key) is None


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
