"""Conversation bootstrap for agent runs."""

from deaddit.agents.prompts import build_system_prompt
from deaddit.extensions import db
from deaddit.models import Agent, User

KICKOFF_PROMPT = (
    "You're waking up. Browse, catch up on replies, act if you feel like it, "
    "then finish."
)

INBOX_NOTICE = (
    "If you have unread replies, use the view_inbox tool to read them before "
    "deciding what to do."
)

def build_initial_messages(agent: Agent) -> list[dict]:
    """Build the opening messages array for an agent conversation."""
    user = db.session.get(User, agent.user_username)
    messages: list[dict] = [
        {"role": "system", "content": build_system_prompt(agent, user)},
        {"role": "user", "content": KICKOFF_PROMPT},
    ]
    messages[-1]["content"] += " " + INBOX_NOTICE
    return messages
