"""Admin CRUD, model search and connection tests for image providers.

Every test here registers a FakeImageAdapter via
deaddit.images.client.register_adapter()/reset_adapters() - the dispatch seam
exists for exactly this purpose - so nothing ever reaches fal.ai or Runware.
The autouse conftest network guard would fail any real egress attempt anyway.
"""

from __future__ import annotations

import pytest

from deaddit.extensions import db
from deaddit.images.client import register_adapter as register_image_adapter
from deaddit.images.client import reset_adapters
from deaddit.images.types import ModelOption, ModelSearchResult, ModelValidation
from deaddit.models import Agent, ImageModel, ImageProvider, User
from tests.fakes import FakeImageAdapter

PROVIDERS = "/admin/api/image-providers"


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


@pytest.fixture()
def fake_fal(monkeypatch):
    """A FakeImageAdapter registered as 'fal', with FALAI_API_KEY set."""
    adapter = FakeImageAdapter()
    register_image_adapter("fal", adapter)
    monkeypatch.setenv("FALAI_API_KEY", "test-fal-secret-value")
    return adapter


def _provider(name="Fal", provider_type="fal", credential_env="FALAI_API_KEY", **kw):
    provider = ImageProvider(
        name=name, provider_type=provider_type, credential_env=credential_env, **kw
    )
    db.session.add(provider)
    db.session.commit()
    return provider


def test_provider_crud_round_trip_never_exposes_the_credential(
    admin_client, app, fake_fal
):
    with app.app_context():
        # The credential env name is defaulted from the provider type.
        created = admin_client.post(
            PROVIDERS, json={"name": "My Fal", "provider_type": "fal"}
        )
        assert created.status_code == 201
        provider = created.get_json()["provider"]
        assert provider["credential_env"] == "FALAI_API_KEY"
        assert provider["default_model"] is None
        assert provider["credential_set"] is True

        # A second account on the same provider type can name its own variable.
        second = admin_client.post(
            PROVIDERS,
            json={
                "name": "Fal Second Account",
                "provider_type": "fal",
                "credential_env": "FALAI_API_KEY_2",
            },
        )
        assert second.status_code == 201
        assert second.get_json()["provider"]["credential_env"] == "FALAI_API_KEY_2"

        # Setting a default model validates it against the provider first.
        fake_fal.enqueue_validate(ModelValidation(compatible=True))
        with_model = admin_client.post(
            PROVIDERS,
            json={
                "name": "Fal With Model",
                "provider_type": "fal",
                "default_model": "fal-ai/flux-1/schnell",
            },
        )
        assert with_model.status_code == 201
        # The secret value itself never appears anywhere in the response.
        assert "test-fal-secret-value" not in with_model.get_data(as_text=True)
        body = with_model.get_json()["provider"]
        assert not {"api_key", "credential", "secret"} & body.keys()
        assert body["credential_set"] is True
        provider_id = body["id"]

        listed = admin_client.get(PROVIDERS)
        assert listed.status_code == 200
        assert len(listed.get_json()["providers"]) == 3
        assert listed.get_json()["providers"][0]["cached_model_count"] == 0
        assert admin_client.get(f"{PROVIDERS}/{provider_id}").status_code == 200

        updated = admin_client.put(
            f"{PROVIDERS}/{provider_id}", json={"name": "Renamed", "is_enabled": False}
        )
        assert updated.status_code == 200
        assert updated.get_json()["provider"]["name"] == "Renamed"
        assert updated.get_json()["provider"]["is_enabled"] is False

        # An empty string clears the default model.
        cleared = admin_client.put(
            f"{PROVIDERS}/{provider_id}", json={"default_model": ""}
        )
        assert cleared.status_code == 200
        assert cleared.get_json()["provider"]["default_model"] is None

        assert admin_client.delete(f"{PROVIDERS}/{provider_id}").status_code == 200
        assert db.session.get(ImageProvider, provider_id) is None

        for method, path in (
            ("get", f"{PROVIDERS}/999999"),
            ("put", f"{PROVIDERS}/999999"),
            ("delete", f"{PROVIDERS}/999999"),
            ("get", f"{PROVIDERS}/999999/models"),
        ):
            assert (
                getattr(admin_client, method)(path, json={"name": "x"}).status_code
                == 404
            )


