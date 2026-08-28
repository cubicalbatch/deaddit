"""Namespaced per-agent image configuration.

Agent.config["image_posts"] is validated through deaddit.admin's create/update
agent endpoints. Every test here registers a FakeImageAdapter via
deaddit.images.client.register_adapter()/reset_adapters() so nothing ever
reaches fal.ai or Runware - the autouse conftest network guard would fail any
real egress attempt anyway.
"""

from __future__ import annotations

import pytest

import deaddit.llm.capabilities as capabilities
from deaddit.extensions import db
from deaddit.images.client import register_adapter as register_image_adapter
from deaddit.images.client import reset_adapters
from deaddit.images.types import ModelValidation
from deaddit.models import Agent, ImageProvider, User
from tests.fakes import FakeImageAdapter


@pytest.fixture()
def admin_client(client):
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


@pytest.fixture(autouse=True)
def _clean_adapter_registry():
    reset_adapters()
    yield
    reset_adapters()


@pytest.fixture(autouse=True)
def _skip_tool_capability_probe(monkeypatch):
    monkeypatch.setattr(
        capabilities, "ensure_tools_allowed", lambda api_url, model_name, **kw: None
    )


@pytest.fixture()
def fake_fal(monkeypatch):
    """A FakeImageAdapter registered as 'fal', with FALAI_API_KEY set."""
    adapter = FakeImageAdapter()
    register_image_adapter("fal", adapter)
    monkeypatch.setenv("FALAI_API_KEY", "test-fal-secret-value")
    return adapter


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
    db_session.add(agent)
    db_session.commit()
    return agent


def _make_provider(db_session, *, default_model="fal-ai/flux-1/schnell", **overrides):
    fields = {
        "name": "Fal",
        "provider_type": "fal",
        "credential_env": "FALAI_API_KEY",
        "default_model": default_model,
        "is_enabled": True,
    }
    fields.update(overrides)
    provider = ImageProvider(**fields)
    db_session.add(provider)
    db_session.commit()
    return provider


