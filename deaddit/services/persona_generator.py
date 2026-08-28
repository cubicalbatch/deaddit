"""Persona generation service for Deaddit.

Generates structured, human-like user personas using the configured LLM endpoint,
persists them via `create_user()`, and optionally enrolls them as autonomous agents.
"""

from __future__ import annotations

import json
import logging
import random
import re
import uuid
from datetime import datetime
from typing import Any

from deaddit.config import Config
from deaddit.extensions import db
from deaddit.llm import ChatRequest, LLMClient, LLMError, Sampling, routing
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

USERNAME_STYLE_RULES = (
    "lowercase; only letters, digits, underscores; 4-20 characters; "
    "must NOT describe the user's personality (never two trait-words like "
    "chill_dude, cozy_ghost, quiet_thinker); no real people's names."
)

USER_PROMPT_TEMPLATE = (
    "Generate {count} unique human-like user personas for the forum.{topic_section}"
    "{troll_section}\n\n"
    "Each persona is assigned a username style. The username for Persona N "
    "must follow Persona N's style:\n"
    "{style_assignments}\n\n"
    f"Username rules: {USERNAME_STYLE_RULES}\n\n"
    "Each persona object must have the following fields:\n"
    '- "username": string following the style assigned to that persona\n'
    '- "bio": string (authentic personal bio, 1-3 sentences)\n'
    '- "age": integer (realistic age, between 18 and 75)\n'
    '- "gender": string ("Male" or "Female")\n'
    '- "occupation": string (job or daily occupation)\n'
    '- "education": string (highest level of education or study)\n'
    '- "interests": list of strings (3 to 6 specific interests or hobbies)\n'
    '- "personality_traits": list of strings (3 to 6 descriptive traits)\n'
    '- "writing_style": string (description of their online writing tone and '
    "style - the batch MUST span the full realistic range: roughly half "
    "casual and short-form (terse, lowercase, typo-prone, jokey, plain "
    "one-liners), a few mid-range conversational, and only a minority "
    "articulate and verbose; never default the whole batch to thoughtful "
    "long-form)\n"
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

TROLL_SECTION = (
    "\nDesign these personas as trolls: argumentative, contrarian, and\n"
    "negative. Each one picks fights in comments, dismisses mainstream\n"
    "views, rarely concedes a point, and adds little constructive value -\n"
    "while still being a realistic, believable person with a plausible\n"
    "job, interests, and backstory. Their trollishness reads as an\n"
    "exaggerated personality of someone convinced they are the only\n"
    "reasonable one, not a caricature.\n"
)

USERNAME_STYLE_CARDS: list[tuple[str, str]] = [
    (
        "phrase",
        "a short humorous phrase handle, e.g. pm_me_your_turtle, i_hate_mondays, legally_a_bird",
    ),
    (
        "mashup",
        "two completely unrelated words mashed together, e.g. toaster_falcon, gravel_piano, sasquatch_ledger",
    ),
    (
        "wordplay",
        "a pun or wordplay on a familiar phrase, e.g. ctrl_alt_defeat, thai_tanic, lug_wrench_romantic",
    ),
    (
        "imperative",
        "an imperative verb + noun, e.g. adopt_a_duck, fear_the_soup, recycle_your_dad",
    ),
    (
        "evocative",
        "a single evocative word + 2-4 digit number, e.g. moonlit_4821, harbor_77, verdigris_302",
    ),
]

USERNAME_STYLE_RULES = (
    "lowercase; only letters, digits, underscores; 4-20 characters; "
    "must NOT describe the user's personality (never two trait-words like "
    "chill_dude, cozy_ghost, quiet_thinker); no real people's names."
)


class PersonaGenerationError(Exception):
    """Raised when persona generation or parsing fails."""


def _assign_styles(n: int) -> list[str]:
    """Assign a random username style directive to each persona in a batch."""
    return random.choices([d for _, d in USERNAME_STYLE_CARDS], k=n)


def _apply_casing(name: str) -> str:
    """Post-treat a resolved username: 50% snake_case, 25% PascalCase, 25% camelCase."""
    r = random.random()
    if r < 0.25:  # PascalCase
        return "".join(p.capitalize() for p in name.split("_"))
    if r < 0.5:  # camelCase
        parts = name.split("_")
        return parts[0] + "".join(p.capitalize() for p in parts[1:])
    return name  # snake_case, unchanged


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

    candidate = _apply_casing(clean_username)
    suffix = 1
    while (
        candidate.lower() in seen_usernames
        or User.query.filter(db.func.lower(User.username) == candidate.lower()).first()
        is not None
    ):
        # Retry with the suffixed base; casing is re-rolled inside the loop
        # so the stored (cased) name is always the one checked for clashes.
        candidate = _apply_casing(f"{clean_username[:35]}_{suffix}")
        suffix += 1
    seen_usernames.add(candidate.lower())

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
        "is_troll": bool(user.is_troll),
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
PERSONA_BATCH_ATTEMPTS = 3


def _batch_sizes(total: int) -> list[int]:
    """Split ``total`` personas into batch sizes of PERSONA_BATCH_SIZE + remainder."""
    full, remainder = divmod(total, PERSONA_BATCH_SIZE)
    sizes = [PERSONA_BATCH_SIZE] * full
    if remainder:
        sizes.append(remainder)
    return sizes


def _batch_plan(troll_count: int, normal_count: int) -> list[tuple[bool, int]]:
    """Batch schedule as ``(is_troll, batch_size)`` pairs.

    Batches are homogeneous (all trolls or all normals) because the troll
    directive applies to the whole prompt. Troll batches are spread evenly
    across the schedule instead of running first, so a partially completed
    run stays close to the requested ratio.
    """
    troll_sizes = _batch_sizes(troll_count)
    normal_sizes = _batch_sizes(normal_count)
    total = len(troll_sizes) + len(normal_sizes)
    is_troll_batch = [False] * total
    for k in range(len(troll_sizes)):
        # k-th troll batch lands at its evenly spaced slot; indices are
        # distinct because len(troll_sizes) <= total.
        is_troll_batch[(k + 1) * total // len(troll_sizes) - 1] = True
    plan: list[tuple[bool, int]] = []
    troll_i = normal_i = 0
    for is_troll in is_troll_batch:
        if is_troll:
            plan.append((True, troll_sizes[troll_i]))
            troll_i += 1
        else:
            plan.append((False, normal_sizes[normal_i]))
            normal_i += 1
    return plan


def generate_personas(
    *,
    count: int = 1,
    topic_hint: str | None = None,
    auto_create_agent: bool = True,
    tier: str = "regular",
    api_url: str | None = None,
    model: str | None = None,
    troll_mode: str = "chance",
) -> dict[str, Any]:
    """Generate human-like user personas using LLM, persist them and optionally create Agents.

    Args:
        count: Number of personas to generate (1 to 500).
        topic_hint: Optional thematic or topical prompt.
        auto_create_agent: If True, creates and activates an Agent for each user.
        tier: Autonomy tier for created agents ("regular", "power_user", "lurker").
        model: Optional override LLM model name.
        troll_mode: Persona troll assignment mode ("chance", "troll", "no_troll").
            In "chance" mode the troll count is a deterministic quota of
            ``round(count * TROLL_USER_CHANCE)`` personas — the LLM never
            decides troll-ness.

    Returns:
        Dictionary containing `users` (list of user dicts), `agents` (list of
        agent dicts) and `skipped` (personas not created because their batch
        kept failing). Batches are retried and then skipped; a run only
        raises when no persona could be created at all.
    """
    if not isinstance(count, int) or count < 1 or count > MAX_PERSONAS_COUNT:
        raise ValueError(f"Count must be an integer between 1 and {MAX_PERSONAS_COUNT}")

    if tier not in ("lurker", "regular", "power_user"):
        raise ValueError(
            f"Invalid tier '{tier}'. Must be one of: lurker, regular, power_user"
        )

    if troll_mode not in ("chance", "troll", "no_troll"):
        raise ValueError(
            f"Invalid troll_mode '{troll_mode}'. Must be one of: "
            "chance, troll, no_troll"
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

    if troll_mode == "troll":
        troll_count = count
    elif troll_mode == "no_troll":
        troll_count = 0
    else:
        raw_chance = Config.get("TROLL_USER_CHANCE")
        try:
            chance = float(raw_chance or "0.1")
            if not 0 <= chance <= 1:
                raise ValueError
        except (TypeError, ValueError):
            logger.warning(
                "Invalid TROLL_USER_CHANCE %r; falling back to 0.1", raw_chance
            )
            chance = 0.1
        chance = max(0.0, min(1.0, chance))
        # Deterministic quota, round-half-up: 200 personas at 10% -> exactly
        # 20 trolls, no Bernoulli noise.
        troll_count = min(count, int(count * chance + 0.5))

    seen_usernames: set[str] = set()
    created_users: list[User] = []
    created_agents: list[Agent] = []
    client = LLMClient()

    def _request_batch(batch_target: int, is_troll: bool) -> int:
        """Generate one homogeneous batch, retrying failed LLM attempts.

        Returns the number of personas created (may be less than
        ``batch_target`` when attempts run out mid-batch). Raises
        PersonaGenerationError only when every attempt failed to produce
        a single persona.
        """
        batch_created = 0
        last_error: Exception | None = None
        for attempt in range(1, PERSONA_BATCH_ATTEMPTS + 1):
            remaining = batch_target - batch_created
            if remaining <= 0:
                break
            user_prompt = USER_PROMPT_TEMPLATE.format(
                count=remaining,
                topic_section=topic_section,
                troll_section=TROLL_SECTION if is_troll else "",
                style_assignments="\n".join(
                    f"Persona {i + 1} username style: {style}"
                    for i, style in enumerate(_assign_styles(remaining))
                ),
            )

            req = ChatRequest(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_prompt,
                model=model,
                api_url=api_url,
                api_key=api_key,
                sampling=Sampling(max_tokens=16384, temperature=0.8),
            )

            try:
                result = client.complete(req)
                raw_personas = _extract_json(result.content)
            except (LLMError, PersonaGenerationError) as exc:
                last_error = exc
                logger.warning(
                    "Persona batch attempt %d/%d failed: %s",
                    attempt,
                    PERSONA_BATCH_ATTEMPTS,
                    exc,
                )
                continue

            if not raw_personas:
                last_error = PersonaGenerationError("LLM returned empty personas list")
                logger.warning(
                    "Persona batch attempt %d/%d returned no personas",
                    attempt,
                    PERSONA_BATCH_ATTEMPTS,
                )
                continue

            for raw_p in raw_personas:
                if batch_created >= batch_target:
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
                    is_troll=is_troll,
                )
                created_users.append(user)
                batch_created += 1

                if auto_create_agent:
                    agent_config = {
                        "max_actions_per_run": 30,
                        "min_delay": 300,
                        "max_delay": 1800,
                        "api_url": api_url,
                        "model": model,
                    }
                    agent = Agent(
                        persona_mode="fixed",
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

        if batch_created == 0:
            raise PersonaGenerationError(
                f"Batch of {batch_target} personas failed after "
                f"{PERSONA_BATCH_ATTEMPTS} attempts: {last_error}"
            )
        return batch_created

    plan = _batch_plan(troll_count, count - troll_count)
    last_batch_error: PersonaGenerationError | None = None
    for batch_is_troll, batch_target in plan:
        try:
            _request_batch(batch_target, batch_is_troll)
        except PersonaGenerationError as exc:
            last_batch_error = exc
            logger.warning(
                "Skipping batch of %d personas after repeated failures",
                batch_target,
            )

    if not created_users and last_batch_error is not None:
        raise last_batch_error

    return {
        "users": [_user_to_dict(u) for u in created_users],
        "agents": [_agent_to_dict(a) for a in created_agents],
        "skipped": count - len(created_users),
    }