def test_provider_writes_fail_closed_and_never_persist_partial_state(
    admin_client, app, fake_fal
):
    with app.app_context():

        def rejected(payload, *, expected_providers=0):
            res = admin_client.post(PROVIDERS, json=payload)
            assert res.status_code == 400, res.get_data(as_text=True)
            assert ImageProvider.query.count() == expected_providers
            return res

        rejected({"name": "Bad", "provider_type": "midjourney"})
        rejected(
            {
                "name": "Bad Env",
                "provider_type": "fal",
                "credential_env": "not a valid name!",
            }
        )

        # An incompatible default model is rejected and nothing is persisted.
        fake_fal.enqueue_validate(
            ModelValidation(compatible=False, reason="no prompt input")
        )
        res = rejected(
            {"name": "Fal", "provider_type": "fal", "default_model": "not-a-real-model"}
        )
        assert "no prompt input" in res.get_json()["error"]

        # No credential, and no adapter at all: both fail before any network call.
        register_image_adapter("runware", FakeImageAdapter())
        rejected(
            {
                "name": "Runware",
                "provider_type": "runware",
                "credential_env": "RUNWARE_API_KEY",
                "default_model": "civitai:1@1",
            }
        )
        reset_adapters()
        rejected(
            {
                "name": "Runware",
                "provider_type": "runware",
                "credential_env": "RUNWARE_API_KEY",
                "default_model": "civitai:1@1",
            }
        )

        provider = _provider(name="Original", is_enabled=True)
        db.session.add(
            ImageProvider(
                name="Other", provider_type="runware", credential_env="RUNWARE_API_KEY"
            )
        )
        db.session.commit()
        assert (
            admin_client.post(
                PROVIDERS, json={"name": "Original", "provider_type": "fal"}
            ).status_code
            == 400
        ), "duplicate names are rejected on create"
        assert (
            admin_client.put(
                f"{PROVIDERS}/{provider.id}", json={"name": "Other"}
            ).status_code
            == 400
        ), "duplicate names are rejected on update"

        # All-or-nothing: a bad default_model must not leave a partial update.
        register_image_adapter("fal", fake_fal)
        fake_fal.enqueue_validate(ModelValidation(compatible=False, reason="bad model"))
        assert (
            admin_client.put(
                f"{PROVIDERS}/{provider.id}",
                json={
                    "name": "Should Not Stick",
                    "is_enabled": False,
                    "default_model": "bogus",
                },
            ).status_code
            == 400
        )
        db.session.expire_all()
        reloaded = db.session.get(ImageProvider, provider.id)
        assert (reloaded.name, reloaded.is_enabled, reloaded.default_model) == (
            "Original",
            True,
            None,
        )

        # A provider an agent still points at cannot be deleted out from under it.
        db.session.add(User(username="image-agent-user"))
        db.session.commit()
        agent = Agent(
            user_username="image-agent-user",
            config={"image_posts": {"enabled": True, "provider_id": provider.id}},
        )
        db.session.add(agent)
        db.session.commit()
        blocked = admin_client.delete(f"{PROVIDERS}/{provider.id}")
        assert blocked.status_code == 400
        assert "image-agent-user" in blocked.get_json()["error"]
        assert db.session.get(ImageProvider, provider.id) is not None

        agent.config = {}
        db.session.commit()
        assert admin_client.delete(f"{PROVIDERS}/{provider.id}").status_code == 200


