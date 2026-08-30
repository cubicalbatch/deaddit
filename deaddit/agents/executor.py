"""Central agent tool executor with guardrails (slice S2).

Pipeline per :func:`execute`: unknown-tool check -> tier gate -> argument
validation -> rate caps -> duplicate suppression -> loop detection ->
handler dispatch. Guardrail rejections are RESULTS (``ok: False`` dicts),
never exceptions; exactly one :class:`~deaddit.models.ToolCall` row is
persisted per recognized call regardless of outcome.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta

from pydantic import BaseModel, ValidationError

from deaddit.agents.registry import (
    BACKSTAGE_SUBDEADDIT_NAME,
    POST_TOOL_NAMES,
    AutonomyTier,
    ToolContext,
    effective_post_configs,
    offered_post_tool_names,
    parse_tier,
)
from deaddit.agents.registry import (
    get as get_tool,
)
from deaddit.extensions import db
from deaddit.llm import SchemaValidationError, ToolSpec, validate_tool_args
from deaddit.models import AgentRun, AgentTurn, Comment, Post, ToolCall, User

__all__ = ["ExecutorError", "execute", "normalize_persona_rate_caps"]


class ExecutorError(Exception):
    """Infrastructure failure only; guardrail rejections are results."""


RATE_CAPS: dict[str, tuple[int, timedelta]] = {
    "create_post": (2, timedelta(hours=1)),
    "create_image_post": (2, timedelta(hours=1)),
    "create_website": (2, timedelta(hours=1)),
    "create_comment": (12, timedelta(hours=1)),
}

_RATE_CAP_MESSAGES = {
    "create_post": "you've posted a lot recently; try again later",
    "create_image_post": "you've posted a lot recently; try again later",
    "create_website": "you've posted a lot recently; try again later",
    "create_comment": "you've commented a lot recently; try again later",
}

#: Budget buckets for per-persona cap overrides: the three post tools
#: publish from one shared bucket (plan 4B), so an override keys the
#: bucket, never an individual tool - otherwise create_post/
#: create_image_post/create_website would each grow its own ceiling
#: against the same shared count.
RATE_CAP_BUCKETS: dict[str, str] = {
    "create_post": "post",
    "create_image_post": "post",
    "create_website": "post",
    "create_comment": "comment",
}


def normalize_persona_rate_caps(raw: object, *, strict: bool = False) -> dict[str, int]:
    """Normalize a persona's per-hour rate-cap overrides.

    Single source of truth for both readers: the executor calls this
    leniently - anything malformed sitting in ``User.agent_state`` is
    dropped, never fatal - while the admin user API calls it with
    ``strict=True`` so bad input is rejected with :class:`ValueError`
    instead of being stored. Valid shape is any subset of
    ``{"post": n, "comment": n, "vote": n}`` with whole numbers >= 0;
    ``0`` deliberately disables that action for the persona. The vote
    override is consumed by the simulated-voting engine, not this executor.
    """
    if not isinstance(raw, dict):
        if strict:
            raise ValueError("rate_caps must be an object")
        return {}
    buckets = {"post", "comment", "vote"}
    overrides: dict[str, int] = {}
    problems: list[str] = []
    for key, value in raw.items():
        if key not in buckets:
            problems.append(f"unknown cap {key!r}")
            continue
        if value is None:
            continue  # explicit "no override" (the admin UI sends null for blank)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            problems.append(f"cap {key!r} must be a whole number >= 0")
            continue
        overrides[key] = value
    if strict and problems:
        raise ValueError("; ".join(problems))
    return overrides


_MAX_RESULT_CHARS = 4096
_LOOP_WINDOW = 8
_DUP_SIMILARITY_THRESHOLD = 0.85
_DUP_LOOKBACK_HOURS = 48
_DUP_OWN_LIMIT = 20
#: Normalized content shorter than this skips the duplicate check: real
#: users repeat short reactions ("lol", "this", "nice one") across threads
#: and days. In-run repetition is still caught by loop detection and the
#: per-run rate caps.
_DUP_MIN_CANDIDATE_LEN = 25

_llm_spec_cache: dict[str, ToolSpec] = {}


def _llm_spec(tool) -> ToolSpec:
    """Adapt a registry Tool to the LLM layer's ToolSpec (cached by name)."""
    spec = _llm_spec_cache.get(tool.name)
    if spec is None:
        spec = ToolSpec(tool.name, tool.description, tool.parameters)
        _llm_spec_cache[tool.name] = spec
    return spec


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize(text: str) -> str:
    """Lowercase, strip non-alphanumerics to spaces, collapse whitespace."""
    return " ".join(_NON_ALNUM.sub(" ", text.lower()).split())


