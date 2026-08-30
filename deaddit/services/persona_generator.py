"""Persona generation service for Deaddit.

Generates structured, human-like user personas using the configured LLM endpoint,
persists them via `create_user()`, and optionally enrolls them as autonomous agents.

Diversity is planned in Python: one assignment matrix per request comes from
`persona_options.build_persona_assignments`, the prompt renders only the
still-unresolved rows, and returned personas are resolved by `assignment_id`.
"""

from __future__ import annotations

import json
import logging
import random
import re
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any

from deaddit.config import Config
from deaddit.extensions import db
from deaddit.llm import ChatRequest, LLMClient, LLMError, Sampling, routing
from deaddit.models import Agent, Subdeaddit, User
from deaddit.services.content import create_user
from deaddit.services.persona_options import (
    USERNAME_STYLES,
    ExistingUserSnapshot,
    PersonaAssignment,
    build_persona_assignments,
)

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
    "Generate {count} unique human-like user personas for the forum."
    "{topic_section}{communities_section}{troll_section}\n\n"
    "Demographics and voice are pre-planned. The assignment matrix below\n"
    "gives each persona fixed facts:\n"
    "- Rows are not interchangeable: never swap, merge, reorder, or drop\n"
    "  row facts, and never mention assignments in the output.\n"
    "- Assigned facts are facts: use each row's exact age, occupation,\n"
    "  employment context, education, required traits, writing style, and\n"
    "  username style for that persona and no other.\n"
    "- Bios must read like one coherent human life, never a job summary.\n"
    "- Never infer gender from profession, education, age, or interests.\n"
    "- Never copy any example username, phrase, or name that appears in\n"
    "  this prompt; invent fresh ones.\n\n"
    "Assignment matrix:\n"
    "{matrix}\n\n"
    f"Username rules: {USERNAME_STYLE_RULES}\n\n"
    "Each persona object must have the following fields:\n"
    '- "assignment_id": string, copied exactly from that persona\'s matrix row\n'
    '- "username": string following the username style in that persona\'s row\n'
    '- "bio": string (authentic personal bio, 1-3 sentences)\n'
    '- "age": integer, exactly the age in that persona\'s row\n'
    '- "gender": string ("Male" or "Female")\n'
    '- "occupation": string, exactly the occupation in that persona\'s row\n'
    '- "education": string, exactly the education text in that persona\'s row\n'
    '- "interests": list of strings (3 to 6 specific interests or hobbies;\n'
    "  must include that row's interest seeds)\n"
    '- "personality_traits": list of strings (3 to 6 descriptive traits;\n'
    "  must include all of that row's required traits)\n"
    '- "writing_style": string (description of their online writing tone,\n'
    "  following that row's writing style)\n"
    "Output JSON only: a valid array with exactly one object per matrix\n"
    "row, each carrying its own assignment_id. No markdown codeblocks,\n"
    "no conversational filler, no extra fields."
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

#: Hard cap on LLM-picked subscriptions per persona: the prompt asks for
#: exactly 2, validation accepts up to this many, and anything the LLM
#: adds beyond it is dropped rather than erroring the whole persona.
MAX_PERSONA_SUBSCRIPTIONS = 3


def _communities_section(sub_rows: list[tuple[str, str]]) -> str:
    """Prompt block listing real communities and requesting subscriptions.

    Empty when the database has no subdeaddits (the field simply is not
    requested then). Names and descriptions come from the database, never
    from a hardcoded list - the persona generator must not suggest
    communities that do not exist.
    """
    if not sub_rows:
        return ""
    listing = "\n".join(
        f"- {name}: {' '.join((description or '').split())[:110]}"
        for name, description in sub_rows
    )
    return (
        "\nThe forum currently has these communities (name: description):\n"
        f"{listing}\n\n"
        "Each persona object must also include:\n"
        '- "subscriptions": list of exactly 2 community names, copied '
        "verbatim from the list above, where this persona would genuinely "
        "spend time given their interests and personality - a "
        "general-purpose community is the honest pick for a "
        "general-interest person. Never invent or guess community names "
        'outside the list. Example: "subscriptions": ["books", '
        '"CasualConversation"]\n'
    )


