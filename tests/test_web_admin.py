"""Per-agent website-post configuration through the admin surface."""

from __future__ import annotations

import pytest

import deaddit.admin as admin_module
from deaddit.llm import capabilities
from deaddit.models import Agent, User


@pytest.fixture()
def admin_client(client):
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


@pytest.fixture(autouse=True)
def _skip_tool_capability_probe(monkeypatch):
    monkeypatch.setattr(
        capabilities, "ensure_tools_allowed", lambda api_url, model_name, **kw: None
    )


def _make_agent(db_session, username, *, tier="regular", config=None):
    agent = Agent(
        user_username=username,
        autonomy_tier=tier,
        is_enabled=False,
        status="idle",
        config=config or {"min_delay": 60, "max_delay": 900, "max_actions_per_run": 30},
        state={},
        consecutive_failures=0,
        next_run_at=None,
    )
    if db_session.get(User, username) is None:
        db_session.add(User(username=username))
        db_session.flush()
    db_session.add(agent)
    db_session.commit()
    return agent


def _create_random(admin_client, **payload):
    body = {"persona_mode": "random", "backfill_memory": False}
    body.update(payload)
    return admin_client.post("/admin/api/agents", json=body)


def test_create_website_posts_shapes_and_absent_key(seeded_db, admin_client):
    plain = _create_random(admin_client)
    assert plain.status_code == 201
    assert "website_posts" not in plain.get_json()["agent"]["config"]

    optional = _create_random(
        admin_client, website_posts={"enabled": True, "policy": "optional"}
    )
    assert optional.status_code == 201
    assert optional.get_json()["agent"]["config"]["website_posts"] == {
        "enabled": True,
        "policy": "optional",
    }

    forced = _create_random(
        admin_client, website_posts={"enabled": True, "policy": "website_only"}
    )
    assert forced.status_code == 201
    assert forced.get_json()["agent"]["config"]["website_posts"] == {
        "enabled": True,
        "policy": "website_only",
    }


def test_create_website_posts_rejects_invalid_configurations(
    seeded_db, admin_client, monkeypatch, db_session
):
    def rejected(payload, expected):
        before = Agent.query.count()
        response = _create_random(admin_client, **payload)
        assert response.status_code == 400
        assert expected in response.get_json()["error"]
        assert Agent.query.count() == before

    rejected({"website_posts": {"enabled": True}, "autonomy_tier": "lurker"}, "lurker")
    rejected(
        {"website_posts": {"enabled": True, "policy": "always"}},
        "policy",
    )
    rejected({"website_posts": []}, "object")

    def resolve_image_posts(raw, tier):
        return {
            "enabled": True,
            "provider_id": 1,
            "model": None,
            "policy": "image_only",
        }, None

    monkeypatch.setattr(admin_module, "_resolve_image_posts", resolve_image_posts)
    before = Agent.query.count()
    conflict = _create_random(
        admin_client,
        image_posts={"enabled": True, "policy": "image_only"},
        website_posts={"enabled": True, "policy": "website_only"},
    )
    assert conflict.status_code == 400
    error = conflict.get_json()["error"]
    assert "image_only" in error and "website_only" in error
    assert Agent.query.count() == before
    db_session.rollback()


def test_update_website_posts_enable_preserve_disable_and_bad_policy(
    seeded_db, admin_client, db_session
):
    agent = _make_agent(db_session, "website-round-trip")
    enabled = admin_client.put(
        f"/admin/api/agents/{agent.id}",
        json={"website_posts": {"enabled": True, "policy": "optional"}},
    )
    assert enabled.status_code == 200
    db_session.refresh(agent)
    assert agent.config["website_posts"] == {"enabled": True, "policy": "optional"}

    stored = dict(agent.config["website_posts"])
    preserved = admin_client.put(
        f"/admin/api/agents/{agent.id}", json={"min_delay": 120}
    )
    assert preserved.status_code == 200
    db_session.refresh(agent)
    assert agent.config["website_posts"] == stored
    assert agent.config["min_delay"] == 120

    invalid = admin_client.put(
        f"/admin/api/agents/{agent.id}",
        json={"website_posts": {"enabled": True, "policy": "invalid"}},
    )
    assert invalid.status_code == 400
    assert "policy" in invalid.get_json()["error"]
    db_session.rollback()
    db_session.refresh(agent)
    assert agent.config["website_posts"] == stored

    disabled = admin_client.put(
        f"/admin/api/agents/{agent.id}", json={"website_posts": {"enabled": False}}
    )
    assert disabled.status_code == 200
    db_session.refresh(agent)
    assert "website_posts" not in agent.config


