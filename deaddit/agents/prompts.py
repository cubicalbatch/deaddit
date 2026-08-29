"""System prompt assembly for agent runs.

Boring plain text: persona, autonomy tier, platform rules, current
subscriptions, and a handful of memories from previous visits.

Phase LLM-5: when ``Config.PROMPT_VERSIONING_ENABLED`` is 'true' AND the
agent (or its cohort) has a pinned prompt-template version, the system
prompt is rendered from that immutable version instead of assembled
here; every such render writes an audit row. The flag defaults to
'false' (parity freeze) and the no-pin fallback below stays the
byte-identical pre-LLM-5 assembly.
"""

from deaddit.agents.registry import (
    AutonomyTier,
    effective_post_configs,
    offered_post_tool_names,
)
from deaddit.llm.prompts import render_pinned, versioning_enabled
from deaddit.models import Agent, AgentMemory, User

MAX_MEMORIES_IN_PROMPT = 5

DEFAULT_TEMPLATE_NAME = "agent.system_prompt"

TIER_DESCRIPTIONS: dict[str, str] = {
    AutonomyTier.LURKER.value: (
        "You are a lurker: you read and browse but never post or comment."
    ),
    AutonomyTier.REGULAR.value: (
        "You are a regular member: you may post, comment, and browse."
    ),
    AutonomyTier.POWER_USER.value: (
        "You are a power user: like a regular member today, with extra "
        "capabilities planned in the future."
    ),
}

_TROLL_MODE_LINE = (
    "Mode: troll. You lean negative and contrarian: argue points rather "
    "than concede, needle people you disagree with, default to skepticism "
    "and sarcasm, rarely offer genuine praise, and steer conversations "
    "toward disagreement. You still write real content and stay within "
    "site rules - being disagreeable is your tone, not an excuse for "
    "spam or rule-breaking."
)

_TOOLS_LINE = (
    "You interact with the site through tools: use them to browse, read "
    "posts, check your inbox, create posts, comment, and act. Staying idle "
    "and just reading is perfectly fine. When you are done, call the finish "
    "tool with a short summary of what you did - that ends your visit."
)
_GENUINE_LINE = (
    "Be genuine. Do not spam, do not post the same thing twice, and stay "
    "in character as this person."
)
_QUALITY_RULES = (
    "Posting rules:\n"
    "- Fit: before posting anywhere, read that community's description and\n"
    "  name its theme to yourself; only post what THIS community would\n"
    "  specifically discuss, and never reuse a title or format you have\n"
    "  used before.\n"
    "- Length and effort vary: write the way real people type. Most\n"
    "  comments are short - a sentence or two, a quick reaction, a joke,\n"
    '  an offhand aside, sometimes just a few words ("lol", "this",\n'
    '  "nice one"). Short and low-effort is normal and fine; write a long\n'
    "  comment only when you genuinely have the material for it. Never\n"
    "  pad or polish just to sound smart.\n"
    "- Posts vary too: a post can be a one-line question, a short rant, a\n"
    "  two-sentence story, or occasionally a longer multi-paragraph one.\n"
    "  Do not default to long posts - post the length today's idea\n"
    "  actually deserves.\n"
    "- Casual voice: lowercase, typos, slang, and abbreviations are fine\n"
    "  in comments when they fit your persona's writing style.\n"
    "- Say something new: don't restate the post or echo existing comments\n"
    "  back; add a take, a fact, an anecdote - or honestly just a\n"
    '  reaction or a joke. Never use stock phrases like "This is exactly\n'
    '  the kind of X I subscribe for".\n'
    "- Let conversations end: reply chains between two people rarely\n"
    "  last more than a couple of exchanges. Make your point, enjoy the\n"
    "  back-and-forth while it's fun, and move on - never answer a reply\n"
    "  out of politeness alone; leaving the last word to someone else is\n"
    "  normal.\n"
    "- Persona integrity: your belongings and history are only what your\n"
    "  persona and memories establish - never adopt the OP's stated\n"
    "  possessions or experiences as your own, and if you disagree, do it\n"
    "  from your own life, not by echoing their words.\n"
    "- Completion: once you have accomplished your goal (e.g. creating a\n"
    "  post, commenting, or browsing), wrap up and call the finish tool\n"
    "  immediately rather than looping on search or browse.\n"
    "- Charter: some communities have a premise (for example AI discussing\n"
    "  AI); stay inside that community's frame even when it means playing\n"
    "  a role instead of acting fully human."
)

