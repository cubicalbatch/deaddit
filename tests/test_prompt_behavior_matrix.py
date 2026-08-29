"""Behavior matrix for prepared agent visit prompts.

These tests characterize the prompt-profile, capability, and tool-spec
contracts that must compose through ``prepare_agent_visit``.
"""

from __future__ import annotations

import random
from unittest.mock import patch

import pytest

from deaddit import Config
from deaddit.agents.prompts import (
    build_system_prompt,
    prepare_agent_visit,
    system_prompt_variables,
)
from deaddit.agents.registry import specs_for, tools_for
from deaddit.models import Agent, AgentMemory, User


READ_ONLY_TOOLS = {
    "browse_feed",
    "read_post",
    "search",
    "view_inbox",
    "view_profile",
    "finish",
}
ALL_TOOLS = {
    *READ_ONLY_TOOLS,
    "create_post",
    "create_image_post",
    "create_website",
    "create_comment",
    "subscribe",
    "unsubscribe",
}
POST_TOOLS = {"create_post", "create_image_post", "create_website"}

# This is the current registry truth table written as data so failures name the
# exact capability cell rather than hiding the contract in test control flow.
CAPABILITY_MATRIX = {
    ("disabled", "disabled"): {"create_post"},
    ("optional", "disabled"): {"create_post", "create_image_post"},
    ("image_only", "disabled"): {"create_image_post"},
    ("disabled", "optional"): {"create_post", "create_website"},
    ("optional", "optional"): POST_TOOLS,
    ("image_only", "optional"): {"create_image_post"},
    ("disabled", "website_only"): {"create_website"},
    ("optional", "website_only"): {"create_website"},
    ("image_only", "website_only"): set(),
}
CAPABILITIES = tuple(CAPABILITY_MATRIX)
TIERS = ("lurker", "regular", "power_user")
INTENTS = ("browse", "post", "image", "website")

_IMAGE_CONFIG = {
    "optional": {
        "enabled": True,
        "policy": "optional",
        "provider_id": 1,
        "model": None,
    },
    "image_only": {
        "enabled": True,
        "policy": "image_only",
        "provider_id": 1,
        "model": None,
    },
}
_WEBSITE_CONFIG = {
    "optional": {"enabled": True, "policy": "optional"},
    "website_only": {"enabled": True, "policy": "website_only"},
}


def _make_agent(
    db_session,
    username: str,
    *,
    tier: str = "regular",
    image_mode: str = "disabled",
    website_mode: str = "disabled",
    subscriptions: list[str] | None = None,
):
    user = User(
        username=username,
        agent_state={"subscriptions": subscriptions or []},
    )
    db_session.add(user)
    db_session.flush()
    config = {}
    if image_mode != "disabled":
        config["image_posts"] = _IMAGE_CONFIG[image_mode]
    if website_mode != "disabled":
        config["website_posts"] = _WEBSITE_CONFIG[website_mode]
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


def _static_post_tools(image_mode: str, website_mode: str) -> set[str]:
    return CAPABILITY_MATRIX[(image_mode, website_mode)]


def _special_tool(intent: str) -> str:
    return "create_image_post" if intent == "image" else "create_website"


def _expected_tools(tier: str, capability_tools: set[str], intent: str) -> set[str]:
    base = READ_ONLY_TOOLS if tier == "lurker" else ALL_TOOLS - POST_TOOLS
    if intent in {"image", "website"}:
        # Lurkers have no post tools regardless of configured namespaces.
        if tier == "lurker":
            return base
        # The effective special-intent lock is applied only when the static
        # namespace offered that tool.  An ineligible direct registry lookup
        # fails closed; memory.py degrades an explicit request before calling
        # the registry in normal runs.
        special = {_special_tool(intent)} & capability_tools
        return base - {"create_comment"} | special
    return base if tier == "lurker" else base | capability_tools


def _expected_image_guidance(
    capability_tools: set[str], image_mode: str, website_mode: str, intent: str
) -> str:
    if intent == "image":
        offered = {"create_image_post"} & capability_tools
        return "image_only" if offered else ""
    if intent == "website":
        return ""
    if "create_image_post" not in capability_tools:
        return ""
    if capability_tools == {"create_image_post"}:
        return "image_only"
    return "optional"


