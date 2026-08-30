"""Phase 5: admin preview, validation, and rollout of visit profiles.

Locks the JSON contract of the deterministic preview: it must run the exact
runtime preparation path with a seeded RNG, stay side-effect-free, and
report the plan/messages/tools/warnings/diff a reviewer needs before
pinning a new profile version.
"""

from __future__ import annotations

import json

import pytest

from deaddit.agents.loop import run_once
from deaddit.agents.prompts import DEFAULT_VISIT_PROFILE
from deaddit.agents.registry import BACKSTAGE_SUBDEADDIT_NAME
from deaddit.llm.prompts import (
    create_template,
    create_version,
    parse_visit_profile,
    serialize_visit_profile,
    set_pin,
)
from deaddit.models import (
    Agent,
    AgentMemory,
    AgentRun,
    AgentTurn,
    PromptRenderAudit,
    Subdeaddit,
    ToolCall,
    User,
)

PINS = "/admin/api/pins"

PREVIEW = "/admin/api/prompts/agent.visit_profile/preview"
VALIDATE = "/admin/api/prompts/agent.visit_profile/validate"
VERSIONS = "/admin/api/prompts/agent.visit_profile/versions"

_IMAGE_CONFIG = {
    "enabled": True,
    "policy": "optional",
    "provider_id": 1,
    "model": None,
}
_IMAGE_ONLY_CONFIG = dict(_IMAGE_CONFIG, policy="image_only")
_WEBSITE_CONFIG = {"enabled": True, "policy": "optional"}


@pytest.fixture()
def authed_client(client):
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


def _make_agent(
    db_session,
    username,
    *,
    tier="regular",
    image=None,
    website=None,
    cohort=None,
    subscriptions=None,
):
    agent_state = {"subscriptions": subscriptions or []}
    user = User(username=username, agent_state=agent_state)
    db_session.add(user)
    db_session.flush()
    config = {}
    if image is not None:
        config["image_posts"] = image
    if website is not None:
        config["website_posts"] = website
    if cohort is not None:
        config["cohort"] = cohort
    agent = Agent(
        user_username=username,
        autonomy_tier=tier,
        is_enabled=True,
        status="idle",
        config=config,
        state={},
    )
    db_session.add(agent)
    db_session.commit()
    return agent, user


def _preview(client, agent, **overrides):
    payload = {
        "agent_id": agent.id,
        "seed": 42,
        "requested_intent": None,
        "unread_count": 0,
        "version": None,
    }
    payload.update(overrides)
    return client.post(PREVIEW, json=payload)


def _kickoff(body) -> str:
    return body["messages"][1]["content"]


def _default_document() -> dict:
    return json.loads(serialize_visit_profile(DEFAULT_VISIT_PROFILE))


def _finish_call():
    return {
        "id": "call_finish",
        "type": "function",
        "function": {"name": "finish", "arguments": "{}"},
    }


class TestPreviewDeterminismAndPurity:
    def test_same_seed_renders_identical_output(self, app, authed_client, db_session):
        agent, _ = _make_agent(db_session, "deterministic")
        first = _preview(authed_client, agent, requested_intent="post")
        second = _preview(authed_client, agent, requested_intent="post")
        assert first.status_code == second.status_code == 200
        assert first.get_json() == second.get_json()

    def test_seed_drives_the_sampled_choices(self, app, authed_client, db_session):
        agent, _ = _make_agent(db_session, "seeded_sampler")
        outcomes = set()
        for seed in range(8):
            body = _preview(authed_client, agent, seed=seed).get_json()
            outcomes.add(tuple(body["plan"]["direction_ids"]))
        assert len(outcomes) >= 2

    def test_preview_writes_nothing(self, app, authed_client, db_session):
        agent, _ = _make_agent(db_session, "pure_preview")
        counts = {
            model: model.query.count()
            for model in (AgentRun, AgentTurn, ToolCall, PromptRenderAudit, AgentMemory)
        }
        for intent in ("browse", "post", None):
            _preview(authed_client, agent, requested_intent=intent)
        assert {
            model: model.query.count()
            for model in (AgentRun, AgentTurn, ToolCall, PromptRenderAudit, AgentMemory)
        } == counts


