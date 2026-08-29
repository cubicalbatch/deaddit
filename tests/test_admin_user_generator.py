"""Tests for admin user & persona generator and agent auto-enrollment."""

from __future__ import annotations

import json
import random
import re

import pytest

from deaddit.extensions import db
from deaddit.models import Agent, Subdeaddit, User
from deaddit.services.persona_generator import (
    PERSONA_BATCH_ATTEMPTS,
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


def _personas_json(n: int) -> str:
    """A JSON array of ``n`` minimal but valid persona dicts."""
    return json.dumps(
        [
            {"username": f"user_{i}", "bio": "A bio", "age": 30, "gender": "Male"}
            for i in range(n)
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

        # Casing post-treatment may alter the LLM username; use stored values
        name1, name2 = (u["username"] for u in result["users"])

        # Verify DB persistence of users
        u1 = User.query.filter(db.func.lower(User.username) == name1.lower()).first()
        assert u1.age == 29
        assert u1.gender == "Male"
        assert u1.occupation == "Software Architect"
        assert u1.education == "M.S. Computer Science"
        assert u1.get_interests() == ["specialty coffee", "rust", "rock climbing"]
        assert u1.get_personality_traits() == ["analytical", "methodical", "curious"]

        u2 = User.query.filter(db.func.lower(User.username) == name2.lower()).first()
        assert u2.gender == "Female"
        assert u2.age == 34

        # Verify Agent auto-enrollment with default config requirements
        a1 = Agent.query.filter(
            db.func.lower(Agent.user_username) == name1.lower()
        ).first()
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

        a2 = Agent.query.filter(
            db.func.lower(Agent.user_username) == name2.lower()
        ).first()
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

        name1 = result["users"][0]["username"]
        u1 = User.query.filter(db.func.lower(User.username) == name1.lower()).first()
        assert u1 is not None
        assert (
            Agent.query.filter(
                db.func.lower(Agent.user_username) == name1.lower()
            ).first()
            is None
        )

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
        usernames = [u["username"] for u in result["users"]]
        # Case-insensitive: suffixed username, never a case-variant collision
        # Casing is applied after suffixing, so the underscore may be
        # absorbed by PascalCase/camelCase (e.g. Coffeecoder1)
        assert any(
            u.lower().startswith("coffeecoder") and u.lower() != "coffeecoder"
            for u in usernames
        )

    def test_count_and_tier_validation_limits(self, app):
        with pytest.raises(ValueError, match="between 1 and 500"):
            generate_personas(count=0)

        with pytest.raises(ValueError, match="between 1 and 500"):
            generate_personas(count=501)

        with pytest.raises(ValueError, match="Invalid tier"):
            generate_personas(count=2, tier="invalid_tier")

    def test_all_batches_failing_still_raises(self, app, fake_llm):
        # Every batch gets PERSONA_BATCH_ATTEMPTS tries; when not a single
        # persona can be created the run still reports a hard failure.
        for _ in range(PERSONA_BATCH_ATTEMPTS):
            fake_llm.enqueue_content("Sorry, I cannot help with this request.")
        with pytest.raises(PersonaGenerationError):
            generate_personas(count=1)
        assert len(fake_llm.requests) == PERSONA_BATCH_ATTEMPTS

    def test_failing_batch_is_retried_then_skipped(self, app, fake_llm):
        # Batch 1 (10 personas) succeeds; batch 2 (2 personas) burns all its
        # attempts -> the run skips it and still returns the 10 created.
        fake_llm.enqueue_content(_personas_json(10))
        for _ in range(PERSONA_BATCH_ATTEMPTS):
            fake_llm.enqueue_content("not json at all")
        result = generate_personas(
            count=12, auto_create_agent=False, troll_mode="no_troll"
        )
        assert len(result["users"]) == 10
        assert result["skipped"] == 2
        assert User.query.count() == 10
        assert len(fake_llm.requests) == 1 + PERSONA_BATCH_ATTEMPTS

    def test_username_style_assignments_in_prompt(self, app, fake_llm, monkeypatch):
        from deaddit.services import persona_generator as pg

        fake_llm.enqueue_content(SAMPLE_PERSONAS_JSON)

        # Freeze the style draw so the two personas demonstrably get
        # different cards (the real draw is uniform and may repeat)
        cards = iter([pg.USERNAME_STYLE_CARDS[0], pg.USERNAME_STYLE_CARDS[1]])
        monkeypatch.setattr(
            pg.random,
            "choices",
            lambda seq, k: [next(c[1] for c in cards if c[1] in seq) for _ in range(k)],
        )
        generate_personas(count=2, auto_create_agent=False, troll_mode="no_troll")

        prompt = fake_llm.requests[-1]["payload"]["messages"][1]["content"]
        assert "Persona 1 username style:" in prompt
        assert "Persona 2 username style:" in prompt
        styles = re.findall(r"Persona \d username style: (.+)", prompt)
        assert len(styles) == 2
        assert styles[0] == pg.USERNAME_STYLE_CARDS[0][1]
        assert styles[1] == pg.USERNAME_STYLE_CARDS[1][1]
        # Anti-pattern ban is present
        assert "chill_dude" in prompt
        assert pg.USERNAME_STYLE_RULES in prompt

    def test_persona_generation_max_tokens(self, app, fake_llm):
        fake_llm.enqueue_content(SAMPLE_PERSONAS_JSON)
        generate_personas(count=1, auto_create_agent=False, troll_mode="no_troll")
        assert fake_llm.requests[-1]["payload"]["max_tokens"] == 16384

    def test_apply_casing_branches(self, monkeypatch):
        from deaddit.services.persona_generator import _apply_casing

        monkeypatch.setattr(random, "random", lambda: 0.1)
        assert _apply_casing("pm_me_your_taco") == "PmMeYourTaco"
        monkeypatch.setattr(random, "random", lambda: 0.3)
        assert _apply_casing("pm_me_your_taco") == "pmMeYourTaco"
        monkeypatch.setattr(random, "random", lambda: 0.9)
        assert _apply_casing("pm_me_your_taco") == "pm_me_your_taco"

    def test_case_insensitive_dedupe_against_db(self, app, fake_llm):
        existing = User(
            username="coffeecoder",
            bio="Original coffee coder",
            age=40,
            gender="Male",
        )
        db.session.add(existing)
        db.session.commit()

        variant = json.loads(SAMPLE_PERSONAS_JSON)
        variant[0]["username"] = "CoffeeCoder"
        fake_llm.enqueue_content(json.dumps(variant))

        result = generate_personas(
            count=1, auto_create_agent=False, troll_mode="no_troll"
        )
        created = result["users"][0]["username"]
        assert created.lower() != "coffeecoder"
        # Casing may absorb the underscore (e.g. Coffeecoder1)
        assert created.lower().startswith("coffeecoder")


@pytest.mark.llm_live
class TestUsernameDiversityLive:
    """Hits the configured LLM endpoint; excluded from deterministic runs."""

    def test_username_diversity_across_batches(self, app):
        from deaddit.services.persona_generator import generate_personas

        all_names: list[str] = []
        for _ in range(3):
            result = generate_personas(
                count=10, auto_create_agent=False, troll_mode="no_troll"
            )
            all_names.extend(u["username"] for u in result["users"])

        lowers = [n.lower() for n in all_names]
        assert len(set(lowers)) >= 0.9 * len(lowers)

        def shape(n: str) -> str:
            if any(c.isdigit() for c in n):
                return "digits"
            if n != n.lower():
                return "mixed"
            if n.count("_") >= 2:
                return "phrase"
            return "simple"

        assert len({shape(n) for n in all_names}) >= 3


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


class TestPersonaSubscriptions:
    """Creation-time LLM-picked subscriptions (initial condition for the
    subscription graph; validated against real communities, never forced)."""

    @pytest.fixture()
    def subs(self, app):
        rows = [
            Subdeaddit(name="books", description="Books, authors, literature."),
            Subdeaddit(name="CasualConversation", description="Casual talk."),
            Subdeaddit(name="localllama", description="Local LLMs."),
        ]
        db.session.add_all(rows)
        db.session.commit()
        return rows

    @staticmethod
    def _persona(username, subscriptions):
        return {
            "username": username,
            "bio": "A bio",
            "age": 30,
            "gender": "Male",
            "subscriptions": subscriptions,
        }

    def test_subscriptions_validated_and_persisted(self, app, fake_llm, subs):
        fake_llm.enqueue_content(
            json.dumps(
                [
                    # Case canonicalization, ghost drop, dedupe.
                    self._persona("user_a", ["Books", "askdaddit", "books"]),
                    # Cap at 3: nosleep is a ghost, the rest survive.
                    self._persona(
                        "user_b",
                        ["localllama", "CasualConversation", "books", "nosleep"],
                    ),
                    # Comma-separated string form with one ghost.
                    self._persona("user_c", "books, quietthoughts"),
                ]
            )
        )

        result = generate_personas(
            count=3, auto_create_agent=False, troll_mode="no_troll"
        )

        # Casing post-treatment may alter the LLM username; match ignoring
        # case and underscores ("user_a" may be stored as "UserA").
        by_key = {u["username"].replace("_", "").lower(): u for u in result["users"]}
        assert by_key["usera"]["subscriptions"] == ["books"]
        assert by_key["userb"]["subscriptions"] == [
            "localllama",
            "CasualConversation",
            "books",
        ]
        assert by_key["userc"]["subscriptions"] == ["books"]

        row_a = User.query.filter_by(username=by_key["usera"]["username"]).first()
        row_b = User.query.filter_by(username=by_key["userb"]["username"]).first()
        assert row_a.agent_state == {"subscriptions": ["books"]}
        assert row_b.agent_state["subscriptions"] == [
            "localllama",
            "CasualConversation",
            "books",
        ]

    def test_missing_or_empty_subscriptions_stay_empty(self, app, fake_llm, subs):
        fake_llm.enqueue_content(
            json.dumps(
                [
                    self._persona("user_d", ["ghostsub", "another_ghost"]),
                    {"username": "user_e", "bio": "A bio", "age": 30},
                ]
            )
        )

        result = generate_personas(
            count=2, auto_create_agent=False, troll_mode="no_troll"
        )

        by_key = {u["username"].replace("_", "").lower(): u for u in result["users"]}
        assert by_key["userd"]["subscriptions"] == []
        assert by_key["usere"]["subscriptions"] == []
        for key in ("userd", "usere"):
            row = User.query.filter_by(username=by_key[key]["username"]).first()
            assert (row.agent_state or {}).get("subscriptions") in (None, [])

    def test_prompt_lists_real_communities(self, app, fake_llm, subs):
        fake_llm.enqueue_content(_personas_json(1))
        generate_personas(count=1, auto_create_agent=False, troll_mode="no_troll")

        prompt = fake_llm.requests[-1]["payload"]["messages"][1]["content"]
        assert "The forum currently has these communities" in prompt
        assert "- books: Books, authors, literature." in prompt
        assert '- "subscriptions"' in prompt

    def test_prompt_omits_community_section_without_subs(self, app, fake_llm):
        fake_llm.enqueue_content(_personas_json(1))
        generate_personas(count=1, auto_create_agent=False, troll_mode="no_troll")

        prompt = fake_llm.requests[-1]["payload"]["messages"][1]["content"]
        assert "communities" not in prompt
        assert '"subscriptions"' not in prompt

    def test_agents_created_with_subscribed_users(self, app, fake_llm, subs):
        fake_llm.enqueue_content(
            json.dumps([self._persona("user_f", ["books", "localllama"])])
        )
        result = generate_personas(
            count=1, auto_create_agent=True, troll_mode="no_troll"
        )
        assert len(result["agents"]) == 1
        stored = result["users"][0]["username"]
        row = User.query.filter_by(username=stored).first()
        assert row.agent_state == {"subscriptions": ["books", "localllama"]}
