"""Single-run agent loop.

Consumes native ``tool_calls`` from ``ChatResult.tool_calls`` only - free-text
JSON parsing is never used. Nothing here schedules anything: importing this
module registers no jobs or threads (decision 1). Manual ``run-once``
invocation is always allowed.
"""

import json
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError

from deaddit import Config
from deaddit.agents.executor import execute
from deaddit.agents.memory import ensure_lazy_backfill, summarize_run
from deaddit.agents.prompts import prepare_agent_visit
from deaddit.agents.registry import ToolContext
from deaddit.extensions import db
from deaddit.images.types import Deadline
from deaddit.llm import (
    ChatRequest,
    LLMClient,
    PermanentLLMError,
    Sampling,
)
from deaddit.llm.prompts import serialize_visit_profile
from deaddit.models import Agent, AgentRun, AgentTurn, LLMProvider, Setting, User
from deaddit.runtime.lull import scaled_wake_delay

logger = logging.getLogger(__name__)

# Budget defaults applied when absent from agent.config.
DEFAULT_CONFIG: dict[str, Any] = {
    "max_actions_per_run": 30,
    "max_run_seconds": 300,
    "min_delay": 60,
    "max_delay": 900,
}

USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")

NUDGE_MESSAGE = "Use a tool to act, or call finish."

CONSECUTIVE_FAILURE_DISABLE_THRESHOLD = 5

FAILURE_BACKOFF_SECONDS = 300

RESERVATION_ATTEMPTS = 5


def is_runtime_enabled() -> bool:
    """Read the AGENT_RUNTIME_ENABLED feature flag (Setting row, default false).

    The wake scheduler consults this flag before polling for due agents.
    Explicit manual invocation is always allowed regardless of the flag.
    """
    value = Setting.get_value("AGENT_RUNTIME_ENABLED", "false")
    return str(value or "false").strip().lower() == "true"


def _int_budget(config: dict[str, Any], key: str) -> int:
    try:
        return int(config.get(key, DEFAULT_CONFIG[key]))
    except (TypeError, ValueError):
        return int(DEFAULT_CONFIG[key])


def _current_run(run_id: int) -> AgentRun | None:
    """Load run ownership from the database, not this worker's identity map."""
    db.session.expire_all()
    return (
        db.session.query(AgentRun)
        .populate_existing()
        .filter(AgentRun.id == run_id)
        .one_or_none()
    )


def _run_is_active(run_id: int) -> bool:
    return (
        db.session.query(AgentRun.status).filter(AgentRun.id == run_id).scalar()
        == "running"
    )


def _recover_stale_runs(agent: Agent) -> bool:
    """Mark runs stuck past max_run_seconds + 60s grace as 'interrupted'.
    Returns True when a genuinely live run still exists (agent must be refused).
    Stale-running recovery runs per-agent in this loop and per-tick in the wake
    scheduler.
    """
    now = datetime.utcnow()
    grace = timedelta(
        seconds=_int_budget(_effective_config(agent), "max_run_seconds") + 60
    )
    stuck_ids = [
        run_id
        for (run_id,) in (
            db.session.query(AgentRun.id)
            .filter_by(agent_id=agent.id, status="running")
            .filter(AgentRun.started_at < now - grace)
            .all()
        )
    ]
    interrupted = 0
    for run_id in stuck_ids:
        updated = db.session.query(AgentRun).filter(
            AgentRun.id == run_id,
            AgentRun.status == "running",
        ).update(
            {
                AgentRun.status: "interrupted",
                AgentRun.finished_at: now,
                AgentRun.error_message: (
                    "Recovered: run exceeded wall-clock budget plus grace."
                ),
            },
            synchronize_session=False,
        )
        interrupted += updated
        if updated:
            db.session.query(Agent).filter(
                Agent.id == agent.id, Agent.status == "running"
            ).update({Agent.status: "idle"}, synchronize_session=False)
    if interrupted:
        db.session.commit()
    return (
        db.session.query(AgentRun)
        .filter_by(agent_id=agent.id, status="running")
        .first()
        is not None
    )