class TestPreviewContract:
    def test_shape_plan_messages_tools(self, app, authed_client, db_session):
        agent, _ = _make_agent(db_session, "shape_check")
        body = _preview(authed_client, agent, requested_intent="post").get_json()
        assert body["agent"]["id"] == agent.id
        assert body["requested"] == {
            "intent": "post",
            "unread_count": 0,
            "seed": 42,
            "version": None,
        }
        plan = body["plan"]
        assert plan["intent"] == "post"
        assert plan["intent_source"] == "requested"
        assert plan["content_kind"] == "text_post"
        assert "create_post" in plan["offered_tool_names"]
        assert plan["length_target_id"] and plan["direction_ids"]
        assert [m["role"] for m in body["messages"]] == ["system", "user"]
        assert body["messages"][0]["content"]
        tool_names = {tool["function"]["name"] for tool in body["tools"]}
        assert tool_names == set(plan["offered_tool_names"])
        assert body["warnings"] == []
        assert body["effective"]["resolution_source"] == "default"
        assert body["diff"] == []

    def test_previewed_tools_equal_real_run_tools(
        self, app, authed_client, seeded_db, db_session, fake_llm
    ):
        agent, _ = _make_agent(db_session, "parity_runner", image=_IMAGE_CONFIG)
        for requested in ("post", "image"):
            fake_llm.enqueue(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [_finish_call()],
                            }
                        }
                    ]
                }
            )
            run = run_once(agent.id, requested_intent=requested)
            assert run.status == "completed"
            meta = run.prompt_metadata
            body = _preview(authed_client, agent, requested_intent=requested).get_json()
            assert body["plan"]["offered_tool_names"] == meta["offered_tool_names"]
            assert body["plan"]["intent"] == run.intent
            assert {t["function"]["name"] for t in body["tools"]} == set(
                meta["offered_tool_names"]
            )

    def test_invalid_payloads_are_rejected(self, app, authed_client, db_session):
        agent, _ = _make_agent(db_session, "bad_payloads")
        assert authed_client.post(PREVIEW, json={"seed": 1}).status_code == 400
        assert (
            authed_client.post(PREVIEW, json={"agent_id": agent.id}).status_code == 400
        )
        assert (
            _preview(authed_client, agent, requested_intent="dance").status_code == 400
        )
        assert _preview(authed_client, agent, unread_count=-1).status_code == 400
        assert _preview(authed_client, agent, version=True).status_code == 400
        assert _preview(authed_client, agent, agent_id=99999).status_code == 404
        assert _preview(authed_client, agent, version=99).status_code == 404

    def test_requires_admin(self, app, client, db_session, monkeypatch):
        from deaddit.config import Config

        agent, _ = _make_agent(db_session, "gated_preview")
        monkeypatch.setattr(
            Config, "get", staticmethod(lambda key, default=None: "s3cret")
        )
        assert (
            client.post(PREVIEW, json={"agent_id": agent.id, "seed": 1}).status_code
            == 302
        )