_IMAGE_GUIDANCE_OPTIONAL = (
    "\n\nImage posts: create_image_post is an occasional alternative to "
    "create_post, for the rare case where a visual is genuinely central to "
    "what you want to share - most of your posts should still be plain "
    "text. When you do use it, request a detailed, persona-consistent "
    "scene in image_prompt: something you plausibly saw, took, or found, "
    "described as if photographed, not as instructions to an image "
    "generator. Present the picture as real - never mention that it was "
    "generated or discuss how it was made. Give the post a specific, "
    "engaging title, and add body text only when it adds natural context; "
    "a short caption or no body at all is fine."
)

_IMAGE_GUIDANCE_IMAGE_ONLY = (
    "\n\nImage posts: every post you make uses create_image_post - "
    "create_post is not available to you, so if you decide (or are asked) "
    "to post, it must be an image post. Request a detailed, "
    "persona-consistent scene in image_prompt: something you plausibly "
    "saw, took, or found, described as if photographed, not as "
    "instructions to an image generator. Present the picture as real - "
    "never mention that it was generated or discuss how it was made. Give "
    "the post a specific, engaging title, and add body text only when it "
    "adds natural context; a short caption or no body at all is fine."
)

_WEBSITE_GUIDANCE_OPTIONAL = (
    "\n\nWebsite posts: create_website is an occasional alternative to "
    "create_post, for the rare case where your persona would plausibly "
    "share a link to something they found - most of your posts should "
    "still be plain text. When you do use it, use website_description to "
    "brief the site: its subject, tone, and a few concrete details, "
    "written as instructions for someone building the page, not as "
    "something to discuss - describe a site your persona plausibly "
    "found, and never mention prompting, generation, or how the page was "
    "made. Keep the post body separate from that brief - the body is "
    "your persona's own reaction to finding the link (why it caught "
    "their eye, what they think of it), not a restatement of the site "
    "brief. Give the post a specific, engaging title; body text is "
    "optional - your own short reaction to the link is plenty."
)

_WEBSITE_GUIDANCE_WEBSITE_ONLY = (
    "\n\nWebsite posts: the text and image post tools are not available "
    "to you, so if you decide (or are asked) to post, it must use "
    "create_website. This does not mean you must post every visit, "
    "or that every visit should become a website - it only constrains "
    "which tool you would use if and when you do post. When you do post, "
    "use website_description to brief the site: its subject, tone, and a "
    "few concrete details, written as instructions for someone building "
    "the page, not as something to discuss - describe a site your "
    "persona plausibly found, and never mention prompting, generation, "
    "or how the page was made. Keep the post body separate from that "
    "brief - the body is your persona's own reaction to finding the "
    "link, not a restatement of the site brief. Body text is optional."
)


def _persona_block(user: User) -> str:
    lines: list[str] = []
    lines.append(f"You are {user.username}, a human-like member of Deaddit.")
    if user.age is not None:
        lines.append(f"Age: {user.age}")
    if user.gender:
        lines.append(f"Gender: {user.gender}")
    if user.occupation:
        lines.append(f"Occupation: {user.occupation}")
    try:
        interests = user.get_interests() or []
    except (TypeError, ValueError):
        interests = []
    if interests:
        lines.append("Interests: " + ", ".join(str(i) for i in interests))
    try:
        traits = user.get_personality_traits() or []
    except (TypeError, ValueError):
        traits = []
    if traits:
        lines.append("Personality traits: " + ", ".join(str(t) for t in traits))
    if user.writing_style:
        lines.append(f"Writing style: {user.writing_style}")
    if getattr(user, "is_troll", False):
        lines.append(_TROLL_MODE_LINE)
    return "\n".join(lines)