def _trigrams(text: str) -> set[str]:
    return {text[i : i + 3] for i in range(len(text) - 2)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / (len(a | b))


def _signature(name: str, arguments: dict | None) -> str:
    payload = json.dumps(arguments or {}, sort_keys=True, default=str)
    return hashlib.sha256(f"{name}{payload}".encode()).hexdigest()


def _reject(error: str, hint: str | None = None) -> dict:
    rejection: dict = {"ok": False, "error": error, "kind": "rejected"}
    if hint is not None:
        rejection["hint"] = hint
    return rejection


def _check_tier(ctx: ToolContext, min_tier_value: str) -> str | None:
    try:
        agent_tier = parse_tier(ctx.agent.autonomy_tier)
    except ValueError as exc:
        return str(exc)
    if agent_tier.allows(min_tier_value):
        return None
    required = AutonomyTier(min_tier_value)
    return (
        f"tool requires the '{required.value}' tier or higher "
        f"(you are '{agent_tier.value}')"
    )


def _check_post_policy(name: str, ctx: ToolContext) -> str | None:
    """Authorize create_post/create_image_post/create_website against config.

    This runs regardless of what ``specs_for``/``tools_for`` offered the
    model, so a forged, stale, or hand-crafted tool call cannot bypass the
    agent's image/website post policy by skipping registry filtering (plan
    4B acceptance; create_website spec "Registry and executor"). It reuses
    :func:`offered_post_tool_names` with :func:`effective_post_configs`
    so enforcement can never drift from offering: a call the registry
    would never have offered is always rejected here too, including the
    invalid ``image_only`` + ``website_only`` combination that fails closed
    to no post tool at all.
    """
    if name not in POST_TOOL_NAMES:
        return None
    intent = getattr(ctx, "post_intent", "browse")
    image_cfg, website_cfg = effective_post_configs(ctx.agent, intent)
    if name in offered_post_tool_names(image_cfg, website_cfg):
        return None
    if name == "create_image_post":
        return "image posts are not enabled for this agent"
    if name == "create_website":
        return "website posts are not enabled for this agent"
    # create_post: excluded either because an image_only/website_only
    # policy forbids the plain-text fallback, or because both forced
    # policies conflict and the agent has no post tool available at all.
    if image_cfg["enabled"] and image_cfg["policy"] == "image_only":
        return "this agent may only publish image posts, not text posts"
    if website_cfg["enabled"] and website_cfg["policy"] == "website_only":
        return "this agent may only publish website posts, not text posts"
    return "no post tool is available for this agent's configuration"


def _post_destination(name: str, validated: dict) -> str | None:
    if name == "create_post":
        return validated.get("subdeaddit")
    if name in ("create_image_post", "create_website"):
        return validated.get("community")
    return None


def _check_target_subdeaddit(
    name: str, ctx: ToolContext, validated: dict
) -> str | None:
    target = ctx.target_subdeaddit
    destination = _post_destination(name, validated)
    if target is None or destination is None or destination == target:
        return None
    return (
        f"this visit is reserved for d/{target}; "
        f"you cannot publish the post in d/{destination}"
    )


def _check_backstage_rotation(
    name: str, ctx: ToolContext, validated: dict
) -> str | None:
    """Keep one persona from opening consecutive backstage threads."""
    if _post_destination(name, validated) != BACKSTAGE_SUBDEADDIT_NAME:
        return None
    latest = (
        Post.query.filter(
            Post.subdeaddit_name == BACKSTAGE_SUBDEADDIT_NAME,
            Post.removed.is_(False),
        )
        .order_by(Post.created_at.desc(), Post.id.desc())
        .first()
    )
    if latest is not None and latest.user == ctx.user_username:
        return "another persona must open the next d/BetweenRobots thread"
    return None


def _parse_raw_arguments(raw_arguments: dict | str) -> dict:
    """Best-effort parse of native tool_call argument JSON (parse once).

    No salvage: anything unparseable becomes ``{}``; the authoritative
    validation happens through ``validate_tool_args``.
    """
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}