def _expected_website_guidance(
    capability_tools: set[str], image_mode: str, website_mode: str, intent: str
) -> str:
    if intent == "website":
        offered = {"create_website"} & capability_tools
        return "website_only" if offered else ""
    if intent == "image":
        return ""
    if "create_website" not in capability_tools:
        return ""
    if capability_tools == {"create_website"}:
        return "website_only"
    return "optional"


@pytest.mark.parametrize("tier", TIERS)
def test_tier_capability_intent_matrix_matches_current_prompt_and_wire_contract(
    app, db_session, tier
):
    """Every tier, capability cell, and intent has one current outcome."""
    for index, (image_mode, website_mode) in enumerate(CAPABILITIES):
        agent, user = _make_agent(
            db_session,
            f"matrix_{tier}_{image_mode}_{website_mode}_{index}",
            tier=tier,
            image_mode=image_mode,
            website_mode=website_mode,
        )
        capability_tools = _static_post_tools(image_mode, website_mode)

        # Tier prose is unconditional in the current system prompt, while
        # capability guidance follows the static post namespaces.
        prompt = build_system_prompt(agent, user)
        tier_words = {
            "lurker": "You are a lurker",
            "regular": "You are a regular member",
            "power_user": "You are a power user",
        }
        assert tier_words[tier] in prompt
        variables = system_prompt_variables(agent, user)
        assert "You interact with the site through tools" in variables["rules_block"]
        assert "Be genuine." in variables["rules_block"]
        assert "Posting rules:" in variables["rules_block"]

        image_guidance = variables["image_guidance_section"]
        expected_image = _expected_image_guidance(
            capability_tools, image_mode, website_mode, "browse"
        )
        if expected_image == "":
            assert image_guidance == ""
        elif expected_image == "image_only":
            assert "every post you make uses create_image_post" in image_guidance
        else:
            assert "occasional alternative to create_post" in image_guidance

        website_guidance = variables["website_guidance_section"]
        expected_website = _expected_website_guidance(
            capability_tools, image_mode, website_mode, "browse"
        )
        if expected_website == "":
            assert website_guidance == ""
        elif expected_website == "website_only":
            assert "must use create_website" in website_guidance
        else:
            assert "occasional alternative to create_post" in website_guidance

        for intent in INTENTS:
            expected = _expected_tools(tier, capability_tools, intent)
            assert {tool.name for tool in tools_for(tier, agent=agent, intent=intent)} == (
                expected
            )
            assert {spec.name for spec in specs_for(tier, agent=agent, intent=intent)} == (
                expected
            )

            intent_variables = system_prompt_variables(agent, user, intent=intent)
            expected_image = _expected_image_guidance(
                capability_tools, image_mode, website_mode, intent
            )
            expected_website = _expected_website_guidance(
                capability_tools, image_mode, website_mode, intent
            )
            assert bool(intent_variables["image_guidance_section"]) == bool(
                expected_image
            )
            assert bool(intent_variables["website_guidance_section"]) == bool(
                expected_website
            )

            intent_prompt = build_system_prompt(agent, user, intent=intent)
            if expected_image == "":
                assert "create_image_post" not in intent_prompt
            else:
                assert "create_image_post" in intent_variables["image_guidance_section"]
            if expected_website == "":
                assert "create_website" not in intent_prompt
            else:
                assert "create_website" in intent_variables["website_guidance_section"]


