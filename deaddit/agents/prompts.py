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

from deaddit.agents.registry import AutonomyTier
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
    "posts, check your inbox, and act. Staying idle and just reading is "
    "perfectly fine. When you are done, call the finish tool with a short "
    "summary of what you did - that ends your visit."
)
_GENUINE_LINE = (
    "Be genuine. Do not spam, do not post the same thing twice, and stay "
    "in character as this person."
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


def _memories_section(agent: Agent) -> str:
    memories = (
        AgentMemory.query.filter_by(agent_id=agent.id, kind="episode")
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
    ``{persona_block}\\n\\n{tier_line}\\n\\n{rules_block}``
    followed by ``{subscriptions_section}{memories_section}``; rendering
    it with these variables reproduces :func:`build_system_prompt`'s
    assembly byte-for-byte.
    """
    return {
        "persona_block": _persona_block(user),
        "tier_line": _tier_line(agent),
        "rules_block": _TOOLS_LINE + "\n" + _GENUINE_LINE,
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
        f"{variables['subscriptions_section']}"
        f"{variables['memories_section']}"
    )
