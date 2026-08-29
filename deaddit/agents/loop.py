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
from deaddit.models import Agent, AgentRun, AgentTurn, Setting, User

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


def _recover_stale_runs(agent: Agent) -> bool:
    """Mark runs stuck past max_run_seconds + 60s grace as 'interrupted'.

    Returns True when a genuinely live run still exists (agent must be
    refused). Stale-running recovery runs per-agent in this loop and per-tick
    in the wake scheduler.
    """
    now = datetime.utcnow()
    grace = timedelta(
        seconds=_int_budget(_effective_config(agent), "max_run_seconds") + 60
    )
    stuck = (
        AgentRun.query.filter_by(agent_id=agent.id, status="running")
        .filter(AgentRun.started_at < now - grace)
        .all()
    )
    for run in stuck:
        run.status = "interrupted"
        run.finished_at = now
        run.error_message = "Recovered: run exceeded wall-clock budget plus grace."
    if stuck:
        db.session.commit()
    return (
        AgentRun.query.filter_by(agent_id=agent.id, status="running").first()
        is not None
    )


def _effective_config(agent: Agent) -> dict[str, Any]:
    return {**DEFAULT_CONFIG, **(agent.config or {})}


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
    return random.choice(pool)


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
    """Close a failed run and apply backoff bookkeeping."""
    now = datetime.utcnow()
    run.status = "failed"
    run.finished_at = now
    run.turn_count = turn_count
    run.action_count = action_count
    run.token_usage = usage
    run.error_message = message[:2000]
    agent.status = "error"
    agent.last_run_at = now
    if agent.is_enabled:
        # A failed run must never leave next_run_at in the past, or the
        # scheduler re-fires immediately against a possibly-dead endpoint.
        # (The strike-disable path below overrides this with NULL.)
        agent.next_run_at = now + timedelta(seconds=FAILURE_BACKOFF_SECONDS)
    if strike:
        agent.consecutive_failures = (agent.consecutive_failures or 0) + 1
        if agent.consecutive_failures >= CONSECUTIVE_FAILURE_DISABLE_THRESHOLD:
            agent.is_enabled = False
            agent.status = "disabled"
            agent.next_run_at = None
    db.session.commit()
    return run


def run_once(
    agent_id: int,
    *,
    trigger: str = "manual",
    force_intent: str | None = None,
    requested_intent: str | None = None,
) -> AgentRun:
    """Run one full agent visit synchronously. Caller provides the app context."""
    req = requested_intent if requested_intent is not None else force_intent
    agent = db.session.get(Agent, agent_id)
    if agent is None:
        raise ValueError(f"No agent with id {agent_id}")

    if _recover_stale_runs(agent):
        raise ValueError(f"Agent {agent.id} already has a run in progress")

    config = _effective_config(agent)
    provider_id = config.get("provider_id")
    provider = None
    if provider_id:
        try:
            from deaddit.models import LLMProvider

            provider = db.session.get(LLMProvider, int(provider_id))
        except Exception:
            provider = None

    if provider is None and config.get("api_url"):
        try:
            from deaddit.models import LLMProvider

            provider = LLMProvider.query.filter(
                (LLMProvider.api_url == str(config["api_url"]).rstrip("/"))
                | (LLMProvider.api_url == str(config["api_url"]))
            ).first()
        except Exception:
            provider = None

    if provider is None:
        try:
            from deaddit.models import LLMProvider

            provider = LLMProvider.get_default()
        except Exception:
            provider = None

    if provider:
        api_url = config.get("api_url") or provider.api_url
        api_key = (
            provider.api_key.strip()
            if (provider.api_key and provider.api_key.strip())
            else Config.get_api_key_for_endpoint(api_url)
        )
        model = (
            config.get("model")
            or provider.default_model
            or Config.get("OPENAI_MODEL", "llama3")
        )
    else:
        api_url = config.get("api_url") or Config.get("OPENAI_API_URL")
        model = config.get("model") or Config.get("OPENAI_MODEL", "llama3")
        api_key = Config.get_api_key_for_endpoint(api_url)

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
            "length_target_id": visit.plan.length_target_id,
            "direction_ids": list(visit.plan.direction_ids),
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
                function = tool_call.get("function") or {}
                name = function.get("name") or ""
                raw_arguments = function.get("arguments", "{}")
                try:
                    # Malformed-argument payloads flow through the executor's
                    # uniform validation/persistence path as a raw string.
                    outcome = execute(name, raw_arguments, ctx)
                except Exception as exc:  # keep the run alive on executor blowups
                    outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
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

    now = datetime.utcnow()
    run.status = "completed"
    run.finished_at = now
    run.turn_count = turn_count
    run.action_count = action_count
    run.token_usage = usage
    agent.consecutive_failures = 0
    agent.status = "idle"
    agent.last_run_at = now
    if agent.is_enabled:
        min_delay = _int_budget(config, "min_delay")
        max_delay = max(min_delay, _int_budget(config, "max_delay"))
        agent.next_run_at = now + timedelta(
            seconds=random.uniform(min_delay, max_delay)
        )
    try:
        summarize_run(agent, run)
    except Exception:
        logger.exception("summarize_run failed; ignoring.")
    db.session.commit()
    return run