def _effective_config(agent: Agent) -> dict[str, Any]:
    return {**DEFAULT_CONFIG, **(agent.config or {})}


def resolve_agent_llm(agent: Agent) -> tuple[LLMProvider | None, str, str]:
    """Resolve the ``(provider, api_url, model)`` an agent visit would use.

    Precedence: agent.config ``provider_id``/``api_url``/``model``, then the
    provider's defaults, then global Config. Shared by the run loop and the
    admin agents list so both show the same effective selection.
    """
    config = _effective_config(agent)
    provider = None
    if config.get("provider_id"):
        try:
            provider = db.session.get(LLMProvider, int(config["provider_id"]))
        except Exception:
            provider = None
    if provider is None and config.get("api_url"):
        try:
            provider = LLMProvider.query.filter(
                (LLMProvider.api_url == str(config["api_url"]).rstrip("/"))
                | (LLMProvider.api_url == str(config["api_url"]))
            ).first()
        except Exception:
            provider = None
    if provider is None:
        try:
            provider = LLMProvider.get_default()
        except Exception:
            provider = None

    if provider:
        api_url = config.get("api_url") or provider.api_url
        model = (
            config.get("model")
            or provider.default_model
            or Config.get("OPENAI_MODEL", "llama3")
        )
    else:
        api_url = config.get("api_url") or Config.get("OPENAI_API_URL")
        model = config.get("model") or Config.get("OPENAI_MODEL", "llama3")
    return provider, api_url, model


def _previous_persona(agent: Agent) -> str | None:
    run = (
        AgentRun.query.filter_by(agent_id=agent.id).order_by(AgentRun.id.desc()).first()
    )
    return run.persona_username if run is not None else None


def _eligible_personas(agent: Agent) -> list[str]:
    fixed = {
        row[0]
        for row in db.session.query(Agent.user_username).filter(
            Agent.user_username.isnot(None)
        )
    }
    running = {
        row[0]
        for row in db.session.query(AgentRun.persona_username).filter(
            AgentRun.status == "running"
        )
    }
    pool = [
        row[0]
        for row in db.session.query(User.username).order_by(User.username)
        if row[0] not in fixed and row[0] not in running
    ]
    previous = _previous_persona(agent)
    if previous in pool and len(pool) > 1:
        pool.remove(previous)
    return pool


def _select_persona(agent: Agent) -> str:
    if agent.persona_mode != "random":
        user = db.session.get(User, agent.user_username)
        if user is None:
            raise ValueError(
                f"Fixed agent {agent.id} has no user '{agent.user_username}'"
            )
        return agent.user_username
    pool = _eligible_personas(agent)
    if not pool:
        raise ValueError(f"No eligible persona available for random agent {agent.id}")
    return _pick_lru_persona(pool)