class TestPreviewScenarios:
    def test_browse(self, app, authed_client, db_session):
        agent, _ = _make_agent(db_session, "browse_case")
        body = _preview(authed_client, agent, requested_intent="browse").get_json()
        assert body["plan"]["intent"] == "browse"
        assert body["plan"]["intent_source"] == "requested"
        assert body["plan"]["content_kind"] == "comment"

    def test_post(self, app, authed_client, db_session):
        agent, _ = _make_agent(db_session, "post_case")
        body = _preview(authed_client, agent, requested_intent="post").get_json()
        assert body["plan"]["intent"] == "post"
        assert "create_post" in body["plan"]["offered_tool_names"]
        assert "create a post" in _kickoff(body)

    def test_backstage(self, app, authed_client, db_session):
        db_session.add(
            Subdeaddit(
                name=BACKSTAGE_SUBDEADDIT_NAME,
                description="AI users speak openly with each other.",
            )
        )
        db_session.commit()
        agent, _ = _make_agent(db_session, "backstage_case")
        body = _preview(authed_client, agent, requested_intent="backstage").get_json()
        plan = body["plan"]
        assert plan["intent"] == "backstage"
        assert plan["target_subdeaddit"] == BACKSTAGE_SUBDEADDIT_NAME
        assert set(plan["offered_tool_names"]) & {
            "create_post",
            "create_image_post",
            "create_website",
        } == {"create_post"}
        assert "create_comment" not in plan["offered_tool_names"]
        assert "actual recent experience" in _kickoff(body)

    def test_image(self, app, authed_client, db_session):
        agent, _ = _make_agent(db_session, "image_case", image=_IMAGE_CONFIG)
        body = _preview(authed_client, agent, requested_intent="image").get_json()
        assert body["plan"]["intent"] == "image"
        offered = body["plan"]["offered_tool_names"]
        assert "create_image_post" in offered
        assert "create_post" not in offered and "create_comment" not in offered
        assert body["plan"]["content_kind"] == "media_post"

    def test_website(self, app, authed_client, db_session):
        agent, _ = _make_agent(db_session, "website_case", website=_WEBSITE_CONFIG)
        body = _preview(authed_client, agent, requested_intent="website").get_json()
        assert body["plan"]["intent"] == "website"
        offered = body["plan"]["offered_tool_names"]
        assert "create_website" in offered
        assert "create_post" not in offered and "create_comment" not in offered
        assert "create a website post using the create_website tool" in _kickoff(body)

    def test_unread(self, app, authed_client, db_session):
        agent, _ = _make_agent(db_session, "unread_case")
        body = _preview(authed_client, agent, unread_count=2).get_json()
        assert body["plan"]["intent"] == "browse"
        assert body["plan"]["intent_source"] == "unread_gate"
        assert "You have 2 unread replies" in _kickoff(body)

    def test_lurker(self, app, authed_client, db_session):
        agent, _ = _make_agent(db_session, "lurker_case", tier="lurker")
        body = _preview(authed_client, agent).get_json()
        assert body["plan"]["intent"] == "browse"
        assert body["plan"]["intent_source"] == "lurker"
        offered = body["plan"]["offered_tool_names"]
        assert not set(offered) & {"create_post", "create_image_post", "create_website"}
        assert _kickoff(body).startswith("You're waking up. Browse the community feeds")

    def test_unsubscribed_persona_gets_one_reserved_community(
        self, app, authed_client, seeded_db, db_session
    ):
        agent, _ = _make_agent(db_session, "no_subs", subscriptions=[])
        body = _preview(authed_client, agent, requested_intent="post").get_json()
        kickoff = _kickoff(body)
        assert "Publish exactly one post in d/" in kickoff
        reserved = kickoff.split("Publish exactly one post in d/", 1)[1].split(";", 1)[
            0
        ]
        assert reserved in {"testsub", "askdeaddit"}

    def test_subscribed_persona_names_its_subscriptions(
        self, app, authed_client, seeded_db, db_session
    ):
        agent, _ = _make_agent(db_session, "has_subs", subscriptions=["testsub"])
        body = _preview(authed_client, agent, requested_intent="post").get_json()
        kickoff = _kickoff(body)
        assert "Publish exactly one post in d/testsub;" in kickoff

    def test_exclusive_media(self, app, authed_client, db_session):
        agent, _ = _make_agent(db_session, "exclusive_case", image=_IMAGE_ONLY_CONFIG)
        body = _preview(authed_client, agent, requested_intent="post").get_json()
        offered = body["plan"]["offered_tool_names"]
        assert "create_image_post" in offered
        assert "create_post" not in offered
        assert "create an image post using the create_image_post tool" in _kickoff(body)

    def test_ineligible_intent_degrades_with_warning(
        self, app, authed_client, db_session
    ):
        agent, _ = _make_agent(db_session, "no_image_case")
        body = _preview(authed_client, agent, requested_intent="image").get_json()
        assert body["plan"]["intent"] == "post"
        assert body["plan"]["intent_source"] == "degraded_request"
        assert any("ineligible" in warning for warning in body["warnings"])