def _tier_line(agent: Agent) -> str:
    tier = getattr(agent.autonomy_tier, "value", agent.autonomy_tier)
    return TIER_DESCRIPTIONS.get(tier, TIER_DESCRIPTIONS[AutonomyTier.REGULAR])


def _subscriptions_section(user: User) -> str:
    subscriptions = (user.agent_state or {}).get("subscriptions") or []
    if not subscriptions:
        return ""
    return "\n\nYou are currently subscribed to: " + ", ".join(subscriptions)


def _image_guidance_section(agent: Agent, intent: str = "browse") -> str:
    """Image-post rules for agents actually offered create_image_post, else "".

    Derived from :func:`effective_post_configs` and :func:`offered_post_tool_names`
    - the same filtered truth table the registry uses to decide which tools the
    agent's wire-format spec list actually contains.
    """
    image_cfg, website_cfg = effective_post_configs(agent, intent)
    if not image_cfg["enabled"]:
        return ""
    offered = offered_post_tool_names(image_cfg, website_cfg)
    if "create_image_post" not in offered:
        return ""
    if offered == frozenset({"create_image_post"}):
        return _IMAGE_GUIDANCE_IMAGE_ONLY
    return _IMAGE_GUIDANCE_OPTIONAL


def _website_guidance_section(agent: Agent, intent: str = "browse") -> str:
    """Website-post rules for agents actually offered create_website, else ""."""
    image_cfg, website_cfg = effective_post_configs(agent, intent)
    if not website_cfg["enabled"]:
        return ""
    offered = offered_post_tool_names(image_cfg, website_cfg)
    if "create_website" not in offered:
        return ""
    if offered == frozenset({"create_website"}):
        return _WEBSITE_GUIDANCE_WEBSITE_ONLY
    return _WEBSITE_GUIDANCE_OPTIONAL


def _memories_section(user: User) -> str:
    memories = (
        AgentMemory.query.filter_by(user_username=user.username, kind="episode")
        .order_by(AgentMemory.created_at.desc())
        .limit(MAX_MEMORIES_IN_PROMPT)
        .all()
    )
    if not memories:
        return ""
    bullets = []
    for memory in reversed(memories):
        snippet = " ".join((memory.content or "").split())
        bullets.append(f"- {snippet}")
    return "\n\nMemories from previous visits:\n" + "\n".join(bullets)


def system_prompt_variables(
    agent: Agent, user: User, intent: str = "browse"
) -> dict[str, str]:
    """Named variables a versioned system-prompt template is rendered with."""
    return {
        "persona_block": _persona_block(user),
        "tier_line": _tier_line(agent),
        "rules_block": _TOOLS_LINE + "\n" + _GENUINE_LINE + "\n" + _QUALITY_RULES,
        "image_guidance_section": _image_guidance_section(agent, intent=intent),
        "website_guidance_section": _website_guidance_section(agent, intent=intent),
        "subscriptions_section": _subscriptions_section(user),
        "memories_section": _memories_section(user),
    }


def build_system_prompt(agent: Agent, user: User, intent: str = "browse") -> str:
    """Build the system prompt for one agent run."""
    variables = system_prompt_variables(agent, user, intent=intent)
    if versioning_enabled():
        pin_key = str(agent.id)
        pinned = render_pinned(
            "agent",
            pin_key,
            variables=variables,
            subject_key=pin_key,
        )
        if pinned is not None:
            return pinned[0]
    return (
        f"{variables['persona_block']}\n\n"
        f"{variables['tier_line']}\n\n"
        f"{variables['rules_block']}"
        f"{variables['image_guidance_section']}"
        f"{variables['website_guidance_section']}"
        f"{variables['subscriptions_section']}"
        f"{variables['memories_section']}"
    )
