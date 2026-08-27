"""Namespaced per-agent image configuration (Phase 3B).

Agent.config["image_posts"] is validated through deaddit.admin's create/update
agent endpoints. Every test here registers a FakeImageAdapter via
deaddit.images.client.register_adapter()/reset_adapters() so nothing ever
reaches fal.ai or Runware - the autouse conftest network guard would fail any
real egress attempt anyway.
"""

from __future__ import annotations

from datetime import datetime

import pytest

import deaddit.llm.capabilities as capabilities
from deaddit.extensions import db
from deaddit.images.client import register_adapter as register_image_adapter
from deaddit.images.client import reset_adapters
from deaddit.images.types import ModelValidation
from deaddit.models import Agent, ImageProvider
from tests.fakes import FakeImageAdapter


@pytest.fixture()
def admin_client(client):
    """Client authenticated as admin."""
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


@pytest.fixture(autouse=True)
def _clean_adapter_registry():
    reset_adapters()
    yield
    reset_adapters()


@pytest.fixture()
def fake_fal(monkeypatch):
    """A FakeImageAdapter registered as 'fal', with FALAI_API_KEY set."""
    adapter = FakeImageAdapter()
    register_image_adapter("fal", adapter)
    monkeypatch.setenv("FALAI_API_KEY", "test-fal-secret-value")
    return adapter


def _noop_tools_allowed(api_url, model_name, **kwargs):
    return None


def _make_agent(db_session, username, *, tier="regular", config=None, enabled=False):
    agent = Agent(
        user_username=username,
        autonomy_tier=tier,
        is_enabled=enabled,
        status="idle",
        config=config or {"min_delay": 60, "max_delay": 900, "max_actions_per_run": 30},
        state={},
        consecutive_failures=0,
        next_run_at=datetime.utcnow() if enabled else None,
    )
    db_session.add(agent)
    db_session.commit()
    return agent


def _make_provider(
    db_session,
    *,
    name="Fal",
    default_model="fal-ai/flux-1/schnell",
    is_enabled=True,
    credential_env="FALAI_API_KEY",
):
    provider = ImageProvider(
        name=name,
        provider_type="fal",
        credential_env=credential_env,
        default_model=default_model,
        is_enabled=is_enabled,
    )
    db_session.add(provider)
    db_session.commit()
    return provider


# --- Existing agents remain image-disabled ------------------------------------


def test_existing_agent_has_no_image_posts_key(seeded_db, db_session):
    agent = _make_agent(db_session, "alice")
    assert "image_posts" not in (agent.config or {})


# --- Create: enabling image posts ----------------------------------------------


def test_create_agent_with_image_posts_falls_back_to_provider_default(
    seeded_db, admin_client, db_session, monkeypatch, fake_fal
):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)
    provider = _make_provider(db_session)

    resp = admin_client.post(
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
    assert resp.status_code == 201
    agent_data = resp.get_json()["agent"]
    assert agent_data["config"]["image_posts"] == {
        "enabled": True,
        "provider_id": provider.id,
        "model": None,
        "policy": "optional",
    }

    stored = Agent.query.filter_by(user_username="alice").first()
    assert stored.config["image_posts"]["model"] is None


def test_create_agent_with_image_posts_model_override_validates(
    seeded_db, admin_client, db_session, monkeypatch, fake_fal
):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)
    provider = _make_provider(db_session)
    fake_fal.enqueue_validate(ModelValidation(compatible=True))

    resp = admin_client.post(
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
    assert resp.status_code == 201
    agent_data = resp.get_json()["agent"]
    assert agent_data["config"]["image_posts"]["model"] == "fal-ai/flux-1/dev"
    assert agent_data["config"]["image_posts"]["policy"] == "image_only"
    assert fake_fal.validate_calls[0]["model_id"] == "fal-ai/flux-1/dev"


def test_create_agent_rejects_lurker_with_image_posts(
    seeded_db, admin_client, db_session, monkeypatch, fake_fal
):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)
    provider = _make_provider(db_session)

    resp = admin_client.post(
        "/admin/api/agents",
        json={
            "username": "alice",
            "backfill_memory": False,
            "autonomy_tier": "lurker",
            "image_posts": {"enabled": True, "provider_id": provider.id},
        },
    )
    assert resp.status_code == 400
    assert "lurker" in resp.get_json()["error"]
    assert Agent.query.filter_by(user_username="alice").first() is None