def test_subscriptions_and_prompt_prose_sections_are_stable(app, db_session):
    """Persistent subscription guidance and the five prose categories coexist."""
    agent, user = _make_agent(
        db_session,
        "subscription_matrix",
        subscriptions=["r/mycology", "r/trains"],
        image_mode="optional",
        website_mode="optional",
    )
    variables = system_prompt_variables(agent, user)
    prompt = build_system_prompt(agent, user)

    # Global behavior: tool use, authenticity, and quality rules.
    assert "You interact with the site through tools" in variables["rules_block"]
    assert "Be genuine." in variables["rules_block"]
    assert "Posting rules:" in variables["rules_block"]
    # Capability guidance: these are sections, not unconditional base prose.
    assert variables["image_guidance_section"]
    assert variables["website_guidance_section"]
    # Subscription state is persistent system context and keeps order.
    assert variables["subscriptions_section"] == (
        "\n\nYou are currently subscribed to: r/mycology, r/trains"
    )
    no_sub_agent, no_sub_user = _make_agent(
        db_session, "subscription_empty_matrix", subscriptions=[]
    )
    assert system_prompt_variables(no_sub_agent, no_sub_user)[
        "subscriptions_section"
    ] == ""
    assert "r/mycology, r/trains" in prompt

    # Content tuning and the transient kickoff objective remain in the user
    # message.  Operational tool wording appears in both the system contract
    # and the forced-post kickoff when a post is explicitly requested.
    with patch("deaddit.agents.prompts.random.choices", return_value=[0]):
        visit = prepare_agent_visit(
            agent, user, requested_intent="post", unread=0
        )
    kickoff = visit.messages[1]["content"]
    assert visit.plan.intent == "post"
    assert "For inspiration, choose at most one" in kickoff
    assert "Length target for this text post body:" in kickoff
    assert "You're waking up with something to share." in kickoff
    assert "using the create_post tool" in kickoff


def test_kickoff_requested_intent_and_unread_matrix(app, db_session):
    """Explicit intent, unread gating, and capability degradation are frozen."""
    cases = (
        ("disabled", "disabled"),
        ("optional", "disabled"),
        ("image_only", "disabled"),
        ("disabled", "optional"),
        ("optional", "optional"),
        ("image_only", "optional"),
        ("disabled", "website_only"),
        ("optional", "website_only"),
        ("image_only", "website_only"),
    )
    requests = ("browse", "post", "image", "website")
    for index, (image_mode, website_mode) in enumerate(cases):
        agent, user = _make_agent(
            db_session,
            f"kickoff_{image_mode}_{website_mode}_{index}",
            image_mode=image_mode,
            website_mode=website_mode,
        )
        capability_tools = _static_post_tools(image_mode, website_mode)
        for requested in requests:
            for unread in (0, 2):
                with patch(
                    "deaddit.agents.prompts.random.choices", return_value=[50]
                ), patch(
                    "deaddit.agents.prompts.random.sample",
                    side_effect=lambda population, count: list(population)[:count],
                ):
                    visit = prepare_agent_visit(
                        agent,
                        user,
                        unread=unread,
                        requested_intent=requested,
                    )
                kickoff = visit.messages[1]["content"]
                resolved = visit.plan.intent

                if unread > 0:
                    expected = (
                        requested
                        if requested in {"image", "website"}
                        and _special_tool(requested) in capability_tools
                        else "browse"
                    )
                elif requested in {"image", "website"}:
                    expected = (
                        requested
                        if _special_tool(requested) in capability_tools
                        else "post" if capability_tools else "browse"
                    )
                elif requested == "post":
                    expected = "post" if capability_tools else "browse"
                else:
                    expected = requested
                assert resolved == expected, (
                    image_mode,
                    website_mode,
                    requested,
                    unread,
                    kickoff,
                )

                if unread > 0:
                    assert "Catch up on your replies" in kickoff
                elif resolved == "browse":
                    assert "Browse your feed" in kickoff
                    assert "Length target for this comment:" in kickoff
                else:
                    assert "something to share" in kickoff
                    if capability_tools:
                        assert "Once your post is published" in kickoff
                        assert any(name in kickoff for name in capability_tools)
                    else:
                        # The invalid two-exclusive-lock cell resolves the
                        # request to post but has no legal publication tool.
                        assert "Once your post is published" not in kickoff


def test_initial_messages_freeze_unread_notice_and_system_kickoff_roles(
    app, db_session, monkeypatch
):
    agent, user = _make_agent(db_session, "initial_messages")
    monkeypatch.setattr("deaddit.agents.prompts.unread_count", lambda username: 2)
    monkeypatch.setattr("deaddit.agents.prompts.visit_memories", lambda username: None)
    with patch("deaddit.agents.prompts.random.choices", return_value=[0]), patch(
        "deaddit.agents.prompts.random.sample", side_effect=lambda population, count: list(population)[:count]
    ):
        visit = prepare_agent_visit(agent, user, requested_intent="browse")
    messages = visit.messages
    assert visit.plan.intent == "browse"
    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[0]["content"].startswith("You are initial_messages")
    assert "You have 2 unread replies. Use the view_inbox tool" in messages[1]["content"]



