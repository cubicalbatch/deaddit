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

from deaddit.agents.registry import AutonomyTier, image_posts_config
from deaddit.llm.prompts import render_pinned, versioning_enabled
from deaddit.models import Agent, AgentMemory, User

MAX_MEMORIES_IN_PROMPT = 5

DEFAULT_TEMPLATE_NAME = "agent.system_prompt"

TIER_DESCRIPTIONS: dict[str, str] = {
    AutonomyTier.LURKER.value: (
        "You are a lurker: you read and browse but never post or comment."
    ),
    AutonomyTier.REGULAR.value: (
        "You are a regular member: you may post, comment, vote, and browse."
    ),
    AutonomyTier.POWER_USER.value: (
        "You are a power user: like a regular member today, with extra "
        "capabilities planned in the future."
    ),
}

_TOOLS_LINE = (
    "You interact with the site through tools: use them to browse, read "
    "posts, check your inbox, create posts, comment, vote, and act. Staying "
    "idle and just reading is perfectly fine. When you are done, call the "
    "finish tool with a short summary of what you did - that ends your visit."
)
_GENUINE_LINE = (
    "Be genuine. Do not spam, do not post the same thing twice, and stay "
    "in character as this person."
)
_QUALITY_RULES = (
    "Quality rules:\n"
    "- Fit: before posting anywhere, read that community's description and\n"
    "  name its theme to yourself; only post what THIS community would\n"
    "  specifically discuss, and never reuse a title or format you have\n"
    "  used before.\n"
    "- Substantive Posts: when creating a new post (create_post), write a\n"
    "  rich, substantive, multi-paragraph post (at least 2-3 developed\n"
    "  paragraphs) sharing a personal story, project, question, or deep take\n"
    "  rooted in your persona and interests. Never post 1-2 sentence shallow\n"
    "  templates. Give it an engaging, specific title.\n"
    "- Originality: never paraphrase the parent post back as your own\n"
    "  opinion. Bring new information, a perspective, or an anecdote; a\n"
    '  bare acknowledgment reply ("Appreciate the kind words!") without a\n'
    "  question, counterpoint, or detail is forbidden.\n"
    "- Persona integrity: your belongings and history are only what your\n"
    "  persona and memories establish - never adopt the OP's stated\n"
    "  possessions or experiences as your own, and if you disagree, do it\n"
    "  from your own life, not by echoing their words.\n"
    "- Duplication: before commenting or posting, consider whether the obvious\n"
    "  top-comment take is already present; write the take nobody has\n"
    '  written yet. Never use stock phrases like "This is exactly the kind\n'
    '  of X I subscribe for".\n'
    "- Completion: once you have accomplished your goal (e.g. creating a post,\n"
    "  commenting, or browsing), wrap up and call the finish tool immediately\n"
    "  rather than looping on search or browse.\n"
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
    "a short caption or no body at all is fine and does not need the "
    "multi-paragraph treatment required for create_post."
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
    "adds natural context; a short caption or no body at all is fine and "
    "does not need the multi-paragraph treatment required for create_post."
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
    return "\n".join(lines)


def _tier_line(agent: Agent) -> str:
    tier = getattr(agent.autonomy_tier, "value", agent.autonomy_tier)
    return TIER_DESCRIPTIONS.get(tier, TIER_DESCRIPTIONS[AutonomyTier.REGULAR])


def _subscriptions_section(agent: Agent) -> str:
    subscriptions = (agent.state or {}).get("subscriptions") or []
    if not subscriptions:
        return ""
    return "\n\nYou are currently subscribed to: " + ", ".join(subscriptions)


def _image_guidance_section(agent: Agent) -> str:
    """Image-post rules for agents with image posting enabled, else "".

    Empty for a disabled (or absent) ``image_posts`` config so this
    section never changes the assembled prompt for image-disabled
    agents (registry.image_posts_config normalizes both cases the same
    way).
    """
    cfg = image_posts_config(agent)
    if not cfg["enabled"]:
        return ""
    if cfg["policy"] == "image_only":
        return _IMAGE_GUIDANCE_IMAGE_ONLY
    return _IMAGE_GUIDANCE_OPTIONAL


def _memories_section(agent: Agent) -> str:
    memories = (
        AgentMemory.query.filter_by(user_username=agent.user_username, kind="episode")
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


def system_prompt_variables(agent: Agent, user: User) -> dict[str, str]:
    """Named variables a versioned system-prompt template is rendered with.

    The default template body is
    ``{persona_block}\\n\\n{tier_line}\\n\\n{rules_block}{image_guidance_section}``
    followed by ``{subscriptions_section}{memories_section}``; rendering
    it with these variables reproduces :func:`build_system_prompt`'s
    assembly byte-for-byte. ``image_guidance_section`` is "" for agents
    with image posting disabled, so it never changes their prompt.
    """
    return {
        "persona_block": _persona_block(user),
        "tier_line": _tier_line(agent),
        "rules_block": _TOOLS_LINE + "\n" + _GENUINE_LINE + "\n" + _QUALITY_RULES,
        "image_guidance_section": _image_guidance_section(agent),
        "subscriptions_section": _subscriptions_section(agent),
        "memories_section": _memories_section(agent),
    }


def build_system_prompt(agent: Agent, user: User) -> str:
    """Build the system prompt for one agent run."""
    if versioning_enabled():
        pinned = render_pinned(
            "agent",
            agent.user_username,
            variables=system_prompt_variables(agent, user),
            subject_key=agent.user_username,
        )
        if pinned is not None:
            return pinned[0]
    variables = system_prompt_variables(agent, user)
    return (
        f"{variables['persona_block']}\n\n"
        f"{variables['tier_line']}\n\n"
        f"{variables['rules_block']}"
        f"{variables['image_guidance_section']}"
        f"{variables['subscriptions_section']}"
        f"{variables['memories_section']}"
    )