class TestPreviewAgainstPinnedVersions:
    def test_version_preview_diffs_against_effective(
        self, app, authed_client, db_session
    ):
        agent, _ = _make_agent(db_session, "diff_case")
        document = _default_document()
        create_template(
            "agent.visit_profile", serialize_visit_profile(DEFAULT_VISIT_PROFILE)
        )
        pinned = set_pin("agent", str(agent.id), "agent.visit_profile", 1)
        assert pinned.version_number == 1
        document["intent_mix"]["post"] = 0.9
        version_row = create_version("agent.visit_profile", json.dumps(document))
        assert version_row.version == 2

        body = _preview(authed_client, agent, version=2).get_json()
        assert body["plan"]["profile_ref"] == "agent.visit_profile:v2"
        assert body["plan"]["resolution_source"] == "preview"
        assert body["effective"]["profile_ref"] == "agent.visit_profile:v1"
        assert body["effective"]["resolution_source"] == "agent"
        entry = next(d for d in body["diff"] if d["path"] == "intent_mix.post")
        assert entry["change"] == "modified"
        assert entry["effective"] == 0.30
        assert entry["preview"] == 0.9

    def test_no_version_previews_the_effective_pin(
        self, app, authed_client, db_session
    ):
        agent, _ = _make_agent(db_session, "effective_case")
        create_template(
            "agent.visit_profile", serialize_visit_profile(DEFAULT_VISIT_PROFILE)
        )
        set_pin("agent", str(agent.id), "agent.visit_profile", 1)
        body = _preview(authed_client, agent).get_json()
        assert body["plan"]["profile_version"] == 1
        assert body["plan"]["resolution_source"] == "agent"
        assert body["effective"]["resolution_source"] == "agent"
        assert body["diff"] == []

    def test_cohort_pin_is_effective_when_agent_pin_absent(
        self, app, authed_client, db_session
    ):
        agent, _ = _make_agent(db_session, "cohort_case", cohort="beta")
        create_template(
            "agent.visit_profile", serialize_visit_profile(DEFAULT_VISIT_PROFILE)
        )
        set_pin("cohort", "beta", "agent.visit_profile", 1)
        body = _preview(authed_client, agent).get_json()
        assert body["plan"]["resolution_source"] == "cohort"
        assert body["effective"]["resolution_source"] == "cohort"

    def test_global_profile_pin_uses_the_resolver_target_key(
        self, app, authed_client, db_session
    ):
        agent, _ = _make_agent(db_session, "global_pin_case")
        create_template(
            "agent.visit_profile", serialize_visit_profile(DEFAULT_VISIT_PROFILE)
        )
        response = authed_client.post(
            PINS,
            json={
                "target_kind": "global",
                # Older UI code sent this display value. The API must map it
                # to the sole runtime global profile target.
                "target_key": "global",
                "template": "agent.visit_profile",
                "version": 1,
            },
        )
        assert response.status_code == 200
        assert response.get_json()[0]["target_key"] == "agent.visit_profile"

        body = _preview(authed_client, agent).get_json()
        assert body["plan"]["resolution_source"] == "global"


class TestValidationApi:
    def test_valid_body_round_trips(self, app, authed_client):
        canonical = serialize_visit_profile(DEFAULT_VISIT_PROFILE)
        resp = authed_client.post(VALIDATE, json={"body": canonical})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["valid"] is True
        parse_visit_profile(body["normalized_body"])

    def test_invalid_body_reports_validator_error(self, app, authed_client):
        document = _default_document()
        document["intent_mix"]["post"] = 1.5
        resp = authed_client.post(VALIDATE, json={"body": json.dumps(document)})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["valid"] is False
        assert "intent_mix.post" in body["error"]

    def test_length_weights_must_total_100(self, app, authed_client):
        document = _default_document()
        document["length_catalog"]["comment"][0]["weight"] = 10
        resp = authed_client.post(VALIDATE, json={"body": json.dumps(document)})
        body = resp.get_json()
        assert body["valid"] is False
        assert "length_catalog.comment weights must total 100" in body["error"]

    def test_requires_body(self, app, authed_client):
        assert authed_client.post(VALIDATE, json={}).status_code == 400

    def test_unknown_template_404_and_other_templates_rejected(
        self, app, authed_client
    ):
        create_template("alpha", "A {x}")
        assert (
            authed_client.post(
                VALIDATE.replace("agent.visit_profile", "nope"), json={"body": "x"}
            ).status_code
            == 404
        )
        assert (
            authed_client.post(
                VALIDATE.replace("agent.visit_profile", "alpha"),
                json={"body": "A {x}"},
            ).status_code
            == 400
        )


class TestVersionCreationValidation:
    def test_valid_profile_body_creates_immutable_version(self, app, authed_client):
        row = create_template(
            "agent.visit_profile", serialize_visit_profile(DEFAULT_VISIT_PROFILE)
        )
        assert row.version == 1
        document = _default_document()
        document["intent_mix"] = {
            "post": 1.0,
            "image": 0.0,
            "website": 0.0,
            "backstage": 0.0,
        }
        resp = authed_client.post(
            VERSIONS, json={"body": json.dumps(document), "created_by": "tester"}
        )
        assert resp.status_code == 201
        assert resp.get_json()["version"] == 2


class TestPromptsPage:
    def test_page_renders(self, app, authed_client):
        resp = authed_client.get("/admin/prompts")
        assert resp.status_code == 200
        assert resp.data


class TestPreviewRNGIsolation:
    def test_preview_does_not_advance_the_global_generator(
        self, app, authed_client, db_session
    ):
        import random as _random

        agent, _ = _make_agent(db_session, "isolated_rng")
        _random.seed(1234)
        expected = _random.random()
        _random.seed(1234)
        _preview(authed_client, agent, requested_intent="post")
        # The preview RNG is a private instance; the global stream is
        # untouched, so the next global draw is still the first one.
        assert _random.random() == expected
