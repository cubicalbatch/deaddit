"""Admin CRUD and model search for image providers (Phase 3A).

Every test here registers a FakeImageAdapter via
deaddit.images.client.register_adapter()/reset_adapters() - the seam Phase
1C built for exactly this purpose - so nothing ever reaches fal.ai or
Runware. The autouse conftest network guard would fail any real egress
attempt anyway.
"""

from __future__ import annotations

import pytest

from deaddit.extensions import db
from deaddit.images.client import register_adapter as register_image_adapter
from deaddit.images.client import reset_adapters
from deaddit.images.types import ModelOption, ModelSearchResult, ModelValidation
from deaddit.models import Agent, ImageModel, ImageProvider, User
from tests.fakes import FakeImageAdapter


@pytest.fixture()
def admin_client(client):
    """Client authenticated as admin."""
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


@pytest.fixture(autouse=True)
def _clean_adapter_registry():
    """Every test starts and ends with an empty image-adapter registry."""
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


# --- Create -----------------------------------------------------------------


def test_create_defaults_credential_env_by_provider_type(admin_client, app):
    with app.app_context():
        res = admin_client.post(
            "/admin/api/image-providers",
            json={"name": "My Fal", "provider_type": "fal"},
        )
        assert res.status_code == 201
        provider = res.get_json()["provider"]
        assert provider["credential_env"] == "FALAI_API_KEY"
        assert provider["provider_type"] == "fal"
        assert provider["default_model"] is None
        assert provider["credential_set"] is False


def test_create_rejects_unknown_provider_type(admin_client, app):
    with app.app_context():
        res = admin_client.post(
            "/admin/api/image-providers",
            json={"name": "Bad", "provider_type": "midjourney"},
        )
        assert res.status_code == 400
        assert ImageProvider.query.count() == 0


def test_create_rejects_duplicate_name(admin_client, app):
    with app.app_context():
        db.session.add(
            ImageProvider(
                name="Dup", provider_type="fal", credential_env="FALAI_API_KEY"
            )
        )
        db.session.commit()

        res = admin_client.post(
            "/admin/api/image-providers",
            json={"name": "Dup", "provider_type": "runware"},
        )
        assert res.status_code == 400
        assert ImageProvider.query.count() == 1


def test_create_rejects_malformed_credential_env(admin_client, app):
    with app.app_context():
        res = admin_client.post(
            "/admin/api/image-providers",
            json={
                "name": "Bad Env",
                "provider_type": "fal",
                "credential_env": "not a valid name!",
            },
        )
        assert res.status_code == 400
        assert ImageProvider.query.count() == 0


def test_create_allows_custom_credential_env_for_second_account(admin_client, app):
    with app.app_context():
        res = admin_client.post(
            "/admin/api/image-providers",
            json={
                "name": "Fal Second Account",
                "provider_type": "fal",
                "credential_env": "FALAI_API_KEY_2",
            },
        )
        assert res.status_code == 201
        assert res.get_json()["provider"]["credential_env"] == "FALAI_API_KEY_2"


def test_create_response_never_leaks_credential_value(admin_client, app, fake_fal):
    with app.app_context():
        fake_fal.enqueue_validate(ModelValidation(compatible=True))
        res = admin_client.post(
            "/admin/api/image-providers",
            json={
                "name": "Fal",
                "provider_type": "fal",
                "default_model": "fal-ai/flux-1/schnell",
            },
        )
        assert res.status_code == 201
        body = res.get_data(as_text=True)
        assert "test-fal-secret-value" not in body
        provider = res.get_json()["provider"]
        assert "api_key" not in provider
        assert "credential" not in provider
        assert "secret" not in provider
        assert provider["credential_set"] is True


def test_create_with_default_model_requires_validation(admin_client, app, fake_fal):
    """An incompatible model is rejected and nothing is persisted."""
    with app.app_context():
        fake_fal.enqueue_validate(
            ModelValidation(compatible=False, reason="no prompt input")
        )
        res = admin_client.post(
            "/admin/api/image-providers",
            json={
                "name": "Fal",
                "provider_type": "fal",
                "default_model": "not-a-real-model",
            },
        )
        assert res.status_code == 400
        assert "no prompt input" in res.get_json()["error"]
        assert ImageProvider.query.count() == 0


def test_create_with_default_model_fails_closed_without_credential(admin_client, app):
    """provider_type has an adapter but no credential is set: fails before any network call."""
    with app.app_context():
        register_image_adapter("fal", FakeImageAdapter())
        res = admin_client.post(
            "/admin/api/image-providers",
            json={
                "name": "Fal",
                "provider_type": "fal",
                "default_model": "fal-ai/flux-1/schnell",
            },
        )
        assert res.status_code == 400
        assert ImageProvider.query.count() == 0