def test_create_agent_rejects_missing_provider_id(
    seeded_db, admin_client, db_session, monkeypatch
):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)

    resp = admin_client.post(
        "/admin/api/agents",
        json={
            "username": "alice",
            "backfill_memory": False,
            "image_posts": {"enabled": True},
        },
    )
    assert resp.status_code == 400
    assert "provider_id" in resp.get_json()["error"]
    assert Agent.query.filter_by(user_username="alice").first() is None


def test_create_agent_rejects_unknown_provider_id(
    seeded_db, admin_client, db_session, monkeypatch
):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)

    resp = admin_client.post(
        "/admin/api/agents",
        json={
            "username": "alice",
            "backfill_memory": False,
            "image_posts": {"enabled": True, "provider_id": 999999},
        },
    )
    assert resp.status_code == 400
    assert "not found" in resp.get_json()["error"]


def test_create_agent_rejects_disabled_provider(
    seeded_db, admin_client, db_session, monkeypatch, fake_fal
):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)
    provider = _make_provider(db_session, is_enabled=False)

    resp = admin_client.post(
        "/admin/api/agents",
        json={
            "username": "alice",
            "backfill_memory": False,
            "image_posts": {"enabled": True, "provider_id": provider.id},
        },
    )
    assert resp.status_code == 400
    assert "disabled" in resp.get_json()["error"]


def test_create_agent_rejects_missing_credential(
    seeded_db, admin_client, db_session, monkeypatch
):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)
    register_image_adapter("fal", FakeImageAdapter())
    provider = _make_provider(db_session)  # FALAI_API_KEY intentionally unset

    resp = admin_client.post(
        "/admin/api/agents",
        json={
            "username": "alice",
            "backfill_memory": False,
            "image_posts": {"enabled": True, "provider_id": provider.id},
        },
    )
    assert resp.status_code == 400
    assert "credential" in resp.get_json()["error"]


def test_create_agent_rejects_incompatible_model_override(
    seeded_db, admin_client, db_session, monkeypatch, fake_fal
):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)
    provider = _make_provider(db_session)
    fake_fal.enqueue_validate(
        ModelValidation(compatible=False, reason="no prompt input")
    )

    resp = admin_client.post(
        "/admin/api/agents",
        json={
            "username": "alice",
            "backfill_memory": False,
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "model": "not-a-real-model",
            },
        },
    )
    assert resp.status_code == 400
    assert "no prompt input" in resp.get_json()["error"]


def test_create_agent_rejects_missing_default_model_without_override(
    seeded_db, admin_client, db_session, monkeypatch, fake_fal
):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)
    provider = _make_provider(db_session, default_model=None)

    resp = admin_client.post(
        "/admin/api/agents",
        json={
            "username": "alice",
            "backfill_memory": False,
            "image_posts": {"enabled": True, "provider_id": provider.id},
        },
    )
    assert resp.status_code == 400
    assert "default" in resp.get_json()["error"]


def test_create_agent_rejects_invalid_policy(
    seeded_db, admin_client, db_session, monkeypatch, fake_fal
):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)
    provider = _make_provider(db_session)

    resp = admin_client.post(
        "/admin/api/agents",
        json={
            "username": "alice",
            "backfill_memory": False,
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "policy": "always",
            },
        },
    )
    assert resp.status_code == 400
    assert "policy" in resp.get_json()["error"]


def test_create_agent_without_image_posts_key_stays_disabled(
    seeded_db, admin_client, db_session, monkeypatch
):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)

    resp = admin_client.post(
        "/admin/api/agents", json={"username": "alice", "backfill_memory": False}
    )
    assert resp.status_code == 201
    assert "image_posts" not in resp.get_json()["agent"]["config"]


# --- Update: enabling, disabling, and preserving other keys --------------------