def _matrix_row(index: int, assignment: PersonaAssignment, is_troll: bool) -> str:
    """Render one numbered assignment row for the prompt matrix."""
    lines = [
        f"Persona {index} [assignment_id {assignment.id}]:",
        f"- age: {assignment.age}",
        f"- occupation: {assignment.occupation} ({assignment.employment_context})",
        f'- education: "{assignment.education}"',
        f"- required traits: {'; '.join(assignment.traits)}",
        f'- writing style: "{assignment.writing_style}"',
        f"- interest seeds: {'; '.join(assignment.interest_seeds)}",
        f"- username style: {assignment.username_style}",
    ]
    if is_troll and assignment.troll_modifier:
        lines.append(f"- troll expression: {assignment.troll_modifier}")
    return "\n".join(lines)


def _build_user_prompt(
    assignments: list[PersonaAssignment],
    *,
    topic_section: str,
    communities_section: str,
    is_troll: bool,
) -> str:
    """Render the user prompt for exactly the given assignment rows.

    Only the rows passed in (the still-unresolved ones, on retries) enter
    the matrix, so the model is never re-sent an already-created persona
    and no unassigned catalog fact leaks into the prompt.
    """
    matrix = "\n\n".join(
        _matrix_row(index, assignment, is_troll)
        for index, assignment in enumerate(assignments, 1)
    )
    return USER_PROMPT_TEMPLATE.format(
        count=len(assignments),
        topic_section=topic_section,
        communities_section=communities_section,
        troll_section=TROLL_SECTION if is_troll else "",
        matrix=matrix,
    )


def _existing_snapshots() -> list[ExistingUserSnapshot]:
    """Snapshot the current population for deficit-aware planning.

    ``persona_seed`` provenance (Phase 3 persistence) is read when present;
    legacy users fall back to normalized label matching inside the planner.
    """
    rows = db.session.query(
        User.agent_state, User.age, User.occupation, User.education
    ).all()
    return [
        ExistingUserSnapshot(
            persona_seed=(state or {}).get("persona_seed"),
            age=age,
            occupation=occupation,
            education=education,
        )
        for state, age, occupation, education in rows
    ]


def _normalize_subscriptions(raw: object, valid_sub_names: set[str]) -> list[str]:
    """Validate LLM-picked subscriptions against real communities.

    Accepts a list or comma-separated string. Unknown names are dropped
    (a case-insensitive match resolves to the stored casing), duplicates
    collapse preserving order, and the result is capped at
    ``MAX_PERSONA_SUBSCRIPTIONS``. An empty result stays empty - a forced
    fallback subscription is how ghost communities get promoted.
    """
    if isinstance(raw, str):
        candidates = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, list):
        candidates = [str(item).strip() for item in raw]
    else:
        return []
    by_lower = {name.lower(): name for name in valid_sub_names}
    picked: list[str] = []
    for candidate in candidates:
        canonical = by_lower.get(candidate.lower()) if candidate else None
        if canonical is not None and canonical not in picked:
            picked.append(canonical)
    return picked[:MAX_PERSONA_SUBSCRIPTIONS]


class PersonaGenerationError(Exception):
    """Raised when persona generation or parsing fails."""


def _assign_styles(n: int, rng: random.Random | None = None) -> list[str]:
    """Assign one catalog username-style directive to each persona.

    Called once for a whole generation request - never per batch or
    retry - so every persona keeps its style through batch partitioning
    and retries. Delegates to the source-controlled catalog in
    ``persona_options``; the directives include short example handles
    that the prompt explicitly forbids copying.
    """
    drawer = rng if rng is not None else random
    return drawer.choices([style.text for style in USERNAME_STYLES], k=n)


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