def test_create_with_default_model_fails_closed_without_registered_adapter(
    admin_client, app
):
    """No adapter registered at all: still fails closed, never an unhandled exception."""
    with app.app_context():
        res = admin_client.post(
            "/admin/api/image-providers",
            json={
                "name": "Runware",
                "provider_type": "runware",
                "credential_env": "RUNWARE_API_KEY",
                "default_model": "civitai:1@1",
            },
        )
        assert res.status_code == 400
        assert ImageProvider.query.count() == 0


# --- List / get ---------------------------------------------------------------


def test_list_and_get(admin_client, app):
    with app.app_context():
        provider = ImageProvider(
            name="Listed", provider_type="runware", credential_env="RUNWARE_API_KEY"
        )
        db.session.add(provider)
        db.session.commit()

        res = admin_client.get("/admin/api/image-providers")
        assert res.status_code == 200
        providers = res.get_json()["providers"]
        assert len(providers) == 1
        assert providers[0]["name"] == "Listed"
        assert providers[0]["cached_model_count"] == 0

        res = admin_client.get(f"/admin/api/image-providers/{provider.id}")
        assert res.status_code == 200
        assert res.get_json()["provider"]["name"] == "Listed"

        res = admin_client.get("/admin/api/image-providers/999999")
        assert res.status_code == 404


# --- Update -------------------------------------------------------------------


def test_update_simple_fields(admin_client, app):
    with app.app_context():
        provider = ImageProvider(
            name="Original", provider_type="fal", credential_env="FALAI_API_KEY"
        )
        db.session.add(provider)
        db.session.commit()

        res = admin_client.put(
            f"/admin/api/image-providers/{provider.id}",
            json={"name": "Renamed", "is_enabled": False},
        )
        assert res.status_code == 200
        data = res.get_json()["provider"]
        assert data["name"] == "Renamed"
        assert data["is_enabled"] is False


def test_update_clears_default_model_with_empty_string(admin_client, app, fake_fal):
    with app.app_context():
        fake_fal.enqueue_validate(ModelValidation(compatible=True))
        provider = ImageProvider(
            name="Fal",
            provider_type="fal",
            credential_env="FALAI_API_KEY",
            default_model="fal-ai/flux-1/schnell",
        )
        db.session.add(provider)
        db.session.commit()

        res = admin_client.put(
            f"/admin/api/image-providers/{provider.id}",
            json={"default_model": ""},
        )
        assert res.status_code == 200
        assert res.get_json()["provider"]["default_model"] is None


def test_update_rolls_back_everything_when_default_model_invalid(
    admin_client, app, fake_fal
):
    """All-or-nothing: a bad default_model must not leave a partial update."""
    with app.app_context():
        provider = ImageProvider(
            name="Original",
            provider_type="fal",
            credential_env="FALAI_API_KEY",
            is_enabled=True,
        )
        db.session.add(provider)
        db.session.commit()
        provider_id = provider.id

        fake_fal.enqueue_validate(ModelValidation(compatible=False, reason="bad model"))
        res = admin_client.put(
            f"/admin/api/image-providers/{provider_id}",
            json={
                "name": "Should Not Stick",
                "is_enabled": False,
                "default_model": "bogus-model",
            },
        )
        assert res.status_code == 400

        db.session.expire_all()
        reloaded = db.session.get(ImageProvider, provider_id)
        assert reloaded.name == "Original"
        assert reloaded.is_enabled is True
        assert reloaded.default_model is None


def test_update_rejects_duplicate_name(admin_client, app):
    with app.app_context():
        db.session.add_all(
            [
                ImageProvider(
                    name="First", provider_type="fal", credential_env="FALAI_API_KEY"
                ),
                ImageProvider(
                    name="Second",
                    provider_type="runware",
                    credential_env="RUNWARE_API_KEY",
                ),
            ]
        )
        db.session.commit()
        second = ImageProvider.query.filter_by(name="Second").first()

        res = admin_client.put(
            f"/admin/api/image-providers/{second.id}",
            json={"name": "First"},
        )
        assert res.status_code == 400


def test_update_unknown_provider_404(admin_client, app):
    with app.app_context():
        res = admin_client.put("/admin/api/image-providers/999999", json={"name": "x"})
        assert res.status_code == 404


# --- Delete ---------------------------------------------------------------


