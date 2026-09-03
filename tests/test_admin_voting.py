"""Admin API and persistence contract for simulated voting controls."""

from __future__ import annotations

from datetime import datetime

import pytest

import deaddit.admin as admin_module
from deaddit.dynamics.engagement import preset_config
from deaddit.extensions import db
from deaddit.models import Setting, VoteCadencePolicy, VoteSimulationHourly


@pytest.fixture()
def admin_client(client):
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


def test_voting_routes_require_authentication_and_not_disabled_in_production(
    client, monkeypatch
):
    monkeypatch.setattr(
        admin_module.Config,
        "get",
        classmethod(
            lambda cls, key, default=None: "token" if key == "API_TOKEN" else default
        ),
    )
    for method, path in (
        ("get", "/admin/voting"),
        ("get", "/admin/api/voting"),
        ("put", "/admin/api/voting/mode"),
        ("post", "/admin/api/voting/policies"),
    ):
        response = getattr(client, method)(path, json={})
        assert response.status_code == 302

    # PRODUCTION only hides the Admin nav link; the routes stay reachable.
    monkeypatch.setattr(
        admin_module.Config,
        "get",
        classmethod(
            lambda cls, key, default=None: "true" if key == "PRODUCTION" else default
        ),
    )
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    assert client.get("/admin/voting").status_code == 200
    assert client.get("/admin/api/voting").status_code == 200


def test_preset_save_is_server_owned_and_mode_requires_policy(seeded_db, admin_client):
    assert (
        admin_client.put("/admin/api/voting/mode", json={"mode": "shadow"}).status_code
        == 400
    )
    response = admin_client.post(
        "/admin/api/voting/policies", json={"preset": "natural"}
    )
    assert response.status_code == 201
    policy = VoteCadencePolicy.query.one()
    assert policy.config == preset_config("natural")
    assert (
        admin_client.put("/admin/api/voting/mode", json={"mode": "live"}).status_code
        == 200
    )
    assert Setting.get_value("SIMULATED_VOTING_MODE") == "live"


def test_custom_save_appends_and_invalid_save_does_not_change_database(
    seeded_db, admin_client
):
    assert (
        admin_client.post(
            "/admin/api/voting/policies", json={"preset": "quiet"}
        ).status_code
        == 201
    )
    original = VoteCadencePolicy.query.one()
    config = preset_config("quiet")
    config["post"]["mean_active_votes"] = 4
    saved = admin_client.post(
        "/admin/api/voting/policies", json={"preset": "custom", "config": config}
    )
    assert saved.status_code == 201
    assert saved.get_json()["policy"]["label"] == "Custom"
    assert VoteCadencePolicy.query.count() == 2
    db.session.refresh(original)
    assert original.config == preset_config("quiet")

    invalid = preset_config("quiet")
    invalid["post"]["half_life_minutes"] = 0
    before = VoteCadencePolicy.query.count()
    response = admin_client.post(
        "/admin/api/voting/policies", json={"preset": "custom", "config": invalid}
    )
    assert response.status_code == 400
    assert "post.half_life_minutes" in response.get_json()["errors"]
    assert VoteCadencePolicy.query.count() == before


def test_voting_api_serializes_history_and_health(seeded_db, admin_client, db_session):
    admin_client.post("/admin/api/voting/policies", json={"preset": "busy"})
    hour = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    db_session.add(
        VoteSimulationHourly(
            hour=hour,
            mode="shadow",
            inserted_votes=3,
            switched_votes=1,
            active_proposals=4,
            cap_skips=2,
            upvotes=3,
            downvotes=1,
            updated_at=datetime.utcnow(),
        )
    )
    db_session.commit()
    body = admin_client.get("/admin/api/voting").get_json()
    assert body["presets"]["busy"]["config"] == preset_config("busy")
    assert body["current_policy"]["label"] == "Busy"
    assert "config" not in body["history"][0]
    assert body["health"]["simulated_votes"] == 4
    assert body["health"]["active_decisions"] == 4
    assert body["health"]["skipped_by_cap"] == 2
    assert body["preview"]["post"]["cumulative_votes"]["active_window_end"] == 18


def test_mode_setting_is_read_directly_for_worker_observation(seeded_db, admin_client):
    admin_client.post("/admin/api/voting/policies", json={"preset": "natural"})
    admin_client.put("/admin/api/voting/mode", json={"mode": "shadow"})
    assert Setting.get_value("SIMULATED_VOTING_MODE") == "shadow"
    admin_client.put("/admin/api/voting/mode", json={"mode": "off"})
    assert Setting.get_value("SIMULATED_VOTING_MODE") == "off"
