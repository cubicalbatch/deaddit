"""Phase 4: versioned visit profiles and run auditability.

Locks the cutover invariants:
- invalid profile documents cannot be created or pinned;
- pin precedence is agent > cohort > global > source default, through the
  real ``prepare_agent_visit`` resolution path;
- the automatic intent mix comes only from the profile, never Config;
- ``AgentRun.prompt_metadata`` records the effective immutable profile and
  the sampled plan, and reproduces the prepared initial messages while the
  first ``AgentTurn`` keeps the exact bytes.
"""

from __future__ import annotations

import json
import re

import pytest

from deaddit import Config
from deaddit.extensions import db
from deaddit.agents.loop import run_once
from deaddit.agents.prompts import (
    DEFAULT_PROFILE_NAME,
    DEFAULT_PROFILE_VERSION,
    DEFAULT_VISIT_PROFILE,
    INTENT_SOURCE_SAMPLED,
    prepare_agent_visit,
)
from deaddit.llm.prompts import (
    PromptError,
    create_template,
    create_version,
    get_template,
    parse_visit_profile,
    render,
    serialize_visit_profile,
    set_pin,
)
from deaddit.models import Agent, AgentTurn, Setting, User

from tests.visit_profiles import (
    PROFILE_TEMPLATE,
    pin_cohort_intent_mix,
    pin_global_intent_mix,
    pin_intent_mix,
)

_RETIRED_MIX_KEYS = (
    "AGENT_POST_INTENT_CHANCE",
    "AGENT_FORCED_IMAGE_CHANCE",
    "AGENT_FORCED_WEBSITE_CHANCE",
)


def _make_agent(db_session, username, *, cohort=None):
    user = User(username=username, agent_state={"subscriptions": []})
    db_session.add(user)
    db_session.flush()
    config = {"cohort": cohort} if cohort else {}
    agent = Agent(
        user_username=username,
        autonomy_tier="regular",
        is_enabled=True,
        status="idle",
        config=config,
        state={},
    )
    db_session.add(agent)
    db_session.commit()
    return agent, user


def _finish_call():
    return {
        "id": "c1",
        "type": "function",
        "function": {"name": "finish", "arguments": json.dumps({"summary": "done"})},
    }


def _replay(body: str, variables: dict) -> str:
    """Re-render one stored layout with exactly the variables it names."""
    names = set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", body))
    return render(body, {name: variables[name] for name in names})


def _invalid_document():
    document = json.loads(serialize_visit_profile(DEFAULT_VISIT_PROFILE))
    document["intent_mix"]["image"] = 0.9
    document["intent_mix"]["website"] = 0.9
    return document


# --- invalid documents cannot be created or pinned -------------------------


class TestInvalidProfilesRejected:
    def test_parse_rejects_bad_json_and_schema(self):
        with pytest.raises(PromptError):
            parse_visit_profile("{not json")
        with pytest.raises(PromptError):
            parse_visit_profile({"schema_version": 1})
        with pytest.raises(PromptError):
            parse_visit_profile(_invalid_document())

    def test_parse_rejects_short_direction_catalog(self):
        document = json.loads(serialize_visit_profile(DEFAULT_VISIT_PROFILE))
        document["direction_catalog"]["post"] = document["direction_catalog"]["post"][
            :1
        ]
        with pytest.raises(PromptError):
            parse_visit_profile(document)

    def test_profile_template_creation_validates(self, app, db_session):
        with pytest.raises(PromptError):
            create_template(PROFILE_TEMPLATE, json.dumps(_invalid_document()))
        assert get_template(PROFILE_TEMPLATE) is None

    def test_profile_version_creation_validates(self, app, db_session):
        create_template(PROFILE_TEMPLATE, serialize_visit_profile(DEFAULT_VISIT_PROFILE))
        with pytest.raises(PromptError):
            create_version(PROFILE_TEMPLATE, json.dumps(_invalid_document()))
        assert get_template(PROFILE_TEMPLATE).versions.count() == 1

    def test_pin_rejects_unknown_profile_version(self, app, db_session):
        agent, _ = _make_agent(db_session, "bad_pin")
        create_template(PROFILE_TEMPLATE, serialize_visit_profile(DEFAULT_VISIT_PROFILE))
        with pytest.raises(PromptError):
            set_pin("agent", str(agent.id), PROFILE_TEMPLATE, 99)

    def test_profile_resolution_rejects_non_profile_pin(self, app, db_session):
        agent, _ = _make_agent(db_session, "wrong_template")
        create_template("agent.system_prompt", "plain {x}")
        set_pin("agent", str(agent.id), "agent.system_prompt", 1)
        with pytest.raises(PromptError):
            prepare_agent_visit(agent, db.session.get(User, agent.user_username))


# --- precedence: agent > cohort > global > default --------------------------


