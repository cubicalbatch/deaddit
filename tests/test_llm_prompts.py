"""Phase LLM-5: prompt versioning registry — deterministic tests (FakeProvider only).

Covers: render determinism (byte-identical), version immutability, pin
resolution precedence, unknown-variable errors, legacy
GenerationTemplate seeding, render audit trail, live-prompt byte
stability vs the pre-phase golden snapshot, and ChatResult template
echo.
"""

from __future__ import annotations

import datetime
import hashlib

import pytest

from deaddit import db
from deaddit.agents.prompts import (
    build_system_prompt,
    system_prompt_variables,
)
from deaddit.llm.prompts import (
    PromptVersionImmutable,
    UnknownPromptVariable,
    clear_pin,
    create_template,
    create_version,
    get_template,
    get_version,
    render,
    render_pinned,
    resolve_pin,
    seed_from_generation_templates,
    set_pin,
    versioning_enabled,
)
from deaddit.models import (
    Agent,
    AgentMemory,
    GenerationTemplate,
    JobType,
    PromptRenderAudit,
    User,
)

DEFAULT_BODY = (
    "{persona_block}\n\n{tier_line}\n\n{rules_block}"
    "{subscriptions_section}{memories_section}"
)

GOLDEN_PATH = "tests/goldens/llm5_agent_system_prompt.txt"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _golden_agent(app, db_session):
    """The exact fixture the pre-phase golden snapshot was generated from."""
    user = User(
        username="golden_lurker",
        age=34,
        gender="female",
        occupation="librarian",
        interests='["mycology","letterpress","trains"]',
        personality_traits='["curious","dry-witted"]',
        writing_style="precise, mildly sardonic",
    )
    db_session.add(user)
    db_session.flush()
    agent = Agent(
        user_username="golden_lurker",
        autonomy_tier="regular",
        config={},
        state={"subscriptions": ["r/mycology", "r/trains"]},
    )
    db_session.add(agent)
    db_session.flush()
    base = datetime.datetime(2026, 1, 1, 12, 0, 0)
    for minutes, content in [
        (0, "Found a rare ghost pipe patch on the north trail."),
        (1, "Argued about gauge standards; won."),
    ]:
        db_session.add(
            AgentMemory(
                agent_id=agent.id,
                kind="episode",
                content=content,
                created_at=base + datetime.timedelta(minutes=minutes),
            )
        )
    db_session.commit()
    return agent, user


# --- rendering ------------------------------------------------------------


class TestRenderDeterminism:
    def test_same_inputs_same_version_byte_identical(self, app, db_session):
        body = "Hello {name}, welcome to {place}!"
        bindings = {"name": "Ada", "place": "Deaddit"}
        first = render(body, bindings)
        second = render(body, dict(bindings))
        assert first == second
        assert _sha(first) == _sha(second)

    def test_repeated_registry_render_is_byte_identical(self, app, db_session):
        v1 = create_template("t", "A {x} B {y}")
        out1 = render(v1.body, {"x": 1, "y": "z"})
        out2 = render(get_version("t", 1).body, {"x": 1, "y": "z"})
        assert out1 == out2 == "A 1 B z"

    def test_missing_variable_raises_with_names(self, app, db_session):
        with pytest.raises(UnknownPromptVariable) as exc:
            render("Hello {name}, bye {other}", {"name": "Ada"})
        assert "other" in str(exc.value)

    def test_extra_variable_raises_with_names(self, app, db_session):
        with pytest.raises(UnknownPromptVariable) as exc:
            render("Hello {name}", {"name": "Ada", "mood": "sunny"})
        assert "mood" in str(exc.value)

    def test_literal_braces_are_not_variables(self, app, db_session):
        assert render('Return JSON like {"a": 1}', {}) == 'Return JSON like {"a": 1}'


# --- immutability ---------------------------------------------------------