def test_delete_succeeds_when_unreferenced(admin_client, app):
    with app.app_context():
        provider = ImageProvider(
            name="Deletable", provider_type="fal", credential_env="FALAI_API_KEY"
        )
        db.session.add(provider)
        db.session.commit()
        provider_id = provider.id

        res = admin_client.delete(f"/admin/api/image-providers/{provider_id}")
        assert res.status_code == 200
        assert db.session.get(ImageProvider, provider_id) is None


def test_delete_blocked_while_agent_config_references_provider(admin_client, app):
    with app.app_context():
        provider = ImageProvider(
            name="In Use", provider_type="fal", credential_env="FALAI_API_KEY"
        )
        db.session.add(provider)
        db.session.commit()

        user = User(username="image-agent-user")
        db.session.add(user)
        db.session.commit()
        agent = Agent(
            user_username=user.username,
            config={"image_posts": {"enabled": True, "provider_id": provider.id}},
        )
        db.session.add(agent)
        db.session.commit()

        res = admin_client.delete(f"/admin/api/image-providers/{provider.id}")
        assert res.status_code == 400
        assert "image-agent-user" in res.get_json()["error"]
        assert db.session.get(ImageProvider, provider.id) is not None

        # Clearing the reference allows deletion.
        agent.config = {}
        db.session.commit()
        res = admin_client.delete(f"/admin/api/image-providers/{provider.id}")
        assert res.status_code == 200


def test_delete_unknown_provider_404(admin_client, app):
    with app.app_context():
        res = admin_client.delete("/admin/api/image-providers/999999")
        assert res.status_code == 404


# --- Model search (bounded) ---------------------------------------------------


def test_model_search_returns_only_the_adapter_supplied_page(
    admin_client, app, fake_fal
):
    with app.app_context():
        provider = ImageProvider(
            name="Fal", provider_type="fal", credential_env="FALAI_API_KEY"
        )
        db.session.add(provider)
        db.session.commit()

        fake_fal.enqueue_search(
            ModelSearchResult(
                options=[
                    ModelOption(
                        model_id="fal-ai/flux-1/schnell", display_name="Flux Schnell"
                    ),
                    ModelOption(model_id="fal-ai/flux-1/dev", display_name="Flux Dev"),
                ],
                next_cursor="page-2",
            )
        )

        res = admin_client.get(
            f"/admin/api/image-providers/{provider.id}/models?q=flux"
        )
        assert res.status_code == 200
        data = res.get_json()
        assert len(data["options"]) == 2
        assert data["next_cursor"] == "page-2"
        assert fake_fal.search_calls[0]["query"] == "flux"
        assert fake_fal.search_calls[0]["cursor"] is None

        # The bounded page was cached for admin visibility.
        cached = ImageModel.query.filter_by(provider_id=provider.id).all()
        assert {m.model_identifier for m in cached} == {
            "fal-ai/flux-1/schnell",
            "fal-ai/flux-1/dev",
        }


def test_model_search_forwards_cursor_for_next_page(admin_client, app, fake_fal):
    with app.app_context():
        provider = ImageProvider(
            name="Fal", provider_type="fal", credential_env="FALAI_API_KEY"
        )
        db.session.add(provider)
        db.session.commit()

        fake_fal.enqueue_search(ModelSearchResult(options=[], next_cursor=None))
        res = admin_client.get(
            f"/admin/api/image-providers/{provider.id}/models?cursor=page-2"
        )
        assert res.status_code == 200
        assert fake_fal.search_calls[0]["cursor"] == "page-2"
        assert res.get_json()["next_cursor"] is None


def test_model_search_fails_closed_without_credential(admin_client, app):
    with app.app_context():
        register_image_adapter("fal", FakeImageAdapter())
        provider = ImageProvider(
            name="Fal", provider_type="fal", credential_env="FALAI_API_KEY"
        )
        db.session.add(provider)
        db.session.commit()

        res = admin_client.get(f"/admin/api/image-providers/{provider.id}/models")
        assert res.status_code == 400


def test_model_search_unknown_provider_404(admin_client, app):
    with app.app_context():
        res = admin_client.get("/admin/api/image-providers/999999/models")
        assert res.status_code == 404


# --- Manual model validation ---------------------------------------------------


def test_validate_model_compatible(admin_client, app, fake_fal):
    with app.app_context():
        provider = ImageProvider(
            name="Fal", provider_type="fal", credential_env="FALAI_API_KEY"
        )
        db.session.add(provider)
        db.session.commit()

        fake_fal.enqueue_validate(ModelValidation(compatible=True))
        res = admin_client.post(
            f"/admin/api/image-providers/{provider.id}/models/validate",
            json={"model_id": "fal-ai/flux-1/schnell"},
        )
        assert res.status_code == 200
        body = res.get_json()
        assert body["success"] is True
        assert body["compatible"] is True
        assert fake_fal.validate_calls[0]["model_id"] == "fal-ai/flux-1/schnell"