def _persona_rate_cap(name: str, ctx: ToolContext) -> tuple[int, timedelta]:
    """Default cap for *name*, raised/lowered by the persona's override."""
    cap, window = RATE_CAPS[name]
    bucket = RATE_CAP_BUCKETS.get(name)
    if bucket is None:
        return cap, window
    persona = db.session.get(User, ctx.user_username)
    state = persona.agent_state if persona is not None else None
    raw = state.get("rate_caps") if isinstance(state, dict) else None
    override = normalize_persona_rate_caps(raw).get(bucket)
    if override is not None:
        cap = override
    return cap, window


def _check_rate_cap(name: str, ctx: ToolContext) -> str | None:
    if name not in RATE_CAPS:
        return None
    cap, window = _persona_rate_cap(name, ctx)
    window_start = datetime.utcnow() - window
    # Caps model how often one simulated human would act, so they key on
    # the run's persona - not on the Agent row, whose single bucket every
    # persona of a random-persona agent would otherwise collide on. The
    # three post tools still share one bucket within the persona.
    names = POST_TOOL_NAMES if name in POST_TOOL_NAMES else (name,)
    recent_count = (
        db.session.query(ToolCall.id)
        .join(AgentRun, AgentRun.id == ToolCall.run_id)
        .filter(
            AgentRun.persona_username == ctx.user_username,
            ToolCall.name.in_(names),
            ToolCall.ok.is_(True),  # rejected attempts don't consume the quota
            ToolCall.created_at >= window_start,
        )
        .count()
    )

    if recent_count >= cap:
        return _RATE_CAP_MESSAGES.get(
            name, f"you've used {name} a lot recently; try again later"
        )
    return None


def _check_duplicate(name: str, ctx: ToolContext, validated: dict) -> str | None:
    """Trigram-Jaccard similarity check against prior content."""
    if name == "create_post":
        candidate = _normalize(
            f"{validated.get('title', '')} {validated.get('content', '')}"
        )
        subdeaddit_name = validated.get("subdeaddit")
    elif name in ("create_image_post", "create_website"):
        candidate = _normalize(
            f"{validated.get('title', '')} {validated.get('content') or ''}"
        )
        subdeaddit_name = validated.get("community")
    elif name == "create_comment":
        subdeaddit_name = None
        # Short reactions ("lol", "this", "nice one") legitimately repeat
        # across threads and days; in-run repetition is still caught by
        # loop detection and rate caps.
        candidate = _normalize(validated.get("content", ""))
        if len(candidate) < _DUP_MIN_CANDIDATE_LEN:
            return None
    else:
        return None
    candidate_trigrams = _trigrams(candidate)

    own_posts = (
        Post.query.filter(Post.user == ctx.user_username)
        .order_by(Post.created_at.desc(), Post.id.desc())
        .limit(_DUP_OWN_LIMIT)
        .all()
    )
    own_comments = (
        Comment.query.filter(Comment.user == ctx.user_username)
        .order_by(Comment.created_at.desc(), Comment.id.desc())
        .limit(_DUP_OWN_LIMIT)
        .all()
    )
    prior_texts = [_normalize(f"{p.title} {p.content or ''}") for p in own_posts]
    prior_texts += [_normalize(c.content or "") for c in own_comments]

    if subdeaddit_name:
        cutoff = datetime.utcnow() - timedelta(hours=_DUP_LOOKBACK_HOURS)
        recent_titles = (
            Post.query.filter(
                Post.subdeaddit_name == subdeaddit_name,
                Post.created_at >= cutoff,
                Post.user != ctx.user_username,
            )
            .order_by(Post.created_at.desc())
            .limit(100)
            .all()
        )
        prior_texts += [_normalize(p.title) for p in recent_titles]

    for text in prior_texts:
        if not text:
            continue
        if _jaccard(candidate_trigrams, _trigrams(text)) >= (_DUP_SIMILARITY_THRESHOLD):
            return "too similar to your earlier content; write something new"
    return None