def test_image_posts_config_round_trip_and_validation(
    seeded_db, admin_client, db_session, monkeypatch, fake_fal
):
    provider = _make_provider(db_session)

    # An agent created without the key stays image-disabled.
    plain = admin_client.post(
        "/admin/api/agents", json={"username": "alice", "backfill_memory": False}
    )
    assert plain.status_code == 201
    assert "image_posts" not in plain.get_json()["agent"]["config"]
    Agent.query.filter_by(user_username="alice").delete()
    db.session.commit()

    # Enabling without a model override falls back to the provider default.
    created = admin_client.post(
        "/admin/api/agents",
        json={
            "username": "alice",
            "backfill_memory": False,
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "policy": "optional",
            },
        },
    )
    assert created.status_code == 201
    assert created.get_json()["agent"]["config"]["image_posts"] == {
        "enabled": True,
        "provider_id": provider.id,
        "model": None,
        "policy": "optional",
    }
    Agent.query.filter_by(user_username="alice").delete()
    db.session.commit()

    # A model override is validated against the provider before it is stored.
    fake_fal.enqueue_validate(ModelValidation(compatible=True))
    overridden = admin_client.post(
        "/admin/api/agents",
        json={
            "username": "alice",
            "backfill_memory": False,
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "model": "fal-ai/flux-1/dev",
                "policy": "image_only",
            },
        },
    )
    assert overridden.status_code == 201
    image_cfg = overridden.get_json()["agent"]["config"]["image_posts"]
    assert image_cfg["model"] == "fal-ai/flux-1/dev"
    assert image_cfg["policy"] == "image_only"
    assert fake_fal.validate_calls[0]["model_id"] == "fal-ai/flux-1/dev"

    def rejected_create(image_posts, expected, **extra):
        Agent.query.filter_by(user_username="bob").delete()
        db.session.commit()
        res = admin_client.post(
            "/admin/api/agents",
            json={
                "username": "bob",
                "backfill_memory": False,
                "image_posts": image_posts,
                **extra,
            },
        )
        assert res.status_code == 400, res.get_data(as_text=True)
        assert expected in res.get_json()["error"]
        assert Agent.query.filter_by(user_username="bob").first() is None

    rejected_create(
        {"enabled": True, "provider_id": provider.id}, "lurker", autonomy_tier="lurker"
    )
    defaulted = admin_client.post(
        "/admin/api/agents",
        json={
            "username": "bob",
            "backfill_memory": False,
            "image_posts": {"enabled": True},
        },
    )
    assert defaulted.status_code == 201
    assert defaulted.get_json()["agent"]["config"]["image_posts"]["provider_id"] is None
    rejected_create({"enabled": True, "provider_id": 999999}, "not found")
    rejected_create(
        {"enabled": True, "provider_id": provider.id, "policy": "always"}, "policy"
    )

    fake_fal.enqueue_validate(
        ModelValidation(compatible=False, reason="no prompt input")
    )
    rejected_create(
        {"enabled": True, "provider_id": provider.id, "model": "not-a-real-model"},
        "no prompt input",
    )

    disabled_provider = _make_provider(
        db_session, name="Disabled Fal", is_enabled=False
    )
    rejected_create({"enabled": True, "provider_id": disabled_provider.id}, "disabled")

    modelless = _make_provider(db_session, name="No Default", default_model=None)
    rejected_create({"enabled": True, "provider_id": modelless.id}, "default")

    monkeypatch.delenv("FALAI_API_KEY", raising=False)
    rejected_create({"enabled": True, "provider_id": provider.id}, "credential")
    monkeypatch.setenv("FALAI_API_KEY", "test-fal-secret-value")
    keyed = _make_provider(db_session, name="Keyed Fal", api_key="stored-key-42")
    db_session.add(User(username="carol"))
    db_session.commit()
    monkeypatch.delenv("FALAI_API_KEY", raising=False)
    keyed_create = admin_client.post(
        "/admin/api/agents",
        json={
            "username": "carol",
            "backfill_memory": False,
            "image_posts": {"enabled": True, "provider_id": keyed.id},
        },
    )
    assert keyed_create.status_code == 201, keyed_create.get_data(as_text=True)
    assert keyed_create.get_json()["agent"]["config"]["image_posts"] == {
        "enabled": True,
        "provider_id": keyed.id,
        "model": None,
        "policy": "optional",
    }
    monkeypatch.setenv("FALAI_API_KEY", "test-fal-secret-value")
    Agent.query.filter_by(user_username="carol").delete()
    db.session.commit()

    # Updates: enabling, preserving unrelated keys, and disabling.
    bob = _make_agent(db_session, "bob", config={"min_delay": 60, "max_delay": 900})
    assert "image_posts" not in bob.config, "existing agents start image-disabled"

    fake_fal.enqueue_validate(ModelValidation(compatible=True))
    enabled = admin_client.put(
        f"/admin/api/agents/{bob.id}",
        json={
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "model": "fal-ai/flux-1/dev",
                "policy": "optional",
            }
        },
    )
    assert enabled.status_code == 200
    db.session.refresh(bob)
    assert bob.config["image_posts"] == {
        "enabled": True,
        "provider_id": provider.id,
        "model": "fal-ai/flux-1/dev",
        "policy": "optional",
    }
    assert (bob.config["min_delay"], bob.config["max_delay"]) == (60, 900)

    stored = dict(bob.config["image_posts"])
    assert (
        admin_client.put(
            f"/admin/api/agents/{bob.id}", json={"min_delay": 120}
        ).status_code
        == 200
    )
    db.session.refresh(bob)
    assert bob.config["image_posts"] == stored, "an unrelated update preserves the key"
    assert bob.config["min_delay"] == 120

    # An image-posting agent cannot be demoted to a lurker.
    lurker = admin_client.put(
        f"/admin/api/agents/{bob.id}", json={"autonomy_tier": "lurker"}
    )
    assert lurker.status_code == 400 and "lurker" in lurker.get_json()["error"]
    db.session.rollback()

    disabled = admin_client.put(
        f"/admin/api/agents/{bob.id}", json={"image_posts": {"enabled": False}}
    )
    assert disabled.status_code == 200
    db.session.refresh(bob)
    assert "image_posts" not in bob.config
    assert bob.config["min_delay"] == 120

    for payload, expected in (
        ({"image_posts": {"enabled": True, "provider_id": 999999}}, "not found"),
        ({"image_posts": "yes please"}, "object"),
    ):
        res = admin_client.put(f"/admin/api/agents/{bob.id}", json=payload)
        assert res.status_code == 400 and expected in res.get_json()["error"]
    db.session.refresh(bob)
    assert "image_posts" not in bob.config

    # Both admin forms expose the image controls.
    create_form = admin_client.get("/admin/agents").get_data(as_text=True)
    for field in (
        "image-enabled-check",
        "image-provider-select",
        "image-model-input",
        "image-policy-select",
    ):
        assert field in create_form
    edit_form = admin_client.get(f"/admin/agents/{bob.id}").get_data(as_text=True)
    for field in (
        "edit-image-enabled-switch",
        "edit-image-provider-select",
        "edit-image-model",
        "edit-image-policy-select",
    ):
        assert field in edit_form


def test_random_agent_image_posts_round_trip(
    seeded_db, admin_client, db_session, fake_fal
):
    provider = _make_provider(db_session)
    created = admin_client.post(
        "/admin/api/agents",
        json={"persona_mode": "random", "backfill_memory": False},
    )
    assert created.status_code == 201
    agent_data = created.get_json()["agent"]
    assert agent_data["persona_mode"] == "random"
    assert agent_data["user_username"] is None

    fake_fal.enqueue_validate(ModelValidation(compatible=True))
    image_config = {
        "enabled": True,
        "provider_id": provider.id,
        "model": "fal-ai/flux-1/dev",
        "policy": "optional",
    }
    updated = admin_client.put(
        f"/admin/api/agents/{agent_data['id']}",
        json={"image_posts": image_config},
    )
    assert updated.status_code == 200
    assert updated.get_json()["agent"]["config"]["image_posts"] == image_config

    updated_agent = updated.get_json()["agent"]
    assert updated_agent["persona_mode"] == "random"
    assert updated_agent["user_username"] is None
    agent = db_session.get(Agent, agent_data["id"])
    assert agent.persona_mode == "random"
    assert agent.user_username is None
    assert agent.config["image_posts"] == image_config