def _pick_lru_persona(pool: list[str]) -> str:
    """Pick a persona using least-recently-used bias to flatten comment volume.

    Sorts the pool by each persona's most recent AgentRun.started_at
    (never-used personas first), then picks randomly from the least
    recently used window. This prevents any small subset of users from
    accumulating a disproportionate share of comments over time.
    """
    last_used: dict[str, datetime] = {}
    rows = (
        db.session.query(AgentRun.persona_username, AgentRun.started_at)
        .filter(AgentRun.persona_username.in_(pool))
        .order_by(AgentRun.started_at.desc())
        .all()
    )
    for username, started_at in rows:
        if username not in last_used and started_at is not None:
            last_used[username] = started_at

    never_used = [u for u in pool if u not in last_used]
    if never_used:
        return random.choice(never_used)

    # Sort by most recent run time (oldest first = least recently used)
    sorted_pool = sorted(pool, key=lambda u: last_used.get(u, datetime.min))
    # Pick from the least-recently-used ~20% (min 3) to keep variety
    window = max(3, len(sorted_pool) // 5)
    window = min(window, len(sorted_pool))
    return random.choice(sorted_pool[:window])


def reserve_persona_run(agent: Agent, *, trigger: str) -> AgentRun:
    """Own persona eligibility and run reservation for every agent run.

    Admin, CLI, and worker callers must reach persona selection only through
    ``run_once`` and this helper. IntegrityError retries handle conflicts from
    the partial unique index ``uq_agent_run_running_persona``.
    """
    for _ in range(RESERVATION_ATTEMPTS):
        persona = _select_persona(agent)
        run = AgentRun(
            agent_id=agent.id,
            persona_username=persona,
            trigger=trigger,
            status="running",
            started_at=datetime.utcnow(),
            turn_count=0,
            action_count=0,
            token_usage={},
        )
        agent.status = "running"
        db.session.add(run)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            continue
        logger.info(
            "Agent %s reserved persona '%s' (run %s)",
            agent.id,
            run.persona_username,
            run.id,
        )
        return run
    raise ValueError(
        f"Could not reserve a persona for agent {agent.id} "
        f"after {RESERVATION_ATTEMPTS} attempts"
    )


def _backoff_without_strike(agent: Agent) -> None:
    """Back off scheduled pool exhaustion without a permanent-LLM failure strike."""
    now = datetime.utcnow()
    agent.status = "error"
    agent.last_run_at = now
    if agent.is_enabled:
        agent.next_run_at = now + timedelta(seconds=FAILURE_BACKOFF_SECONDS)
    db.session.commit()


def _fail(
    agent: Agent,
    run: AgentRun,
    turn_count: int,
    action_count: int,
    usage: dict,
    message: str,
    *,
    strike: bool,
) -> AgentRun:
    """Close a failed run without stealing a replacement's ownership."""
    now = datetime.utcnow()
    db.session.rollback()
    updated = db.session.query(AgentRun).filter(
        AgentRun.id == run.id,
        AgentRun.status == "running",
    ).update(
        {
            AgentRun.status: "failed",
            AgentRun.finished_at: now,
            AgentRun.turn_count: turn_count,
            AgentRun.action_count: action_count,
            AgentRun.token_usage: usage,
            AgentRun.error_message: message[:2000],
        },
        synchronize_session=False,
    )
    if not updated:
        db.session.rollback()
        return _current_run(run.id) or run

    current_agent = (
        db.session.query(Agent)
        .populate_existing()
        .filter(Agent.id == agent.id)
        .one_or_none()
    )
    if current_agent is not None and current_agent.status == "running":
        current_agent.status = "error"
        current_agent.last_run_at = now
        if current_agent.is_enabled:
            # A failed run must never leave next_run_at in the past, or the
            # scheduler re-fires immediately against a possibly-dead endpoint.
            current_agent.next_run_at = now + timedelta(seconds=FAILURE_BACKOFF_SECONDS)
        if strike:
            current_agent.consecutive_failures = (
                current_agent.consecutive_failures or 0
            ) + 1
            if (
                current_agent.consecutive_failures
                >= CONSECUTIVE_FAILURE_DISABLE_THRESHOLD
            ):
                current_agent.is_enabled = False
                current_agent.status = "disabled"
                current_agent.next_run_at = None
    db.session.commit()
    return _current_run(run.id) or run


def run_once(
    agent_id: int,
    *,
    trigger: str = "manual",
    requested_intent: str | None = None,
) -> AgentRun:
    """Run one full agent visit synchronously. Caller provides the app context."""
    req = requested_intent
    agent = db.session.get(Agent, agent_id)
    if agent is None:
        raise ValueError(f"No agent with id {agent_id}")

    if _recover_stale_runs(agent):
        raise ValueError(f"Agent {agent.id} already has a run in progress")

    config = _effective_config(agent)
    provider, api_url, model = resolve_agent_llm(agent)
    api_key = (
        provider.api_key.strip()
        if (provider and provider.api_key and provider.api_key.strip())
        else Config.get_api_key_for_endpoint(api_url)
    )

    try:
        run = reserve_persona_run(agent, trigger=trigger)
    except ValueError:
        if trigger == "schedule":
            _backoff_without_strike(agent)
        raise

    usage: dict[str, int] = dict.fromkeys(USAGE_KEYS, 0)
    try:
        user = db.session.get(User, run.persona_username)
        ensure_lazy_backfill(agent, user)
        visit = prepare_agent_visit(agent, user, requested_intent=req)
        messages = visit.messages
        run.intent = visit.plan.intent
        run.prompt_metadata = {
            "schema_version": 1,
            "profile": {
                "name": visit.plan.profile_name,
                "version": visit.plan.profile_version,
                "ref": visit.plan.profile_ref,
                "resolution_source": visit.plan.resolution_source,
                "body": serialize_visit_profile(
                    # The immutable profile body is carried in render metadata.
                    # ``prepare_agent_visit`` keeps this source on the plan.
                    visit.plan.profile,
                ),
            },
            "intent": visit.plan.intent,
            "intent_source": visit.plan.intent_source,
            "content_kind": visit.plan.content_kind,
            "target_subdeaddit": visit.plan.target_subdeaddit,
            "length_target_id": visit.plan.length_target_id,
            "direction_ids": list(visit.plan.direction_ids),
            "engagement_focus_id": visit.plan.engagement_focus_id,
            "offered_tool_names": sorted(visit.plan.offered_tool_names),
            "render_variables": {
                kind: dict(values)
                for kind, values in visit.plan.render_variables.items()
            },
            "initial_messages": [dict(message) for message in messages],
        }
        specs = visit.tool_specs
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return _fail(
            agent,
            run,
            0,
            0,
            usage,
            f"{type(exc).__name__}: {exc}",
            strike=False,
        )
    if not _run_is_active(run.id):
        return _current_run(run.id) or run

    turn_count = 0
    action_count = 0
    nudged = False
    rejected_streak = 0
    started = time.monotonic()
    run_deadline = Deadline.after(max(1, _int_budget(config, "max_run_seconds")))
    ctx = ToolContext(
        agent=agent,
        run=run,
        user_username=run.persona_username,
        post_intent=visit.plan.intent,
        target_subdeaddit=visit.plan.target_subdeaddit,
        llm_api_url=api_url,
        llm_api_key=api_key,
        llm_model=model,
        deadline=run_deadline,
    )
    client = LLMClient()

    def accumulate(chunk: dict | None) -> None:
        chunk = chunk or {}
        for key in USAGE_KEYS:
            usage[key] += int(chunk.get(key, 0) or 0)

    try:
        while True:
            if not _run_is_active(run.id):
                return _current_run(run.id) or run
            if time.monotonic() - started >= _int_budget(config, "max_run_seconds"):
                break

            request_messages = [dict(message) for message in messages]
            result = client.complete(
                ChatRequest(
                    system_prompt="",
                    user_prompt="",
                    model=model,
                    api_url=api_url,
                    api_key=api_key,
                    sampling=Sampling(max_tokens=4096),
                    extra_payload={"messages": messages},
                    tools=specs,
                )
            )
            accumulate(result.usage)
            if not _run_is_active(run.id):
                return _current_run(run.id) or run

            assistant: dict[str, Any] = {
                "role": "assistant",
                "content": result.content or "",
            }
            tool_calls = result.tool_calls or []
            if tool_calls:
                # Verbatim native tool_calls; never parsed out of free text.
                assistant["tool_calls"] = tool_calls

            turn = AgentTurn(
                run_id=run.id,
                seq=turn_count + 1,
                request_messages=request_messages,
                response_message=assistant,
                model=result.model,
                latency_ms=int(result.latency_ms or 0),
            )
            db.session.add(turn)
            db.session.commit()
            turn_count += 1
            messages.append(assistant)

            if not tool_calls:
                if nudged:
                    # Second offense: force finish.
                    break
                messages.append({"role": "user", "content": NUDGE_MESSAGE})
                nudged = True
                continue

            ended = False
            for tool_call in tool_calls:
                if not _run_is_active(run.id):
                    return _current_run(run.id) or run
                function = tool_call.get("function") or {}
                name = function.get("name") or ""
                raw_arguments = function.get("arguments", "{}")
                try:
                    # Malformed-argument payloads flow through the executor's
                    # uniform validation/persistence path as a raw string.
                    outcome = execute(name, raw_arguments, ctx)
                except Exception as exc:  # keep the run alive on executor blowups
                    outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                if not _run_is_active(run.id):
                    return _current_run(run.id) or run
                action_count += 1
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id"),
                        "content": json.dumps(outcome, default=str),
                    }
                )
                if outcome.get("force_finish") or name == "finish":
                    ended = True
                    break
                # Correction cap: two consecutive guardrail rejections in a
                # row mean the model is not converging — force finish (the
                # plan's "max 2 correction attempts" semantics).
                if outcome.get("kind") == "rejected":
                    rejected_streak += 1
                    if rejected_streak >= 2:
                        ended = True
                        break
                else:
                    rejected_streak = 0
            if ended:
                break
            if action_count >= _int_budget(config, "max_actions_per_run"):
                break
    except PermanentLLMError as exc:
        return _fail(agent, run, turn_count, action_count, usage, str(exc), strike=True)
    except Exception as exc:
        return _fail(
            agent,
            run,
            turn_count,
            action_count,
            usage,
            f"{type(exc).__name__}: {exc}",
            strike=False,
        )

    db.session.rollback()
    current_run = _current_run(run.id)
    if current_run is None or current_run.status != "running":
        return current_run or run
    current_agent = (
        db.session.query(Agent)
        .populate_existing()
        .filter(Agent.id == agent.id)
        .one_or_none()
    )
    now = datetime.utcnow()
    metadata = (
        dict(current_run.prompt_metadata)
        if isinstance(current_run.prompt_metadata, dict)
        else {}
    )
    values = {
        AgentRun.status: "completed",
        AgentRun.finished_at: now,
        AgentRun.turn_count: turn_count,
        AgentRun.action_count: action_count,
        AgentRun.token_usage: usage,
    }
    if (
        current_agent is not None
        and current_agent.status == "running"
        and current_agent.is_enabled
    ):
        min_delay = _int_budget(config, "min_delay")
        max_delay = max(min_delay, _int_budget(config, "max_delay"))
        base_delay = float(random.uniform(min_delay, max_delay))
        scheduled_delay = float(scaled_wake_delay(base_delay, now))
        current_agent.next_run_at = now + timedelta(seconds=scheduled_delay)
        metadata["cadence_sample"] = {
            "base_delay_seconds": base_delay,
            "scheduled_delay_seconds": scheduled_delay,
        }
        values[AgentRun.prompt_metadata] = metadata
    elif current_agent is not None:
        values[AgentRun.prompt_metadata] = metadata

    updated = db.session.query(AgentRun).filter(
        AgentRun.id == run.id,
        AgentRun.status == "running",
    ).update(values, synchronize_session=False)
    if not updated:
        db.session.rollback()
        return _current_run(run.id) or run

    if current_agent is not None and current_agent.status == "running":
        current_agent.consecutive_failures = 0
        current_agent.status = "idle"
        current_agent.last_run_at = now
        if not current_agent.is_enabled:
            current_agent.next_run_at = None
    db.session.commit()
    finished = _current_run(run.id) or run
    try:
        summarize_run(current_agent or agent, finished)
    except Exception:
        logger.exception("summarize_run failed; ignoring.")
    db.session.commit()
    return finished
