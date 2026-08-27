"""Tests for LLM Provider admin management, auto-default, model refresh, and agent routing."""

from unittest.mock import patch

import pytest

from deaddit.config import Config
from deaddit.extensions import db
from deaddit.llm import routing
from deaddit.models import ApiModel, LLMProvider, User


@pytest.fixture()
def admin_client(client):
    """Client authenticated as admin."""
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


def test_provider_crud_and_auto_default(admin_client, app):
    """Test creating providers, auto-default behavior, updates, and deletion."""
    with app.app_context():
        # Initially no providers
        res = admin_client.get("/admin/api/providers")
        assert res.status_code == 200
        assert res.get_json()["providers"] == []

        # 1. Create first provider (should become default automatically)
        with patch("deaddit.admin.fetch_all_models_from_api", return_value=(["gpt-4o", "gpt-4o-mini"], "OK")):
            res = admin_client.post(
                "/admin/api/providers",
                json={
                    "name": "OpenAI",
                    "api_url": "https://api.openai.com/v1",
                    "api_key": "sk-secret-key-1234",
                    "default_model": "gpt-4o",
                    "is_default": False,  # Should become True because it is the first provider
                },
            )
        assert res.status_code == 201
        p1_data = res.get_json()["provider"]
        assert p1_data["name"] == "OpenAI"
        assert p1_data["is_default"] is True
        assert p1_data["has_key"] is True
        assert p1_data["key_last4"] == "1234"
        p1_id = p1_data["id"]

        # 2. Create second provider with is_default=False
        with patch("deaddit.admin.fetch_all_models_from_api", return_value=(["llama-3.3-70b-versatile"], "OK")):
            res = admin_client.post(
                "/admin/api/providers",
                json={
                    "name": "Groq",
                    "api_url": "https://api.groq.com/openai/v1",
                    "api_key": "gsk-secret-5678",
                    "default_model": "llama-3.3-70b-versatile",
                    "is_default": False,
                },
            )
        assert res.status_code == 201
        p2_data = res.get_json()["provider"]
        assert p2_data["name"] == "Groq"
        assert p2_data["is_default"] is False
        p2_id = p2_data["id"]

        # Check that OpenAI is still default
        p1 = db.session.get(LLMProvider, p1_id)
        p2 = db.session.get(LLMProvider, p2_id)
        assert p1.is_default is True
        assert p2.is_default is False

        # 3. Set Groq as default
        res = admin_client.post(f"/admin/api/providers/{p2_id}/set-default")
        assert res.status_code == 200
        assert res.get_json()["provider"]["is_default"] is True

        db.session.expire_all()
        p1 = db.session.get(LLMProvider, p1_id)
        p2 = db.session.get(LLMProvider, p2_id)
        assert p1.is_default is False
        assert p2.is_default is True

        # 4. Update provider name and model
        res = admin_client.put(
            f"/admin/api/providers/{p1_id}",
            json={
                "name": "OpenAI Primary",
                "default_model": "gpt-4o-mini",
            },
        )
        assert res.status_code == 200
        p1_updated = res.get_json()["provider"]
        assert p1_updated["name"] == "OpenAI Primary"
        assert p1_updated["default_model"] == "gpt-4o-mini"

        # 5. List providers
        res = admin_client.get("/admin/api/providers")
        assert res.status_code == 200
        providers = res.get_json()["providers"]
        assert len(providers) == 2
        # Default provider comes first
        assert providers[0]["id"] == p2_id
        assert providers[0]["is_default"] is True

        # 6. Delete default provider (Groq) -> remaining provider (OpenAI) should become default
        res = admin_client.delete(f"/admin/api/providers/{p2_id}")
        assert res.status_code == 200

        db.session.expire_all()
        assert db.session.get(LLMProvider, p2_id) is None
        p1 = db.session.get(LLMProvider, p1_id)
        assert p1 is not None
        assert p1.is_default is True

        # 7. Delete last provider
        res = admin_client.delete(f"/admin/api/providers/{p1_id}")
        assert res.status_code == 200
        assert LLMProvider.query.count() == 0


def test_provider_model_refresh_and_retrieval(admin_client, app):
    """Test model refresh and cached model endpoints."""
    with app.app_context():
        provider = LLMProvider(
            name="Ollama Local",
            api_url="http://localhost:11434/v1",
            api_key=None,
            default_model=None,
            is_default=True,
        )
        db.session.add(provider)
        db.session.commit()

        # Mock API returning models
        with patch("deaddit.admin.fetch_all_models_from_api", return_value=(["llama3:latest", "mistral:latest", "phi3:latest"], "Success")):
            res = admin_client.post(f"/admin/api/providers/{provider.id}/refresh-models")
        assert res.status_code == 200
        data = res.get_json()
        assert data["success"] is True
        assert data["models"] == ["llama3:latest", "mistral:latest", "phi3:latest"]
        assert data["count"] == 3

        # Verify cached in database ApiModel table
        cached = ApiModel.get_models_for_api(provider.api_url)
        assert len(cached) == 3

        # Query provider models GET endpoint
        res = admin_client.get(f"/admin/api/providers/{provider.id}/models")
        assert res.status_code == 200
        assert res.get_json()["models"] == ["llama3:latest", "mistral:latest", "phi3:latest"]