class TestVersionImmutability:
    def test_body_mutation_rejected(self, app, db_session):
        v1 = create_template("imm", "original {a}")
        v1.body = "tampered {a}"
        with pytest.raises(PromptVersionImmutable):
            db.session.commit()
        db.session.rollback()
        assert get_version("imm", 1).body == "original {a}"

    def test_version_number_mutation_rejected(self, app, db_session):
        create_template("imm2", "body")
        row = get_version("imm2", 1)
        row.version = 99
        with pytest.raises(PromptVersionImmutable):
            db.session.commit()
        db.session.rollback()
        assert get_version("imm2", 1).version == 1

    def test_edit_creates_v_n_plus_1_leaving_v_n_queryable(self, app, db_session):
        create_template("edit", "v1 body {a}")
        create_version("edit", "v2 body {a}")
        create_version("edit", "v3 body {a}")
        bodies = [get_version("edit", n).body for n in (1, 2, 3)]
        assert bodies == ["v1 body {a}", "v2 body {a}", "v3 body {a}"]
        # old version still renders byte-identically after later edits
        assert render(bodies[0], {"a": "X"}) == "v1 body X"


# --- pins -----------------------------------------------------------------


class TestPinResolution:
    def _agent(self, db_session, username="pin_user", cohort=None):
        user = User(username=username)
        db_session.add(user)
        db_session.flush()
        config = {"cohort": cohort} if cohort else {}
        agent = Agent(user_username=username, autonomy_tier="regular", config=config)
        db_session.add(agent)
        db_session.commit()
        return agent

    def test_no_pin_resolves_to_none(self, app, db_session):
        self._agent(db_session, "lonely")
        assert resolve_pin("agent", "lonely") is None

    def test_cohort_pin_used_when_agent_unpinned(self, app, db_session):
        create_template("ct", "cohort {v}")
        set_pin("cohort", "parity", "ct", 1)
        self._agent(db_session, "member", cohort="parity")
        pin = resolve_pin("agent", "member")
        assert pin.target_kind == "cohort"
        assert pin.version_number == 1

    def test_agent_pin_beats_cohort_pin(self, app, db_session):
        create_template("pt", "template")
        create_version("pt", "template v2")
        set_pin("cohort", "parity", "pt", 1)
        self._agent(db_session, "special", cohort="parity")
        set_pin("agent", "special", "pt", 2)
        pin = resolve_pin("agent", "special")
        assert pin.target_kind == "agent"
        assert pin.version_number == 2
        text, version_row = render_pinned(
            "agent", "special", {"unused": None} if False else {}
        )
        assert text == "template v2"

    def test_clear_pin_falls_back_to_cohort(self, app, db_session):
        create_template("cpt", "b")
        set_pin("cohort", "wave5", "cpt", 1)
        self._agent(db_session, "clearable", cohort="wave5")
        set_pin("agent", "clearable", "cpt", 1)
        assert clear_pin("agent", "clearable") is True
        assert clear_pin("agent", "clearable") is False
        assert resolve_pin("agent", "clearable").target_kind == "cohort"

    def test_set_pin_rejects_unknown_template(self, app, db_session):
        from deaddit.llm.prompts import PromptError

        with pytest.raises(PromptError):
            set_pin("agent", "x", "no-such-template", 1)


# --- render audit ---------------------------------------------------------


class TestRenderAudit:
    def test_render_pinned_writes_joinable_audit_row(self, app, db_session):
        create_template("aud", "Hello {who}")
        set_pin("agent", "audited", "aud", 1)
        user = User(username="audited")
        db_session.add(user)
        db_session.flush()
        db_session.add(Agent(user_username="audited", autonomy_tier="regular"))
        db_session.commit()

        text, version_row = render_pinned(
            "agent", "audited", {"who": "World"}, subject_key="audited"
        )
        row = PromptRenderAudit.query.one()
        assert row.template_version_id == version_row.id
        assert row.template_id == version_row.template_id
        assert row.subject_kind == "agent"
        assert row.subject_key == "audited"
        assert row.rendered_sha256 == _sha(text)

        # stored variables reproduce the exact bytes (golden-render proof)
        import json

        replay = render(version_row.body, json.loads(row.variables_json))
        assert replay == text
        assert _sha(replay) == row.rendered_sha256