def test_update_website_posts_conflicts_against_stored_forced_policies(
    seeded_db, admin_client, db_session, monkeypatch
):
    image_only = _make_agent(
        db_session,
        "stored-image-only",
        config={
            "min_delay": 60,
            "max_delay": 900,
            "image_posts": {
                "enabled": True,
                "provider_id": 42,
                "model": None,
                "policy": "image_only",
            },
        },
    )
    original_image_config = dict(image_only.config["image_posts"])
    conflict = admin_client.put(
        f"/admin/api/agents/{image_only.id}",
        json={"website_posts": {"enabled": True, "policy": "website_only"}},
    )
    assert conflict.status_code == 400
    error = conflict.get_json()["error"]
    assert "image_only" in error and "website_only" in error
    db_session.rollback()
    db_session.refresh(image_only)
    assert image_only.config["image_posts"] == original_image_config
    assert "website_posts" not in image_only.config

    website_only = _make_agent(
        db_session,
        "stored-website-only",
        config={
            "min_delay": 60,
            "max_delay": 900,
            "website_posts": {"enabled": True, "policy": "website_only"},
        },
    )
    original_website_config = dict(website_only.config["website_posts"])

    def resolve_image_posts(raw, tier):
        return {
            "enabled": True,
            "provider_id": 42,
            "model": None,
            "policy": "image_only",
        }, None

    monkeypatch.setattr(admin_module, "_resolve_image_posts", resolve_image_posts)
    mirror = admin_client.put(
        f"/admin/api/agents/{website_only.id}",
        json={"image_posts": {"enabled": True, "policy": "image_only"}},
    )
    assert mirror.status_code == 400
    error = mirror.get_json()["error"]
    assert "image_only" in error and "website_only" in error
    db_session.rollback()
    db_session.refresh(website_only)
    assert website_only.config["website_posts"] == original_website_config
    assert "image_posts" not in website_only.config


def test_update_tier_to_lurker_rejects_stored_website_posts(
    seeded_db, admin_client, db_session
):
    agent = _make_agent(
        db_session,
        "website-lurker",
        config={
            "min_delay": 60,
            "max_delay": 900,
            "website_posts": {"enabled": True, "policy": "optional"},
        },
    )
    response = admin_client.put(
        f"/admin/api/agents/{agent.id}", json={"autonomy_tier": "lurker"}
    )
    assert response.status_code == 400
    assert "lurker" in response.get_json()["error"]
    db_session.rollback()
    db_session.refresh(agent)
    assert agent.autonomy_tier == "regular"
    assert agent.config["website_posts"] == {"enabled": True, "policy": "optional"}


def test_both_admin_forms_expose_website_controls(seeded_db, admin_client, db_session):
    agent = _make_agent(db_session, "website-form-agent")
    create_form = admin_client.get("/admin/agents").get_data(as_text=True)
    for field in (
        "website-enabled-check",
        "website-policy-select",
        "website-conflict-feedback",
    ):
        assert field in create_form
    assert "32K+ completion tokens per site" in create_form

    edit_form = admin_client.get(f"/admin/agents/{agent.id}").get_data(as_text=True)
    for field in (
        "edit-website-enabled-switch",
        "edit-website-policy-select",
        "edit-website-conflict-feedback",
    ):
        assert field in edit_form
    assert "32K+ completion tokens per site" in edit_form