def _sanitize_persona(
    item: dict, seen_usernames: set[str], valid_sub_names: set[str]
) -> dict:
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

    subscriptions = _normalize_subscriptions(item.get("subscriptions"), valid_sub_names)

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
        "subscriptions": subscriptions,
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
        "subscriptions": ((user.agent_state or {}).get("subscriptions") or []),
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

    One assignment matrix (age, occupation, education, required traits,
    writing style, interest seeds, username style, troll modifier) is
    planned in Python for the whole request before any LLM call. The model
    only synthesizes the planned rows into believable bios and voices, and
    returned rows are resolved by ``assignment_id``.

    Args:
        count: Number of personas to generate (1 to 500).
        topic_hint: Optional interest lens - it may flavor interests only,
            never the assigned demographics of the batch.
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
        f'\nInterest lens: some of these personas may have interests '
        f'connected to "{topic_hint.strip()}". The lens applies to '
        "interests only - never change any assigned age, occupation, "
        "education, traits, or writing style to match it, and keep most "
        "personas' interests independent of it.\n"
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

    sub_rows = (
        db.session.query(Subdeaddit.name, Subdeaddit.description)
        .order_by(Subdeaddit.name.asc())
        .all()
    )
    valid_sub_names = {name for name, _ in sub_rows}
    communities_section = _communities_section(sub_rows)

    # One assignment plan for the whole request: demographics, traits,
    # styles, and the troll/normal split are fixed before any batch call,
    # so batch partitioning and retries can never reroll a persona.
    rng = random.Random()
    assignments = build_persona_assignments(
        count, troll_count, _existing_snapshots(), rng
    )
    styles = _assign_styles(count, rng)
    assignments = tuple(
        replace(assignment, username_style=style)
        for assignment, style in zip(assignments, styles, strict=True)
    )
    normal_rows = [a for a in assignments if a.troll_modifier is None]
    troll_rows = [a for a in assignments if a.troll_modifier is not None]

    seen_usernames: set[str] = set()
    created_users: list[User] = []
    created_agents: list[Agent] = []
    client = LLMClient()

    def _request_batch(
        batch_assignments: list[PersonaAssignment], is_troll: bool
    ) -> int:
        """Generate one homogeneous batch, retrying unresolved assignment IDs.

        Every attempt prompts for only the still-unresolved rows, and each
        returned row is matched to its assignment by ``assignment_id``;
        unknown, duplicate, or missing IDs are ignored - rows are never
        paired by position. Returns the number of personas created (may be
        less than requested when attempts run out mid-batch). Raises
        PersonaGenerationError only when every attempt failed to produce
        a single persona.
        """
        pending = list(batch_assignments)
        batch_created = 0
        last_error: Exception | None = None
        for attempt in range(1, PERSONA_BATCH_ATTEMPTS + 1):
            if not pending:
                break
            user_prompt = _build_user_prompt(
                pending,
                topic_section=topic_section,
                communities_section=communities_section,
                is_troll=is_troll,
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

            by_id = {assignment.id: assignment for assignment in pending}
            resolved: set[str] = set()
            for raw_p in raw_personas:
                assignment = by_id.get(str(raw_p.get("assignment_id") or "").strip())
                if assignment is None or assignment.id in resolved:
                    # Unknown, duplicate, or absent assignment IDs are
                    # never mapped by position; the row stays pending and
                    # is prompted for again on the next attempt.
                    continue
                resolved.add(assignment.id)
                p = _sanitize_persona(raw_p, seen_usernames, valid_sub_names)
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
                if p["subscriptions"]:
                    # LLM-picked initial subscriptions ride the existing
                    # agent_state["subscriptions"] machinery (feed bias,
                    # system prompt, subscribe/unsubscribe tools). Never
                    # forced: empty stays empty.
                    user.agent_state = {"subscriptions": p["subscriptions"]}
                    if not auto_create_agent:
                        db.session.commit()
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

            pending = [
                assignment for assignment in pending if assignment.id not in resolved
            ]
            if created_agents:
                db.session.commit()

        if batch_created == 0:
            raise PersonaGenerationError(
                f"Batch of {len(batch_assignments)} personas failed after "
                f"{PERSONA_BATCH_ATTEMPTS} attempts: {last_error}"
            )
        return batch_created

    plan = _batch_plan(len(troll_rows), len(normal_rows))
    batches: list[tuple[bool, list[PersonaAssignment]]] = []
    next_normal = next_troll = 0
    for batch_is_troll, batch_size in plan:
        if batch_is_troll:
            batches.append((True, troll_rows[next_troll : next_troll + batch_size]))
            next_troll += batch_size
        else:
            batches.append((False, normal_rows[next_normal : next_normal + batch_size]))
            next_normal += batch_size

    last_batch_error: PersonaGenerationError | None = None
    for batch_is_troll, batch_rows in batches:
        try:
            _request_batch(batch_rows, batch_is_troll)
        except PersonaGenerationError as exc:
            last_batch_error = exc
            logger.warning(
                "Skipping batch of %d personas after repeated failures",
                len(batch_rows),
            )

    if not created_users and last_batch_error is not None:
        raise last_batch_error

    return {
        "users": [_user_to_dict(u) for u in created_users],
        "agents": [_agent_to_dict(a) for a in created_agents],
        "skipped": count - len(created_users),
    }
