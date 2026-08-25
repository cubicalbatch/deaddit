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

from deaddit import Config
from deaddit.agents.executor import execute
from deaddit.agents.memory import build_initial_messages, summarize_run
from deaddit.agents.registry import ToolContext, specs_for
from deaddit.extensions import db
from deaddit.llm import (
    ChatRequest,
    LLMClient,
    PermanentLLMError,
    Sampling,
)
from deaddit.models import Agent, AgentRun, AgentTurn, Setting

logger = logging.getLogger(__name__)

# Budget defaults applied when absent from agent.config.
DEFAULT_CONFIG: dict[str, Any] = {
    "max_actions_per_run": 12,
    "max_run_seconds": 300,
    "min_delay": 60,
    "max_delay": 900,
}

USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")

NUDGE_MESSAGE = "Use a tool to act, or call finish."

CONSECUTIVE_FAILURE_DISABLE_THRESHOLD = 5


def is_runtime_enabled() -> bool:
    """Read the AGENT_RUNTIME_ENABLED feature flag (Setting row, default false).

    Nothing consults this yet - scheduling arrives in Phase 2. Explicit manual
    invocation (`deaddit agent run-once`) is always allowed regardless of the
    flag: explicit user intent satisfies decision 1.
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
    refused). Stale-running recovery proper lands in Phase 2.
    """
    now = datetime.utcnow()
    grace = timedelta(seconds=_int_budget(_effective_config(agent), "max_run_seconds") + 60)
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
        AgentRun.query.filter_by(agent_id=agent.id, status="running").first() is not None
    )


def _effective_config(agent: Agent) -> dict[str, Any]:
    return {**DEFAULT_CONFIG, **(agent.config or {})}


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
    if strike:
        agent.consecutive_failures = (agent.consecutive_failures or 0) + 1
        if agent.consecutive_failures >= CONSECUTIVE_FAILURE_DISABLE_THRESHOLD:
            agent.is_enabled = False
            agent.status = "disabled"
            agent.next_run_at = None
    db.session.commit()
    return run


def run_once(username: str, *, trigger: str = "manual") -> AgentRun:
    """Run one full agent visit synchronously. Caller provides the app context."""
    agent = Agent.query.filter_by(user_username=username).first()
    if agent is None:
        raise ValueError(f"No agent registered for user '{username}'")

    if _recover_stale_runs(agent):
        raise ValueError(
            f"Agent '{username}' already has a run in progress "
            f"(stale-running recovery lands in Phase 2)"
        )

    config = _effective_config(agent)
    api_url = config.get("api_url") or Config.get("OPENAI_API_URL")
    model = config.get("model") or Config.get("OPENAI_MODEL", "llama3")
    api_key = Config.get_api_key_for_endpoint(api_url)
    specs = specs_for(agent.autonomy_tier)

    now = datetime.utcnow()
    run = AgentRun(
        agent_id=agent.id,
        trigger=trigger,
        status="running",
        started_at=now,
        turn_count=0,
        action_count=0,
        token_usage={},
    )
    agent.status = "running"
    db.session.add(run)
    db.session.commit()

    messages = build_initial_messages(agent)
    usage: dict[str, int] = dict.fromkeys(USAGE_KEYS, 0)
    turn_count = 0
    action_count = 0
    nudged = False
    rejected_streak = 0
    started = time.monotonic()
    ctx = ToolContext(agent=agent, run=run, user_username=username)
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
                    sampling=Sampling(max_tokens=2048),
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
        return _fail(
            agent, run, turn_count, action_count, usage, str(exc), strike=True
        )
    except Exception as exc:
        return _fail(
            agent, run, turn_count, action_count, usage, f"{type(exc).__name__}: {exc}", strike=False
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
        agent.next_run_at = now + timedelta(seconds=random.uniform(min_delay, max_delay))
    try:
        summarize_run(agent, run)
    except Exception:
        logger.exception("summarize_run failed; ignoring.")
    db.session.commit()
    return run