def test_provider_connection_test(admin_client, app):
    """Test connection testing endpoint using provider_id and direct url."""
    with app.app_context():
        provider = LLMProvider(
            name="OpenRouter",
            api_url="https://openrouter.ai/api/v1",
            api_key="sk-or-v1-testkey",
            is_default=True,
        )
        db.session.add(provider)
        db.session.commit()

        # Test using provider_id
        with patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            res = admin_client.post("/admin/api/test-connection", json={"provider_id": provider.id})
            assert res.status_code == 200
            assert res.get_json()["success"] is True
            # Verify headers used the provider's api_key
            mock_get.assert_called_once()
            call_kwargs = mock_get.call_args[1]
            assert call_kwargs["headers"]["Authorization"] == "Bearer sk-or-v1-testkey"

        # Test failure response
        with patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 401
            res = admin_client.post("/admin/api/test-connection", json={"provider_id": provider.id})
            assert res.status_code == 200
            assert res.get_json()["success"] is False
            assert "401" in res.get_json()["message"]


def test_config_api_key_resolution_with_providers(app):
    """Test that Config.get_api_key_for_endpoint looks up provider keys from DB."""
    with app.app_context():
        p1 = LLMProvider(
            name="Custom Provider",
            api_url="https://custom.llm.com/v1",
            api_key="secret-key-abc",
            is_default=True,
        )
        p2 = LLMProvider(
            name="Secondary Provider",
            api_url="https://secondary.llm.com/v1",
            api_key="secret-key-xyz",
            is_default=False,
        )
        db.session.add_all([p1, p2])
        db.session.commit()

        # Lookup by matching endpoint URL
        assert Config.get_api_key_for_endpoint("https://custom.llm.com/v1") == "secret-key-abc"
        assert Config.get_api_key_for_endpoint("https://custom.llm.com/v1/") == "secret-key-abc"
        assert Config.get_api_key_for_endpoint("https://secondary.llm.com/v1") == "secret-key-xyz"

        # Lookup with None/empty fallback to default provider
        assert Config.get_api_key_for_endpoint(None) == "secret-key-abc"
        assert Config.get_api_key_for_endpoint("") == "secret-key-abc"


def test_routing_resolve_with_default_provider(app):
    """Test that routing.resolve uses default provider settings."""
    with app.app_context():
        provider = LLMProvider(
            name="Fast Groq",
            api_url="https://api.groq.com/openai/v1",
            api_key="gsk-123",
            default_model="llama-3.3-70b-versatile",
            is_default=True,
        )
        db.session.add(provider)
        db.session.commit()

        # Clear active ModelRoutes that might override
        from deaddit.models import ModelRoute

        ModelRoute.query.delete()
        db.session.commit()
        routing._routes_checked = False

        api_url, model = routing.resolve()
        assert api_url == "https://api.groq.com/openai/v1"
        assert model == "llama-3.3-70b-versatile"


def test_agent_create_and_update_with_provider(admin_client, app):
    """Test creating an agent associated with a provider and updating its provider."""
    with app.app_context():
        user = User(username="test_persona_agent")
        db.session.add(user)

        provider1 = LLMProvider(
            name="Provider One",
            api_url="https://provider1.com/v1",
            api_key="key-one",
            default_model="model-one",
            is_default=True,
        )
        provider2 = LLMProvider(
            name="Provider Two",
            api_url="https://provider2.com/v1",
            api_key="key-two",
            default_model="model-two",
            is_default=False,
        )
        db.session.add_all([provider1, provider2])
        db.session.commit()

        with patch("deaddit.llm.capabilities.ensure_tools_allowed") as mock_ensure:
            # 1. Create agent with provider_id
            res = admin_client.post(
                "/admin/api/agents",
                json={
                    "username": "test_persona_agent",
                    "provider_id": provider1.id,
                    "autonomy_tier": "regular",
                    "model": "model-one",
                    "backfill_memory": False,
                },
            )
            assert res.status_code == 201
            agent_data = res.get_json()["agent"]
            assert agent_data["config"]["provider_id"] == provider1.id
            assert agent_data["config"]["api_url"] == "https://provider1.com/v1"
            assert agent_data["config"]["model"] == "model-one"
            # Ensure capability check received the provider's API key
            mock_ensure.assert_called_with(
                "https://provider1.com/v1",
                "model-one",
                api_key="key-one",
                auto_probe=True,
            )

            # 2. Update agent to switch to Provider Two
            agent_id = agent_data["id"]
            res = admin_client.put(
                f"/admin/api/agents/{agent_id}",
                json={
                    "provider_id": provider2.id,
                    "model": "model-two",
                },
            )
            assert res.status_code == 200
            updated_agent = res.get_json()["agent"]
            assert updated_agent["config"]["provider_id"] == provider2.id
            assert updated_agent["config"]["api_url"] == "https://provider2.com/v1"
            assert updated_agent["config"]["model"] == "model-two"
