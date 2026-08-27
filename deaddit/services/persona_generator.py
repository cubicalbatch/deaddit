"""Persona generation service for Deaddit.

Generates structured, human-like user personas using the configured LLM endpoint,
persists them via `create_user()`, and optionally enrolls them as autonomous agents.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from deaddit.config import Config
from deaddit.extensions import db
from deaddit.llm import ChatRequest, LLMClient, Sampling, routing
from deaddit.models import Agent, User
from deaddit.services.content import create_user

logger = logging.getLogger(__name__)

__all__ = ["PersonaGenerationError", "generate_personas"]

SYSTEM_PROMPT = (
    "You are an expert AI persona designer for an online community forum called Deaddit.\n"
    "Generate unique, realistic, human-like user personas with distinct personalities, "
    "writing styles, backgrounds, and interests.\n"
    "Respond ONLY with a valid JSON array of objects conforming to the requested schema, "
    "with no markdown codeblocks or conversational filler."
)

USER_PROMPT_TEMPLATE = (
    "Generate {count} unique human-like user personas for the forum.{topic_section}\n\n"
    "Each persona object must have the following fields:\n"
    '- "username": string (creative username, alphanumeric with underscores, 3-25 characters)\n'
    '- "bio": string (authentic personal bio, 1-3 sentences)\n'
    '- "age": integer (realistic age, between 18 and 75)\n'
    '- "gender": string ("Male" or "Female")\n'
    '- "occupation": string (job or daily occupation)\n'
    '- "education": string (highest level of education or study)\n'
    '- "interests": list of strings (3 to 6 specific interests or hobbies)\n'
    '- "personality_traits": list of strings (3 to 6 descriptive traits)\n'
    '- "writing_style": string (description of their online writing tone and style)\n\n'
    "Output JSON format (must be a valid JSON array):\n"
    "[\n"
    "  {{\n"
    '    "username": "example_user",\n'
    '    "bio": "...",\n'
    '    "age": 28,\n'
    '    "gender": "Female",\n'
    '    "occupation": "Software Engineer",\n'
    '    "education": "B.S. Computer Science",\n'
    '    "interests": ["coding", "sci-fi", "hiking"],\n'
    '    "personality_traits": ["analytical", "curious", "dry wit"],\n'
    '    "writing_style": "concise, lowercase, uses technical terms"\n'
    "  }}\n"
    "]"
)


class PersonaGenerationError(Exception):
    """Raised when persona generation or parsing fails."""


def _extract_json(raw: str) -> list[dict]:
    """Extract a list of persona dicts from LLM text output."""
    cleaned = raw.strip()
    # Strip markdown codeblocks
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2:
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

    # Try direct parse
    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            for key in ("personas", "users", "data", "results"):
                if key in data and isinstance(data[key], list):
                    return [d for d in data[key] if isinstance(d, dict)]
            return [data]
    except Exception:
        pass

    # Try finding outer [ ... ]
    start_bracket = cleaned.find("[")
    end_bracket = cleaned.rfind("]")
    if start_bracket != -1 and end_bracket != -1 and end_bracket > start_bracket:
        try:
            data = json.loads(cleaned[start_bracket : end_bracket + 1])
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        except Exception:
            pass

    # Try finding outer { ... }
    start_brace = cleaned.find("{")
    end_brace = cleaned.rfind("}")
    if start_brace != -1 and end_brace != -1 and end_brace > start_brace:
        try:
            data = json.loads(cleaned[start_brace : end_brace + 1])
            if isinstance(data, dict):
                for key in ("personas", "users", "data", "results"):
                    if key in data and isinstance(data[key], list):
                        return [d for d in data[key] if isinstance(d, dict)]
                return [data]
        except Exception:
            pass

    raise PersonaGenerationError(
        f"Could not parse valid persona JSON from LLM response: {raw[:200]}"
    )


def _sanitize_persona(item: dict, seen_usernames: set[str]) -> dict:
    """Sanitize and normalize persona dictionary fields."""
    raw_username = str(item.get("username") or "").strip()
    clean_username = re.sub(r"[^a-zA-Z0-9_]", "_", raw_username)
    clean_username = re.sub(r"_+", "_", clean_username).strip("_")
    if not clean_username:
        clean_username = f"user_{uuid.uuid4().hex[:8]}"
    clean_username = clean_username[:40]

    candidate = clean_username
    suffix = 1
    while (
        candidate in seen_usernames
        or User.query.filter_by(username=candidate).first() is not None
    ):
        candidate = f"{clean_username[:35]}_{suffix}"
        suffix += 1
    seen_usernames.add(candidate)

    try:
        age = int(item.get("age", 25))
    except (TypeError, ValueError):
        age = 25
    age = max(18, min(age, 99))

    raw_gender = str(item.get("gender") or "").strip().lower()
    gender = "Female" if "female" in raw_gender or raw_gender == "f" else "Male"

    bio = str(item.get("bio") or "").strip() or "Deaddit community member."
    occupation = str(item.get("occupation") or "").strip() or "Community Member"
    education = str(item.get("education") or "").strip() or "Self-taught"

    raw_interests = item.get("interests")
    if isinstance(raw_interests, list):
        interests = [str(x).strip() for x in raw_interests if str(x).strip()]
    elif isinstance(raw_interests, str) and raw_interests.strip():
        interests = [x.strip() for x in raw_interests.split(",") if x.strip()]
    else:
        interests = []
    if not interests:
        interests = ["general discussion", "technology"]

    raw_traits = item.get("personality_traits")
    if isinstance(raw_traits, list):
        traits = [str(x).strip() for x in raw_traits if str(x).strip()]
    elif isinstance(raw_traits, str) and raw_traits.strip():
        traits = [x.strip() for x in raw_traits.split(",") if x.strip()]
    else:
        traits = []
    if not traits:
        traits = ["curious", "friendly"]

    writing_style = (
        str(item.get("writing_style") or "").strip() or "Conversational and thoughtful"
    )

    return {
        "username": candidate,
        "age": age,
        "gender": gender,
        "bio": bio,
        "occupation": occupation,
        "education": education,
        "interests": interests,
        "personality_traits": traits,
        "writing_style": writing_style,
    }


def _user_to_dict(user: User) -> dict[str, Any]:
    return {
        "username": user.username,
        "age": user.age,
        "gender": user.gender,
        "bio": user.bio,
        "occupation": user.occupation,
        "education": user.education,
        "interests": user.get_interests() if user.interests else [],
        "personality_traits": (
            user.get_personality_traits() if user.personality_traits else []
        ),
        "writing_style": user.writing_style,
        "model": user.model,
    }


def _agent_to_dict(agent: Agent) -> dict[str, Any]:
    return {
        "id": agent.id,
        "user_username": agent.user_username,
        "autonomy_tier": agent.autonomy_tier,
        "is_enabled": bool(agent.is_enabled),
        "status": agent.status,
        "config": agent.config or {},
        "state": agent.state or {},
        "last_run_at": agent.last_run_at.isoformat() if agent.last_run_at else None,
        "next_run_at": agent.next_run_at.isoformat() if agent.next_run_at else None,
        "consecutive_failures": int(agent.consecutive_failures or 0),
    }


MAX_PERSONAS_COUNT = 500
PERSONA_BATCH_SIZE = 10


def generate_personas(
    *,
    count: int = 1,
    topic_hint: str | None = None,
    auto_create_agent: bool = True,
    tier: str = "regular",
    api_url: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Generate human-like user personas using LLM, persist them and optionally create Agents.

    Args:
        count: Number of personas to generate (1 to 500).
        topic_hint: Optional thematic or topical prompt.
        auto_create_agent: If True, creates and activates an Agent for each user.
        tier: Autonomy tier for created agents ("regular", "power_user", "lurker").
        api_url: Optional override LLM API endpoint URL.
        model: Optional override LLM model name.

    Returns:
        Dictionary containing `users` (list of user dicts) and `agents` (list of agent dicts).
    """
    if not isinstance(count, int) or count < 1 or count > MAX_PERSONAS_COUNT:
        raise ValueError(f"Count must be an integer between 1 and {MAX_PERSONAS_COUNT}")

    if tier not in ("lurker", "regular", "power_user"):
        raise ValueError(
            f"Invalid tier '{tier}'. Must be one of: lurker, regular, power_user"
        )

    if not api_url or not model:
        first_agent = Agent.query.first()
        agent_cfg = (first_agent.config or {}) if first_agent else {}
        active_url = agent_cfg.get("api_url")
        active_model = agent_cfg.get("model")

        resolved_url, resolved_model = routing.resolve()
        api_url = api_url or active_url or resolved_url
        model = model or active_model or resolved_model

    api_key = None
    if api_url:
        try:
            api_key = Config.get_api_key_for_endpoint(api_url)
        except Exception:
            api_key = None

    topic_section = (
        f"\nTheme/Topic focus: The personas should relate to or have strong interest in: {topic_hint.strip()}.\n"
        if topic_hint and topic_hint.strip()
        else ""
    )

    seen_usernames: set[str] = set()
    created_users: list[User] = []
    created_agents: list[Agent] = []
    client = LLMClient()

    while len(created_users) < count:
        batch_target = min(count - len(created_users), PERSONA_BATCH_SIZE)
        user_prompt = USER_PROMPT_TEMPLATE.format(
            count=batch_target,
            topic_section=topic_section,
        )

        req = ChatRequest(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model=model,
            api_url=api_url,
            api_key=api_key,
            sampling=Sampling(max_tokens=4096, temperature=0.8),
        )

        result = client.complete(req)
        raw_personas = _extract_json(result.content)
        if not raw_personas:
            if created_users:
                break
            raise PersonaGenerationError("LLM returned empty personas list")

        for raw_p in raw_personas:
            if len(created_users) >= count:
                break
            p = _sanitize_persona(raw_p, seen_usernames)
            user = create_user(
                username=p["username"],
                age=p["age"],
                gender=p["gender"],
                bio=p["bio"],
                interests=p["interests"],
                occupation=p["occupation"],
                education=p["education"],
                writing_style=p["writing_style"],
                personality_traits=p["personality_traits"],
                model=model,
            )
            created_users.append(user)

            if auto_create_agent:
                agent_config = {
                    "max_actions_per_run": 30,
                    "min_delay": 300,
                    "max_delay": 1800,
                    "api_url": api_url,
                    "model": model,
                }
                agent = Agent(
                    user_username=user.username,
                    autonomy_tier=tier,
                    is_enabled=True,
                    status="idle",
                    config=agent_config,
                    state={},
                    consecutive_failures=0,
                    next_run_at=datetime.utcnow(),
                )
                db.session.add(agent)
                created_agents.append(agent)

        if created_agents:
            db.session.commit()

    return {
        "users": [_user_to_dict(u) for u in created_users],
        "agents": [_agent_to_dict(a) for a in created_agents],
    }