# --- legacy GenerationTemplate seed ----------------------------------------


class TestLegacySeed:
    def test_prompt_string_becomes_verbatim_body(self, app, db_session):
        db_session.add(
            GenerationTemplate(
                name="post_tpl",
                type=JobType.CREATE_POST,
                parameters={"prompt": "Write about {topic} in {style}"},
                description="d",
            )
        )
        db_session.commit()
        seeded = seed_from_generation_templates()
        assert len(seeded) == 1
        assert seeded[0].version == 1
        assert seeded[0].body == "Write about {topic} in {style}"
        assert render(seeded[0].body, {"topic": "cats", "style": "haiku"}) == (
            "Write about cats in haiku"
        )

    def test_non_prompt_parameters_become_canonical_json(self, app, db_session):
        db_session.add(
            GenerationTemplate(
                name="odd_tpl",
                type=JobType.CREATE_USER,
                parameters={"b": 2, "a": 1},
            )
        )
        db_session.commit()
        seeded = seed_from_generation_templates()
        assert seeded[0].body == '{"a":1,"b":2}'

    def test_idempotent(self, app, db_session):
        db_session.add(
            GenerationTemplate(
                name="idem", type=JobType.CREATE_POST, parameters={"prompt": "p {x}"}
            )
        )
        db_session.commit()
        assert len(seed_from_generation_templates()) == 1
        assert seed_from_generation_templates() == []
        assert get_template("idem") is not None


# --- parity freeze: live-prompt byte stability -----------------------------


class TestParityFreezeByteStability:
    def test_flag_defaults_off(self, app, db_session):
        assert versioning_enabled() is False

    def test_flag_off_matches_pre_phase_golden(self, app, db_session):
        agent, user = _golden_agent(app, db_session)
        rendered = build_system_prompt(agent, user)
        golden = open(GOLDEN_PATH).read()
        assert rendered == golden

    def test_flag_on_without_pin_still_matches_golden(self, app, db_session):
        from deaddit.models import Setting

        db_session.add(Setting(key="PROMPT_VERSIONING_ENABLED", value="true"))
        db_session.commit()
        agent, user = _golden_agent(app, db_session)
        assert build_system_prompt(agent, user) == open(GOLDEN_PATH).read()
        assert PromptRenderAudit.query.count() == 0

    def test_default_template_renders_byte_identical_via_registry(
        self, app, db_session
    ):
        agent, user = _golden_agent(app, db_session)
        v1 = create_template(
            "agent.system_prompt", DEFAULT_BODY, description="LLM-5 default"
        )
        set_pin("agent", "golden_lurker", "agent.system_prompt", 1)
        from deaddit.models import Setting

        db_session.add(Setting(key="PROMPT_VERSIONING_ENABLED", value="true"))
        db_session.commit()
        pinned_text = build_system_prompt(agent, user)
        assert pinned_text == open(GOLDEN_PATH).read()
        assert PromptRenderAudit.query.count() == 1
        # registry path and direct renderer agree byte-for-byte
        assert render(v1.body, system_prompt_variables(agent, user)) == pinned_text


# --- ChatRequest/ChatResult echo -------------------------------------------


class TestChatResultEcho:
    def test_complete_echoes_template_version(self, app, fake_llm):
        from deaddit.llm import ChatRequest, LLMClient

        fake_llm.enqueue_content("ok")
        result = LLMClient().complete(
            ChatRequest(
                system_prompt="s",
                user_prompt="u",
                model="fake-model",
                api_url="http://localhost:9/v1",
                template_name="agent.system_prompt",
                template_version=3,
            )
        )
        assert result.content == "ok"
        assert result.template_version == 3

    def test_default_template_version_is_none(self, app, fake_llm):
        from deaddit.llm import ChatRequest, LLMClient

        fake_llm.enqueue_content("ok")
        result = LLMClient().complete(
            ChatRequest(
                system_prompt="s",
                user_prompt="u",
                model="fake-model",
                api_url="http://localhost:9/v1",
            )
        )
        assert result.template_version is None
