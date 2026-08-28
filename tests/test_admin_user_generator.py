"""Tests for admin user & persona generator and agent auto-enrollment."""

from __future__ import annotations

import json

import pytest

from deaddit.extensions import db
from deaddit.models import Agent, User
from deaddit.services.persona_generator import (
    PersonaGenerationError,
    generate_personas,
)


@pytest.fixture()
def admin_client(client):
    """Client authenticated for admin endpoints."""
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


SAMPLE_PERSONAS_JSON = json.dumps(
    [
        {
            "username": "coffeecoder",
            "bio": "Espresso enthusiast building distributed systems.",
            "age": 29,
            "gender": "Male",
            "occupation": "Software Architect",
            "education": "M.S. Computer Science",
            "interests": ["specialty coffee", "rust", "rock climbing"],
            "personality_traits": ["analytical", "methodical", "curious"],
            "writing_style": "concise, direct, uses code snippets",
        },
        {
            "username": "sarah_diy",
            "bio": "Restoring vintage motorcycles and woodworking in my garage.",
            "age": 34,
            "gender": "Female",
            "occupation": "Mechanical Engineer",
            "education": "B.S. Mechanical Engineering",
            "interests": ["woodworking", "motorcycles", "3d printing"],
            "personality_traits": ["practical", "resourceful", "encouraging"],
            "writing_style": "friendly, detailed instructions, bullet points",
        },
    ]
)


class TestPersonaGeneratorService:
    def test_generate_personas_creates_users_and_agents(self, app, fake_llm):
        fake_llm.enqueue_content(SAMPLE_PERSONAS_JSON)

        result = generate_personas(
            count=2,
            topic_hint="makers and coders",
            auto_create_agent=True,
            tier="power_user",
            troll_mode="no_troll",
        )

        assert len(result["users"]) == 2
        assert len(result["agents"]) == 2

        # Verify DB persistence of users
        u1 = User.query.filter_by(username="coffeecoder").first()
        assert u1 is not None
        assert u1.age == 29
        assert u1.gender == "Male"
        assert u1.occupation == "Software Architect"
        assert u1.education == "M.S. Computer Science"
        assert u1.get_interests() == ["specialty coffee", "rust", "rock climbing"]
        assert u1.get_personality_traits() == ["analytical", "methodical", "curious"]

        u2 = User.query.filter_by(username="sarah_diy").first()
        assert u2 is not None
        assert u2.gender == "Female"
        assert u2.age == 34

        # Verify Agent auto-enrollment with default config requirements
        a1 = Agent.query.filter_by(user_username="coffeecoder").first()
        assert a1 is not None
        assert a1.autonomy_tier == "power_user"
        assert a1.persona_mode == "fixed"
        assert a1.is_enabled is True
        assert a1.status == "idle"
        assert a1.next_run_at is not None
        assert a1.config["max_actions_per_run"] == 30
        assert a1.config["min_delay"] == 300
        assert a1.config["max_delay"] == 1800
        assert "api_url" in a1.config
        assert "model" in a1.config

        a2 = Agent.query.filter_by(user_username="sarah_diy").first()
        assert a2 is not None
        assert a2.autonomy_tier == "power_user"
        assert a2.persona_mode == "fixed"
        assert a2.is_enabled is True

    def test_generate_personas_without_agent_creation(self, app, fake_llm):
        fake_llm.enqueue_content(SAMPLE_PERSONAS_JSON)

        result = generate_personas(
            count=1,
            auto_create_agent=False,
            troll_mode="no_troll",
        )
        assert len(result["users"]) == 1
        assert len(result["agents"]) == 0

        u1 = User.query.filter_by(username="coffeecoder").first()
        assert u1 is not None
        assert Agent.query.filter_by(user_username="coffeecoder").first() is None

    def test_generate_personas_handles_codeblock_wrapping(self, app, fake_llm):
        wrapped = f"```json\n{SAMPLE_PERSONAS_JSON}\n```"
        fake_llm.enqueue_content(wrapped)

        result = generate_personas(
            count=2, auto_create_agent=True, tier="regular", troll_mode="no_troll"
        )
        assert len(result["users"]) == 2
        assert len(result["agents"]) == 2
        assert result["agents"][0]["autonomy_tier"] == "regular"

    def test_generate_personas_handles_duplicate_usernames(self, app, fake_llm):
        # Seed an existing user with username "coffeecoder"
        existing = User(
            username="coffeecoder",
            bio="Original coffee coder",
            age=40,
            gender="Male",
        )
        db.session.add(existing)
        db.session.commit()

        fake_llm.enqueue_content(SAMPLE_PERSONAS_JSON)

        result = generate_personas(
            count=2, auto_create_agent=True, troll_mode="no_troll"
        )
        # Should generate a non-colliding username
        usernames = [u["username"] for u in result["users"]]
        assert "coffeecoder" not in usernames
        assert any(u.startswith("coffeecoder_") for u in usernames)

    def test_count_and_tier_validation_limits(self, app):
        with pytest.raises(ValueError, match="between 1 and 500"):
            generate_personas(count=0)

        with pytest.raises(ValueError, match="between 1 and 500"):
            generate_personas(count=501)

        with pytest.raises(ValueError, match="Invalid tier"):
            generate_personas(count=2, tier="invalid_tier")

    def test_malformed_llm_response_raises_error(self, app, fake_llm):
        fake_llm.enqueue_content("Sorry, I cannot help with this request.")
        with pytest.raises(PersonaGenerationError):
            generate_personas(count=1)


