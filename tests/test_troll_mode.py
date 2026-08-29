"""Coverage for troll persona generation, prompts, admin API, and migration."""

from __future__ import annotations

import json
import sqlite3

import pytest

import deaddit
import deaddit.services.persona_generator as generator
from deaddit.agents.prompts import _persona_block, system_prompt_variables
from deaddit.config import Config
from deaddit.models import Agent, User
from deaddit.services.content import create_user
from deaddit.services.persona_generator import (
    TROLL_SECTION,
    USER_PROMPT_TEMPLATE,
    _assign_styles,
    _batch_plan,
    _user_to_dict,
    generate_personas,
)


def _one_response(username: str = "generated") -> str:
    return json.dumps(
        [{"username": username, "bio": "A bio", "age": 30, "gender": "Male"}]
    )


def _n_response(prefix: str, n: int) -> str:
    return json.dumps(
        [
            {"username": f"{prefix}_{i}", "bio": "A bio", "age": 30, "gender": "Male"}
            for i in range(n)
        ]
    )


@pytest.fixture()
def admin_client(client):
    with client.session_transaction() as session:
        session["admin_authenticated"] = True
    return client


class TestPersonaGeneratorTrollMode:
    def test_chance_extremes_and_legacy_prompt_parity(self, app, fake_llm, monkeypatch):
        Config.set("TROLL_USER_CHANCE", "1.0")
        fake_llm.enqueue_content(_one_response("all_troll"))
        result = generate_personas(count=1, auto_create_agent=False)
        assert result["users"][0]["is_troll"] is True
        assert (
            TROLL_SECTION in fake_llm.requests[0]["payload"]["messages"][1]["content"]
        )
        Config.set("TROLL_USER_CHANCE", "0.0")
        # Freeze the style-card draw so the parity check is deterministic
        monkeypatch.setattr(generator.random, "choices", lambda seq, k: [seq[0]] * k)
        fake_llm.enqueue_content(_one_response("all_normal"))
        generate_personas(count=1, auto_create_agent=False)
        prompt = fake_llm.requests[1]["payload"]["messages"][1]["content"]
        assert prompt == USER_PROMPT_TEMPLATE.format(
            count=1,
            topic_section="",
            communities_section="",
            troll_section="",
            style_assignments="Persona 1 username style: " + _assign_styles(1)[0],
        )
        assert TROLL_SECTION not in prompt

    @pytest.mark.parametrize(
        ("mode", "expected"), [("troll", True), ("no_troll", False)]
    )
    def test_explicit_mode_overrides_chance(self, app, fake_llm, mode, expected):
        Config.set("TROLL_USER_CHANCE", "0.0" if expected else "1.0")
        fake_llm.enqueue_content(_one_response(f"forced_{mode}"))
        result = generate_personas(count=1, auto_create_agent=False, troll_mode=mode)
        assert result["users"][0]["is_troll"] is expected

    def test_invalid_mode_and_user_serialization(self, app, fake_llm):
        with pytest.raises(ValueError, match="Invalid troll_mode"):
            generate_personas(count=1, troll_mode="bogus")
        user = User(username="serialized", is_troll=True)
        assert _user_to_dict(user)["is_troll"] is True

    def test_chance_quota_is_exact_and_spread(self, app, fake_llm):
        Config.set("TROLL_USER_CHANCE", "0.1")
        # 25 personas at 10% -> exactly 3 trolls (round-half-up), with the
        # troll batch scheduled after the three normal batches.
        fake_llm.enqueue_content(_n_response("normal", 10))
        fake_llm.enqueue_content(_n_response("normal", 10))
        fake_llm.enqueue_content(_n_response("normal", 2))
        fake_llm.enqueue_content(_n_response("troll", 3))
        result = generate_personas(count=25, auto_create_agent=False)
        assert result["skipped"] == 0
        assert [u["is_troll"] for u in result["users"]] == [False] * 22 + [True] * 3
        prompts = [r["payload"]["messages"][1]["content"] for r in fake_llm.requests]
        assert [TROLL_SECTION in p for p in prompts] == [False, False, False, True]

    def test_chance_mixed_batch_uses_homogeneous_requests(self, app, fake_llm):
        Config.set("TROLL_USER_CHANCE", "0.5")
        # 2 personas at 50% -> exactly 1 troll; the normal batch is scheduled
        # first, so troll and normal personas never share one request.
        fake_llm.enqueue_content(_one_response("mixed_normal"))
        fake_llm.enqueue_content(_one_response("mixed_troll"))
        result = generate_personas(count=2, auto_create_agent=False)
        assert len(fake_llm.requests) == 2
        prompts = [r["payload"]["messages"][1]["content"] for r in fake_llm.requests]
        assert TROLL_SECTION not in prompts[0]
        assert TROLL_SECTION in prompts[1]
        assert [u["is_troll"] for u in result["users"]] == [False, True]