def _check_loop(
    ctx: ToolContext, name: str, validated: dict
) -> tuple[str | None, bool]:
    """Return ``(warning, force_finish)`` based on consecutive repeats."""
    signature = _signature(name, validated)
    rows = (
        ToolCall.query.filter_by(run_id=ctx.run.id)
        .order_by(ToolCall.id.desc())
        .limit(_LOOP_WINDOW)
        .all()
    )
    streak = 0
    for row in rows:
        if row.name != name or _signature(row.name, row.arguments) != signature:
            break
        streak += 1
    if streak >= 2:
        warning = None
        force_finish = True
    elif streak == 1:
        warning = "you are repeating the same action; vary your behaviour"
        force_finish = False
    else:
        warning = None
        force_finish = False
    return warning, force_finish


def _latest_turn_id(run_id: int) -> int | None:
    row = (
        db.session.query(AgentTurn.id)
        .filter(AgentTurn.run_id == run_id)
        .order_by(AgentTurn.seq.desc())
        .first()
    )
    return row[0] if row else None


def _shorten_strings(value: object, max_len: int) -> object:
    """Recursively shorten long strings inside a JSON-safe structure."""
    if isinstance(value, str) and len(value) > max_len:
        return value[:max_len] + "…[truncated]"
    if isinstance(value, dict):
        return {k: _shorten_strings(v, max_len) for k, v in value.items()}
    if isinstance(value, list):
        return [_shorten_strings(v, max_len) for v in value]
    return value


def _truncate_result(result: dict) -> dict:
    """Shrink a result for storage while keeping it parseable JSON.

    Long string values are shortened first (structure preserved); if the
    serialized form still exceeds the cap, fall back to a preview wrapper.
    The full result is still returned to the model — this is audit-only.
    """
    if len(json.dumps(result, default=str)) <= _MAX_RESULT_CHARS:
        return result
    max_len = 512
    while max_len >= 32:
        trimmed = _shorten_strings(result, max_len)
        if len(json.dumps(trimmed, default=str)) <= _MAX_RESULT_CHARS:
            return trimmed
        max_len //= 2
    serialized = json.dumps(result, default=str)
    return {"truncated": True, "preview": serialized[: _MAX_RESULT_CHARS - 96]}


def _persist_and_return(
    ctx: ToolContext,
    name: str,
    arguments: dict,
    result: dict,
    started_monotonic: float,
) -> dict:
    duration_ms = int((time.monotonic() - started_monotonic) * 1000)
    stored = _truncate_result(result)
    db.session.add(
        ToolCall(
            turn_id=_latest_turn_id(ctx.run.id),
            run_id=ctx.run.id,
            name=name,
            arguments=arguments,
            result=stored,
            ok=bool(result.get("ok")),
            error=None if result.get("ok") else result.get("error"),
            duration_ms=duration_ms,
            created_at=datetime.utcnow(),
        )
    )
    db.session.commit()
    return result


