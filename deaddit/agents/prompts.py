"""System prompt assembly for agent runs.

Boring plain text: persona, autonomy tier, platform rules, current
subscriptions, and a handful of memories from previous visits.
"""

from deaddit.agents.registry import AutonomyTier
from deaddit.models import Agent, AgentMemory, User

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

MAX_MEMORIES_IN_PROMPT = 5


def build_system_prompt(agent: Agent, user: User) -> str:
    """Build the system prompt for one agent run."""
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

    tier = getattr(agent.autonomy_tier, "value", agent.autonomy_tier)
    lines.append("")
    lines.append(TIER_DESCRIPTIONS.get(tier, TIER_DESCRIPTIONS[AutonomyTier.REGULAR]))

    lines.append("")
    lines.append(
        "You interact with the site through tools: use them to browse, read "
        "posts, check your inbox, and act. Staying idle and just reading is "
        "perfectly fine. When you are done, call the finish tool with a short "
        "summary of what you did - that ends your visit."
    )
    lines.append(
        "Be genuine. Do not spam, do not post the same thing twice, and stay "
        "in character as this person."
    )

    state = agent.state or {}
    subscriptions = state.get("subscriptions") or []
    if subscriptions:
        lines.append("")
        lines.append("You are currently subscribed to: " + ", ".join(subscriptions))

    memories = (
        AgentMemory.query.filter_by(agent_id=agent.id, kind="episode")
        .order_by(AgentMemory.created_at.desc())
        .limit(MAX_MEMORIES_IN_PROMPT)
        .all()
    )
    if memories:
        lines.append("")
        lines.append("Memories from previous visits:")
        for memory in reversed(memories):
            snippet = " ".join((memory.content or "").split())
            lines.append(f"- {snippet}")

    return "\n".join(lines)