class TestBatchPlan:
    def test_troll_batches_spread_evenly(self):
        plan = _batch_plan(20, 180)
        assert [is_troll for is_troll, _ in plan] == (
            [False] * 9 + [True] + [False] * 9 + [True]
        )
        assert [size for _, size in plan] == [10] * 20

    def test_single_type_and_edge_shapes(self):
        assert _batch_plan(0, 5) == [(False, 5)]
        assert _batch_plan(5, 0) == [(True, 5)]
        assert _batch_plan(3, 22) == [(False, 10), (False, 10), (False, 2), (True, 3)]
        assert _batch_plan(25, 0) == [(True, 10), (True, 10), (True, 5)]


class TestTrollPromptAndAdmin:
    def test_persona_block_and_prompt_variable_keys(self, app):
        troll = create_user(
            username="prompt_troll",
            age=25,
            gender="Male",
            bio="bio",
            interests=[],
            occupation="Worker",
            education="School",
            writing_style="Direct",
            personality_traits=[],
            is_troll=True,
        )
        normal = create_user(
            username="prompt_normal",
            age=25,
            gender="Male",
            bio="bio",
            interests=[],
            occupation="Worker",
            education="School",
            writing_style="Direct",
            personality_traits=[],
            is_troll=False,
        )
        agent = Agent(
            user_username=troll.username, autonomy_tier="regular", config={}, state={}
        )
        assert "Mode: troll." in _persona_block(troll)
        assert "Mode: troll." not in _persona_block(normal)
        assert set(system_prompt_variables(agent, troll)) == {
            "persona",
            "persona_block",
            "autonomy_tier",
            "tier_line",
            "rules_block",
            "tools",
            "tools_line",
            "genuine",
            "genuine_line",
            "quality_rules",
            "profile_quality_rules",
            "capability_guidance",
            "memories",
            "memory_block",
            "memories_section",
            "subscriptions",
            "subscriptions_section",
            "community_hint",
            "intent",
            "content_kind",
            "length_target",
            "directions",
            "sample_count",
            "image_guidance_section",
            "website_guidance_section",
        }

    def test_admin_generation_toggle_and_config(self, admin_client, fake_llm, app):
        fake_llm.enqueue_content(_one_response("admin_troll"))
        response = admin_client.post(
            "/admin/api/users/generate",
            json={"count": 1, "auto_create_agent": False, "troll_mode": "troll"},
        )
        assert response.status_code == 201
        assert response.get_json()["users"][0]["is_troll"] is True
        invalid = admin_client.post(
            "/admin/api/users/generate", json={"count": 1, "troll_mode": "invalid"}
        )
        assert invalid.status_code == 400
        assert invalid.get_json() == {
            "success": False,
            "error": "Unknown troll_mode 'invalid'",
        }

        user = create_user(
            username="toggle_user",
            age=25,
            gender="Male",
            bio="bio",
            interests=[],
            occupation="Worker",
            education="School",
            writing_style="Direct",
            personality_traits=[],
            is_troll=False,
        )
        assert (
            admin_client.get(f"/admin/api/users/{user.username}").get_json()["is_troll"]
            is False
        )
        updated = admin_client.put(
            f"/admin/api/users/{user.username}", json={"is_troll": True}
        )
        assert updated.status_code == 200
        assert User.query.get(user.username).is_troll is True

        saved = admin_client.post(
            "/admin/api/save-config", json={"troll_user_chance": "0.25"}
        )
        assert saved.status_code == 200
        assert Config.get("TROLL_USER_CHANCE") == "0.25"
        for value in ("abc", "1.5"):
            assert (
                admin_client.post(
                    "/admin/api/save-config", json={"troll_user_chance": value}
                ).status_code
                == 400
            )


def _migration_runner(tmp_path):
    db_path = tmp_path / "troll-migration.db"
    app = deaddit.create_app(
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "TESTING": True}
    )
    return db_path, app, app.test_cli_runner()


def test_troll_migration_upgrade_and_downgrade(tmp_path):
    db_path, _app, runner = _migration_runner(tmp_path)
    assert runner.invoke(args=["db", "upgrade", "c5e7a9b1d3f6"]).exit_code == 0
    before = sqlite3.connect(db_path)
    try:
        assert "is_troll" not in {
            row[1] for row in before.execute("PRAGMA table_info(user)")
        }
    finally:
        before.close()
    upgraded = runner.invoke(args=["db", "upgrade", "c4b9e2f7a1d3"])
    assert upgraded.exit_code == 0, upgraded.output
    conn = sqlite3.connect(db_path)
    try:
        column = next(
            row
            for row in conn.execute("PRAGMA table_info(user)")
            if row[1] == "is_troll"
        )
        assert column[3] == 1
        assert str(column[4]) in {"0", "'0'"}
    finally:
        conn.close()
    downgraded = runner.invoke(args=["db", "downgrade", "c5e7a9b1d3f6"])
    assert downgraded.exit_code == 0, downgraded.output
    conn = sqlite3.connect(db_path)
    try:
        assert "is_troll" not in {
            row[1] for row in conn.execute("PRAGMA table_info(user)")
        }
    finally:
        conn.close()