def test_update_agent_enable_image_posts_round_trip(
    seeded_db, admin_client, db_session, fake_fal
):
    agent = _make_agent(db_session, "bob", config={"min_delay": 60, "max_delay": 900})
    provider = _make_provider(db_session)
    fake_fal.enqueue_validate(ModelValidation(compatible=True))

    resp = admin_client.put(
        f"/admin/api/agents/{agent.id}",
        json={
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "model": "fal-ai/flux-1/dev",
                "policy": "optional",
            }
        },
    )
    assert resp.status_code == 200
    db.session.refresh(agent)
    assert agent.config["image_posts"] == {
        "enabled": True,
        "provider_id": provider.id,
        "model": "fal-ai/flux-1/dev",
        "policy": "optional",
    }
    # Unrelated existing config keys survive the update.
    assert agent.config["min_delay"] == 60
    assert agent.config["max_delay"] == 900


def test_update_agent_disable_removes_image_posts_key(
    seeded_db, admin_client, db_session
):
    provider = _make_provider(db_session)
    agent = _make_agent(
        db_session,
        "bob",
        config={
            "min_delay": 60,
            "max_delay": 900,
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "model": None,
                "policy": "optional",
            },
        },
    )

    resp = admin_client.put(
        f"/admin/api/agents/{agent.id}",
        json={"image_posts": {"enabled": False}},
    )
    assert resp.status_code == 200
    db.session.refresh(agent)
    assert "image_posts" not in agent.config
    assert agent.config["min_delay"] == 60


def test_update_agent_not_touching_image_posts_preserves_it(
    seeded_db, admin_client, db_session
):
    provider = _make_provider(db_session)
    image_cfg = {
        "enabled": True,
        "provider_id": provider.id,
        "model": None,
        "policy": "optional",
    }
    agent = _make_agent(
        db_session,
        "bob",
        config={"min_delay": 60, "max_delay": 900, "image_posts": dict(image_cfg)},
    )

    resp = admin_client.put(f"/admin/api/agents/{agent.id}", json={"min_delay": 120})
    assert resp.status_code == 200
    db.session.refresh(agent)
    assert agent.config["image_posts"] == image_cfg
    assert agent.config["min_delay"] == 120


def test_update_agent_rejects_switch_to_lurker_while_image_posts_enabled(
    seeded_db, admin_client, db_session
):
    provider = _make_provider(db_session)
    agent = _make_agent(
        db_session,
        "bob",
        config={
            "min_delay": 60,
            "max_delay": 900,
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "model": None,
                "policy": "optional",
            },
        },
    )

    resp = admin_client.put(
        f"/admin/api/agents/{agent.id}", json={"autonomy_tier": "lurker"}
    )
    assert resp.status_code == 400
    assert "lurker" in resp.get_json()["error"]


def test_update_agent_rejects_enabling_with_deleted_provider(
    seeded_db, admin_client, db_session
):
    agent = _make_agent(db_session, "bob")

    resp = admin_client.put(
        f"/admin/api/agents/{agent.id}",
        json={"image_posts": {"enabled": True, "provider_id": 999999}},
    )
    assert resp.status_code == 400
    assert "not found" in resp.get_json()["error"]
    db.session.refresh(agent)
    assert "image_posts" not in agent.config


def test_update_agent_rejects_invalid_image_posts_shape(
    seeded_db, admin_client, db_session
):
    agent = _make_agent(db_session, "bob")

    resp = admin_client.put(
        f"/admin/api/agents/{agent.id}", json={"image_posts": "yes please"}
    )
    assert resp.status_code == 400
    assert "object" in resp.get_json()["error"]


# --- Template rendering: new form fields are present ---------------------------


def test_agents_create_form_renders_image_fields(seeded_db, admin_client, db_session):
    resp = admin_client.get("/admin/agents")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "image-enabled-check" in html
    assert "image-provider-select" in html
    assert "image-model-input" in html
    assert "image-policy-select" in html


def test_agent_detail_edit_form_renders_image_fields(
    seeded_db, admin_client, db_session
):
    agent = _make_agent(db_session, "alice")
    resp = admin_client.get(f"/admin/agents/{agent.user_username}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "edit-image-enabled-switch" in html
    assert "edit-image-provider-select" in html
    assert "edit-image-model" in html
    assert "edit-image-policy-select" in html