def test_model_discovery_endpoints_are_bounded_and_admin_only(
    admin_client, app, fake_fal, monkeypatch
):
    with app.app_context():
        provider = _provider()

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
        page = admin_client.get(f"{PROVIDERS}/{provider.id}/models?q=flux")
        assert page.status_code == 200
        assert len(page.get_json()["options"]) == 2
        assert page.get_json()["next_cursor"] == "page-2"
        assert fake_fal.search_calls[0]["query"] == "flux"
        assert fake_fal.search_calls[0]["cursor"] is None
        # Only the bounded page the adapter returned is cached for admin display.
        assert {
            m.model_identifier
            for m in ImageModel.query.filter_by(provider_id=provider.id)
        } == {
            "fal-ai/flux-1/schnell",
            "fal-ai/flux-1/dev",
        }

        fake_fal.enqueue_search(ModelSearchResult(options=[], next_cursor=None))
        assert (
            admin_client.get(
                f"{PROVIDERS}/{provider.id}/models?cursor=page-2"
            ).status_code
            == 200
        )
        assert fake_fal.search_calls[1]["cursor"] == "page-2"

        fake_fal.enqueue_validate(ModelValidation(compatible=True))
        ok = admin_client.post(
            f"{PROVIDERS}/{provider.id}/models/validate",
            json={"model_id": "fal-ai/flux-1/schnell"},
        )
        assert ok.status_code == 200
        assert ok.get_json()["compatible"] is True
        assert fake_fal.validate_calls[0]["model_id"] == "fal-ai/flux-1/schnell"

        fake_fal.enqueue_validate(
            ModelValidation(compatible=False, reason="no images output")
        )
        bad = admin_client.post(
            f"{PROVIDERS}/{provider.id}/models/validate",
            json={"model_id": "fal-ai/some-video-model"},
        )
        assert bad.get_json()["compatible"] is False
        assert bad.get_json()["reason"] == "no images output"
        assert (
            admin_client.post(
                f"{PROVIDERS}/{provider.id}/models/validate", json={}
            ).status_code
            == 400
        )

        # Connection tests search only - for a saved provider and for a draft.
        fake_fal.enqueue_search(
            ModelSearchResult(options=[ModelOption(model_id="m1", display_name="M1")])
        )
        saved = admin_client.post(
            f"{PROVIDERS}/test-connection", json={"provider_id": provider.id}
        )
        assert saved.status_code == 200
        assert saved.get_json()["sample_model_ids"] == ["m1"]
        assert not fake_fal.generate_calls

        fake_fal.enqueue_search(ModelSearchResult(options=[]))
        draft = admin_client.post(
            f"{PROVIDERS}/test-connection",
            json={"provider_type": "fal", "credential_env": "FALAI_API_KEY"},
        )
        assert draft.status_code == 200 and draft.get_json()["success"] is True
        assert ImageProvider.query.count() == 1, (
            "a draft test never persists a provider"
        )

        # An unregistered type fails closed as a reported failure, not a 500.
        unavailable = admin_client.post(
            f"{PROVIDERS}/test-connection",
            json={"provider_type": "runware", "credential_env": "RUNWARE_API_KEY"},
        )
        assert (
            unavailable.status_code == 200
            and unavailable.get_json()["success"] is False
        )
        assert (
            admin_client.post(
                f"{PROVIDERS}/test-connection", json={"provider_id": 999999}
            ).status_code
            == 404
        )

        monkeypatch.delenv("FALAI_API_KEY", raising=False)
        assert admin_client.get(f"{PROVIDERS}/{provider.id}/models").status_code == 400

    # Every endpoint is gated behind the admin session.
    monkeypatch.setenv("API_TOKEN", "sekrit-token")
    anonymous = app.test_client()
    for method, path in (
        ("get", PROVIDERS),
        ("post", PROVIDERS),
        ("post", f"{PROVIDERS}/test-connection"),
        ("get", f"{PROVIDERS}/1"),
        ("put", f"{PROVIDERS}/1"),
        ("get", f"{PROVIDERS}/1/models"),
        ("post", f"{PROVIDERS}/1/models/validate"),
        ("delete", f"{PROVIDERS}/1"),
    ):
        resp = getattr(anonymous, method)(path, json={})
        assert resp.status_code == 302, f"{method.upper()} {path} was not gated"
        assert "/admin/login" in resp.headers["Location"]