def test_prepared_messages_render_persistent_memory_once_in_system(
    app, db_session
):
    agent, user = _make_agent(db_session, "memory_once")
    db_session.add_all(
        [
            AgentMemory(
                user_username=user.username,
                kind="backfill",
                content="History before becoming an agent.",
            ),
            AgentMemory(
                user_username=user.username,
                kind="episode",
                content="Created a memorable post yesterday.",
            ),
        ]
    )
    db_session.commit()

    visit = prepare_agent_visit(
        agent, user, requested_intent="browse", unread=0
    )
    system, kickoff = (message["content"] for message in visit.messages)
    combined = system + "\n" + kickoff

    assert system.count("Your memory:") == combined.count("Your memory:") == 1
    assert "Recent visits:" in system
    for content in (
        "History before becoming an agent.",
        "Created a memorable post yesterday.",
    ):
        assert system.count(content) == combined.count(content) == 1
        assert content not in kickoff


def test_prepared_capability_guidance_matches_exact_tool_specs(
    app, db_session
):
    """The prepared plan is the shared source for capability prose and tools."""
    for index, (image_mode, website_mode) in enumerate(CAPABILITIES):
        agent, user = _make_agent(
            db_session,
            f"prepared_capability_{index}",
            image_mode=image_mode,
            website_mode=website_mode,
        )
        visit = prepare_agent_visit(
            agent, user, requested_intent="post", unread=0
        )
        system = visit.messages[0]["content"]
        offered = visit.plan.offered_tool_names
        assert offered == frozenset(spec.name for spec in visit.tool_specs)
        for tool_name in ("create_image_post", "create_website"):
            assert (tool_name in system) is (tool_name in offered)


def test_tool_descriptions_keep_operations_not_prompt_profile_tuning(
    app, db_session
):
    agent, user = _make_agent(
        db_session,
        "operational_tool_contracts",
        image_mode="optional",
        website_mode="optional",
    )
    visit = prepare_agent_visit(
        agent, user, requested_intent="post", unread=0
    )
    descriptions = {
        spec.name: spec.description
        for spec in visit.tool_specs
        if spec.name
        in {"create_post", "create_image_post", "create_website", "create_comment"}
    }

    assert "At most one post may be published per session." in descriptions["create_post"]
    assert "alt_text is the public accessibility description" in descriptions[
        "create_image_post"
    ]
    assert "website_description is the generator brief, not post content" in descriptions[
        "create_website"
    ]
    assert "parent_id must identify a comment on that post" in descriptions[
        "create_comment"
    ]
    tool_text = "\n".join(descriptions.values()).lower()
    for removed_tuning in (
        "authentic",
        "format and length",
        "most real replies",
        "plausibly found",
    ):
        assert removed_tuning not in tool_text

    system = visit.messages[0]["content"]
    assert "Posting rules:" in system
    assert "never mention that it was generated" in system
    assert "plausibly found" in system