def execute(name: str, raw_arguments: dict | str, ctx: ToolContext) -> dict:
    """Run one tool call under all guardrails. See module docstring."""
    started = time.monotonic()

    try:
        tool = get_tool(name)
    except KeyError:
        result = _reject(f"unknown tool '{name}'")
        # The retired vote action is deliberately not audited as a tool call:
        # it is no longer an executor surface, and its activity belongs to
        # the simulator's Vote rows instead.
        if name == "vote":
            return result
        return _persist_and_return(
            ctx,
            name,
            {},
            result,
            started,
        )

    raw_arguments_dict = _parse_raw_arguments(raw_arguments)

    # Tier gate.
    tier_problem = _check_tier(ctx, tool.min_tier.value)
    if tier_problem is not None:
        return _persist_and_return(
            ctx,
            name,
            raw_arguments_dict,
            _reject(tier_problem, hint="pick a tool within your tier"),
            started,
        )

    # Post-tool policy gate: independent of whether specs_for offered this
    # tool, so a direct executor call cannot bypass the agent's image/website
    # post configuration.
    policy_problem = _check_post_policy(name, ctx)
    if policy_problem is not None:
        return _persist_and_return(
            ctx,
            name,
            raw_arguments_dict,
            _reject(
                policy_problem,
                hint="check your agent's image-post/website-post configuration",
            ),
            started,
        )

    # Reserved post visits do not expose comments, and direct executor calls
    # cannot bypass that focus.
    intent = getattr(ctx, "post_intent", "browse")
    if intent in ("image", "website", "backstage") and name == "create_comment":
        return _persist_and_return(
            ctx,
            name,
            raw_arguments_dict,
            _reject(
                f"comments are not available during a reserved {intent} post visit",
                hint="focus on your post or use finish to conclude your visit",
            ),
            started,
        )

    # Argument validation via the shared LLM-layer validator. The registry's
    # Tool dataclass is not an llm.ToolSpec, so wrap it (cached per tool).
    try:
        validated = validate_tool_args(_llm_spec(tool), raw_arguments)
    except SchemaValidationError as exc:
        hint = str(exc)
        cause = exc.__cause__
        if isinstance(cause, ValidationError) and cause.errors():
            first = cause.errors()[0]
            loc = ".".join(str(part) for part in first["loc"])
            hint = f"{loc}: {first['msg']}"
        return _persist_and_return(
            ctx,
            name,
            raw_arguments_dict,
            _reject(f"invalid arguments for '{name}'", hint=hint),
            started,
        )

    target_problem = _check_target_subdeaddit(name, ctx, validated)
    if target_problem is not None:
        return _persist_and_return(
            ctx,
            name,
            validated,
            _reject(
                target_problem,
                hint=f"publish this post in d/{ctx.target_subdeaddit}",
            ),
            started,
        )

    rotation_problem = _check_backstage_rotation(name, ctx, validated)
    if rotation_problem is not None:
        return _persist_and_return(
            ctx,
            name,
            validated,
            _reject(rotation_problem, hint="finish this visit without posting"),
            started,
        )

    # Rate caps (reads uncapped).
    rate_problem = _check_rate_cap(name, ctx)
    if rate_problem is not None:
        return _persist_and_return(
            ctx,
            name,
            validated,
            _reject(rate_problem, hint="your limits reset over time"),
            started,
        )

    # Duplicate suppression for content-creating tools.
    dup_problem = _check_duplicate(name, ctx, validated)
    if dup_problem is not None:
        return _persist_and_return(
            ctx,
            name,
            validated,
            _reject(dup_problem),
            started,
        )

    # Loop detection over this run's recent calls.
    loop_warning, force_finish = _check_loop(ctx, name, validated)
    if force_finish:
        return _persist_and_return(
            ctx,
            name,
            validated,
            {
                "ok": False,
                "error": "repeating the same action",
                "hint": "try something different, or use finish to end your run",
                "force_finish": True,
            },
            started,
        )

    # Dispatch to the handler with a validated params instance.
    params_instance: BaseModel = tool.parameters.model_validate(validated)
    try:
        result = tool.handler(ctx, params_instance)
    except ValueError as exc:
        result = {"ok": False, "error": str(exc)}

    final = dict(result)
    if "ok" not in final:
        final = {"ok": True, **final}
    if loop_warning is not None:
        final["warning"] = loop_warning
    return _persist_and_return(ctx, name, validated, final, started)