def test_validate_model_incompatible(admin_client, app, fake_fal):
    with app.app_context():
        provider = ImageProvider(
            name="Fal", provider_type="fal", credential_env="FALAI_API_KEY"
        )
        db.session.add(provider)
        db.session.commit()

        fake_fal.enqueue_validate(
            ModelValidation(compatible=False, reason="no images output")
        )
        res = admin_client.post(
            f"/admin/api/image-providers/{provider.id}/models/validate",
            json={"model_id": "fal-ai/some-video-model"},
        )
        assert res.status_code == 200
        body = res.get_json()
        assert body["compatible"] is False
        assert body["reason"] == "no images output"


def test_validate_model_requires_model_id(admin_client, app):
    with app.app_context():
        provider = ImageProvider(
            name="Fal", provider_type="fal", credential_env="FALAI_API_KEY"
        )
        db.session.add(provider)
        db.session.commit()

        res = admin_client.post(
            f"/admin/api/image-providers/{provider.id}/models/validate", json={}
        )
        assert res.status_code == 400


# --- Connection test (search only, never generation) ---------------------------


def test_connection_test_by_provider_id(admin_client, app, fake_fal):
    with app.app_context():
        provider = ImageProvider(
            name="Fal", provider_type="fal", credential_env="FALAI_API_KEY"
        )
        db.session.add(provider)
        db.session.commit()

        fake_fal.enqueue_search(
            ModelSearchResult(options=[ModelOption(model_id="m1", display_name="M1")])
        )
        res = admin_client.post(
            "/admin/api/image-providers/test-connection",
            json={"provider_id": provider.id},
        )
        assert res.status_code == 200
        body = res.get_json()
        assert body["success"] is True
        assert body["sample_model_ids"] == ["m1"]
        assert not fake_fal.generate_calls  # never generates


def test_connection_test_draft_provider_before_save(admin_client, app, fake_fal):
    with app.app_context():
        fake_fal.enqueue_search(ModelSearchResult(options=[]))
        res = admin_client.post(
            "/admin/api/image-providers/test-connection",
            json={"provider_type": "fal", "credential_env": "FALAI_API_KEY"},
        )
        assert res.status_code == 200
        assert res.get_json()["success"] is True
        assert ImageProvider.query.count() == 0  # never persisted


def test_connection_test_reports_failure_without_raising(admin_client, app):
    with app.app_context():
        # No adapter registered for "runware": dispatch fails closed.
        res = admin_client.post(
            "/admin/api/image-providers/test-connection",
            json={"provider_type": "runware", "credential_env": "RUNWARE_API_KEY"},
        )
        assert res.status_code == 200
        assert res.get_json()["success"] is False


def test_connection_test_unknown_provider_id_404(admin_client, app):
    with app.app_context():
        res = admin_client.post(
            "/admin/api/image-providers/test-connection",
            json={"provider_id": 999999},
        )
        assert res.status_code == 404


# --- Authorization: every new endpoint is gated ---------------------------------


def test_every_image_provider_endpoint_requires_admin_auth(
    app, client, monkeypatch, db_session
):
    monkeypatch.setenv("API_TOKEN", "sekrit-token")

    provider = ImageProvider(
        name="Gated", provider_type="fal", credential_env="FALAI_API_KEY"
    )
    db_session.add(provider)
    db_session.commit()
    provider_id = provider.id

    calls = [
        ("get", "/admin/api/image-providers"),
        ("post", "/admin/api/image-providers"),
        ("post", "/admin/api/image-providers/test-connection"),
        ("get", f"/admin/api/image-providers/{provider_id}"),
        ("put", f"/admin/api/image-providers/{provider_id}"),
        ("get", f"/admin/api/image-providers/{provider_id}/models"),
        ("post", f"/admin/api/image-providers/{provider_id}/models/validate"),
        ("delete", f"/admin/api/image-providers/{provider_id}"),
    ]

    for method, path in calls:
        resp = getattr(client, method)(path, json={})
        assert resp.status_code == 302, f"{method.upper()} {path} was not gated"
        assert "/admin/login" in resp.headers["Location"]

    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True

    # Now authenticated, the same list endpoint works.
    assert client.get("/admin/api/image-providers").status_code == 200