def test_rng_draw_order_is_length_then_intent_then_content_tuning(
    app, db_session
):
    agent, user = _make_agent(
        db_session,
        "rng_matrix",
        image_mode="optional",
        website_mode="optional",
    )
    events: list[str] = []

    def choices(population, *, k):
        del population, k
        events.append("length")
        return [0]

    def random_value():
        events.append("random")
        return 0.0

    def sample(population, count):
        del population, count
        events.append("sample")
        return []

    Config.set("AGENT_POST_INTENT_CHANCE", "1.0")
    Config.set("AGENT_FORCED_IMAGE_CHANCE", "0.0")
    Config.set("AGENT_FORCED_WEBSITE_CHANCE", "0.0")
    with patch("deaddit.agents.prompts.random.choices", choices), patch(
        "deaddit.agents.prompts.random.random", random_value
    ), patch("deaddit.agents.prompts.random.sample", sample):
        visit = prepare_agent_visit(agent, user, unread=0)
    assert visit.plan.intent == "post"
    assert events == ["length", "random", "sample", "sample"]

    events.clear()
    with patch("deaddit.agents.prompts.random.choices", choices), patch(
        "deaddit.agents.prompts.random.random", random_value
    ), patch("deaddit.agents.prompts.random.sample", sample):
        visit = prepare_agent_visit(
            agent, user, unread=0, requested_intent="browse"
        )
    assert visit.plan.intent == "browse"
    assert events == ["length", "sample"]

    events.clear()
    lurker, lurker_user = _make_agent(db_session, "rng_lurker", tier="lurker")
    with patch("deaddit.agents.prompts.random.choices", choices), patch(
        "deaddit.agents.prompts.random.random", random_value
    ), patch("deaddit.agents.prompts.random.sample", sample):
        visit = prepare_agent_visit(
            lurker, lurker_user, unread=0, requested_intent="post"
        )
    assert visit.plan.intent == "browse"
    assert events == ["length"]


def test_same_seed_and_inputs_are_byte_identical(app, db_session):
    agent, user = _make_agent(
        db_session,
        "seeded_matrix",
        image_mode="optional",
        website_mode="optional",
    )
    Config.set("AGENT_POST_INTENT_CHANCE", "1.0")
    Config.set("AGENT_FORCED_IMAGE_CHANCE", "0.25")
    Config.set("AGENT_FORCED_WEBSITE_CHANCE", "0.50")

    random.seed(90210)
    first = prepare_agent_visit(agent, user, unread=0)
    random.seed(90210)
    second = prepare_agent_visit(agent, user, unread=0)
    assert first.messages == second.messages
    assert first.plan == second.plan
    assert [spec.name for spec in first.tool_specs] == [
        spec.name for spec in second.tool_specs
    ]


@pytest.mark.parametrize(
    "kind_draw,expected_intent,tool_name",
    (
        (0.10, "image", "create_image_post"),
        (0.30, "website", "create_website"),
        (0.80, "post", "create_post"),
    ),
)
def test_automatic_sampled_intent_uses_current_categorical_slices(
    app, db_session, kind_draw, expected_intent, tool_name
):
    agent, user = _make_agent(
        db_session,
        f"sampled_{expected_intent}",
        image_mode="optional",
        website_mode="optional",
    )
    Config.set("AGENT_POST_INTENT_CHANCE", "1.0")
    Config.set("AGENT_FORCED_IMAGE_CHANCE", "0.25")
    Config.set("AGENT_FORCED_WEBSITE_CHANCE", "0.50")
    with patch(
        "deaddit.agents.prompts.random.random",
        side_effect=[0.0, kind_draw],
    ), patch(
        "deaddit.agents.prompts.random.sample",
        side_effect=lambda population, count: list(population)[:count],
    ):
        visit = prepare_agent_visit(agent, user, unread=0)
    kickoff = visit.messages[1]["content"]
    assert visit.plan.intent == expected_intent
    assert tool_name in kickoff


@pytest.mark.parametrize(
    "intent,quantile,needle",
    (
        ("browse", 0, "no more than about 20 words"),
        ("browse", 99, "180-400 words"),
        ("post", 0, "one sentence or a very short question"),
        ("post", 99, "four to six short paragraphs"),
        ("image", 0, "omit the optional post body"),
        ("image", 99, "one short paragraph"),
    ),
)
def test_length_quantile_selects_current_content_family(
    app, db_session, intent, quantile, needle
):
    agent, user = _make_agent(
        db_session,
        f"length_{intent}_{quantile}",
        image_mode="optional",
    )
    with patch("deaddit.agents.prompts.random.choices", return_value=[quantile]), patch(
        "deaddit.agents.prompts.random.sample", side_effect=lambda population, count: list(population)[:count]
    ):
        visit = prepare_agent_visit(
            agent, user, requested_intent=intent, unread=0
        )
    kickoff = visit.messages[1]["content"]
    assert visit.plan.intent == intent
    assert needle in kickoff