class TestProfilePrecedence:
    def test_default_source_identified_without_pins(self, app, db_session):
        agent, user = _make_agent(db_session, "plain")
        visit = prepare_agent_visit(agent, user, requested_intent="browse", unread=0)
        assert visit.plan.profile_name == DEFAULT_PROFILE_NAME
        assert visit.plan.profile_version == DEFAULT_PROFILE_VERSION
        assert visit.plan.resolution_source == "default"

    def test_agent_pin_beats_cohort_and_global(self, app, db_session):
        agent, user = _make_agent(db_session, "special", cohort="parity")
        pin_global_intent_mix(post=0.0, image=0.0, website=0.0)
        pin_cohort_intent_mix("parity", post=0.0, image=0.0, website=0.0)
        pin_intent_mix(agent, post=1.0, image=0.0, website=0.0)
        visit = prepare_agent_visit(agent, user, unread=0)
        assert visit.plan.resolution_source == "agent"
        assert visit.plan.intent == "post"
        assert visit.plan.intent_source == INTENT_SOURCE_SAMPLED

    def test_cohort_pin_beats_global(self, app, db_session):
        agent, user = _make_agent(db_session, "member", cohort="parity")
        pin_global_intent_mix(post=0.0, image=0.0, website=0.0)
        pin_cohort_intent_mix("parity", post=1.0, image=0.0, website=0.0)
        visit = prepare_agent_visit(agent, user, unread=0)
        assert visit.plan.resolution_source == "cohort"
        assert visit.plan.intent == "post"

    def test_global_pin_beats_default(self, app, db_session):
        agent, user = _make_agent(db_session, "glob")
        pin_global_intent_mix(post=1.0, image=0.0, website=0.0)
        visit = prepare_agent_visit(agent, user, unread=0)
        assert visit.plan.resolution_source == "global"
        assert visit.plan.intent == "post"

    def test_intent_mix_comes_only_from_profile(self, app, db_session):
        """Retired Config rows must not resurrect a second live mix source."""
        agent, user = _make_agent(db_session, "no_config")
        for key in _RETIRED_MIX_KEYS:
            Config.set(key, "1.0")
        try:
            visit = prepare_agent_visit(agent, user, unread=0)
            # The default profile mix (0.30 post) is the only live source;
            # the stray settings are inert.
            assert visit.plan.resolution_source == "default"
            assert visit.plan.profile_version == DEFAULT_PROFILE_VERSION
        finally:
            Setting.query.filter(Setting.key.in_(_RETIRED_MIX_KEYS)).delete(
                synchronize_session=False
            )
            db_session.commit()


# --- auditability: metadata reproduces prepared messages --------------------


class TestRunPromptMetadata:
    def test_metadata_reproduces_initial_messages(self, seeded_db, db_session, fake_llm):
        agent, _ = _make_agent(db_session, "audited_runner")
        fake_llm.enqueue(
            {
                "choices": [
                    {"message": {"role": "assistant", "tool_calls": [_finish_call()]}}
                ]
            }
        )

        run = run_once(agent.id)

        assert run.status == "completed"
        meta = run.prompt_metadata
        assert meta["schema_version"] == 1
        assert meta["profile"]["name"] == DEFAULT_PROFILE_NAME
        assert meta["profile"]["version"] == DEFAULT_PROFILE_VERSION
        assert meta["profile"]["resolution_source"] == "default"
        assert meta["intent"] == run.intent
        assert meta["offered_tool_names"] == sorted(meta["offered_tool_names"])
        assert isinstance(meta["length_target_id"], str) or meta[
            "length_target_id"
        ] is None
        assert isinstance(meta["direction_ids"], list)

        # The immutable profile body and the stored variables reproduce the
        # exact initial system-message bytes.
        profile = parse_visit_profile(meta["profile"]["body"])
        replayed = _replay(profile.layouts["system"], meta["render_variables"]["system"])
        assert replayed == meta["initial_messages"][0]["content"]

        # The first AgentTurn kept the exact request bytes.
        first_turn = AgentTurn.query.filter_by(run_id=run.id, seq=1).one()
        assert first_turn.request_messages == meta["initial_messages"]

    def test_pinned_profile_is_identified_in_metadata(
        self, seeded_db, db_session, fake_llm
    ):
        agent, _ = _make_agent(db_session, "pinned_runner")
        pin_intent_mix(agent, post=0.0, image=0.0, website=0.0)
        fake_llm.enqueue(
            {
                "choices": [
                    {"message": {"role": "assistant", "tool_calls": [_finish_call()]}}
                ]
            }
        )

        run = run_once(agent.id)
        meta = run.prompt_metadata
        assert meta["profile"]["resolution_source"] == "agent"
        assert meta["profile"]["version"] >= 1
        assert meta["profile"]["name"] == PROFILE_TEMPLATE
        # The pinned body round-trips through the strict parser.
        parse_visit_profile(meta["profile"]["body"])