class TestAdminUserGeneratorAPI:
    def test_unauthenticated_request_rejected(self, client, monkeypatch):
        monkeypatch.setenv("API_TOKEN", "unit-test-admin-token")
        resp = client.post(
            "/admin/api/users/generate",
            json={"count": 2, "auto_create_agent": True, "tier": "regular"},
        )
        assert resp.status_code == 302
        assert "/admin/login" in resp.headers.get("Location", "")

    def test_api_validation_errors(self, admin_client):
        # Count too low
        resp = admin_client.post("/admin/api/users/generate", json={"count": 0})
        assert resp.status_code == 400
        assert "between 1 and 500" in resp.get_json()["error"]

        # Count too high
        resp = admin_client.post("/admin/api/users/generate", json={"count": 501})
        assert resp.status_code == 400
        assert "between 1 and 500" in resp.get_json()["error"]

        # Non-integer count
        resp = admin_client.post("/admin/api/users/generate", json={"count": "three"})
        assert resp.status_code == 400

        # Invalid tier
        resp = admin_client.post(
            "/admin/api/users/generate",
            json={"count": 2, "tier": "ultra_admin"},
        )
        assert resp.status_code == 400
        assert "Unknown tier" in resp.get_json()["error"]

    def test_api_generate_success(self, admin_client, fake_llm):
        fake_llm.enqueue_content(SAMPLE_PERSONAS_JSON)

        payload = {
            "count": 2,
            "auto_create_agent": True,
            "tier": "lurker",
            "topic_hint": "coffee lovers",
            "troll_mode": "no_troll",
        }
        resp = admin_client.post("/admin/api/users/generate", json=payload)
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["users"]) == 2
        assert len(data["agents"]) == 2

        # Verify agent tier and config
        for agent in data["agents"]:
            assert agent["autonomy_tier"] == "lurker"
            assert agent["is_enabled"] is True
            assert agent["config"]["max_actions_per_run"] == 30
            assert agent["config"]["min_delay"] == 300
            assert agent["config"]["max_delay"] == 1800

        # Verify LLM request included topic hint
        assert len(fake_llm.requests) == 1
        prompt = fake_llm.requests[0]["payload"]["messages"][1]["content"]
        assert "coffee lovers" in prompt

    def test_api_generate_without_agents(self, admin_client, fake_llm):
        fake_llm.enqueue_content(SAMPLE_PERSONAS_JSON)

        payload = {
            "count": 1,
            "auto_create_agent": False,
            "troll_mode": "no_troll",
        }
        resp = admin_client.post("/admin/api/users/generate", json=payload)
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["users"]) == 1
        assert len(data["agents"]) == 0

    def test_api_generate_defaults_to_personas_only(self, admin_client, fake_llm):
        """Omitting auto_create_agent must NOT enroll agents (admin UI default)."""
        fake_llm.enqueue_content(SAMPLE_PERSONAS_JSON)

        resp = admin_client.post(
            "/admin/api/users/generate",
            json={"count": 1, "troll_mode": "no_troll"},
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["users"]) == 1
        assert data["agents"] == []
        assert Agent.query.count() == 0

    def test_generate_personas_batches_over_ten(self, app, fake_llm):
        # Enqueue two batches of 2 personas each
        fake_llm.enqueue_content(SAMPLE_PERSONAS_JSON)
        fake_llm.enqueue_content(
            json.dumps(
                [
                    {
                        "username": "gamer3",
                        "bio": "Gamer",
                        "age": 22,
                        "gender": "Female",
                    },
                    {
                        "username": "gamer4",
                        "bio": "Gamer",
                        "age": 25,
                        "gender": "Male",
                    },
                ]
            )
        )
        result = generate_personas(
            count=3, auto_create_agent=False, troll_mode="no_troll"
        )
        assert len(result["users"]) == 3
