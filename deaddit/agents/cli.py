"""Click commands for managing autonomous agents."""

import json
import sys

import click

from deaddit import Config, create_app
from deaddit.agents.loop import DEFAULT_CONFIG, run_once
from deaddit.agents.registry import AutonomyTier
from deaddit.extensions import db
from deaddit.llm import CapabilityError, ensure_tools_allowed
from deaddit.models import Agent, ToolCall, User


@click.group()
def agent() -> None:
    """Manage autonomous agents."""


@agent.command("create")
@click.option("--username", required=True, help="Existing user persona to embody")
@click.option(
    "--tier",
    type=click.Choice([t.value for t in AutonomyTier]),
    default=AutonomyTier.REGULAR.value,
    show_default=True,
)
@click.option("--api-url", default=None, help="LLM API base URL")
@click.option("--model", default="qwen3.8-27b", show_default=True)
@click.option("--min-delay", type=int, default=60, show_default=True)
@click.option("--max-delay", type=int, default=900, show_default=True)
@click.option("--enable/--no-enable", default=False, show_default=True)
def create(username, tier, api_url, model, min_delay, max_delay, enable) -> None:
    """Register or update an agent for an existing user persona."""
    app = create_app()
    with app.app_context():
        user = db.session.get(User, username)
        if user is None:
            raise click.ClickException(
                f"User persona '{username}' does not exist. Create it first."
            )

        api_url = api_url or Config.get("OPENAI_API_URL") or ""
        try:
            ensure_tools_allowed(api_url, model, auto_probe=True)
        except CapabilityError as exc:
            raise click.ClickException(
                f"Endpoint {api_url} / model {model} does not support tools: {exc}"
            ) from exc

        config = {
            "api_url": api_url,
            "model": model,
            "min_delay": min_delay,
            "max_delay": max_delay,
            "max_actions_per_run": DEFAULT_CONFIG["max_actions_per_run"],
            "max_run_seconds": DEFAULT_CONFIG["max_run_seconds"],
        }

        existing = Agent.query.filter_by(user_username=username).first()
        if existing is not None:
            existing.autonomy_tier = tier
            existing.config = config
            existing.is_enabled = enable
            agent_row = existing
        else:
            agent_row = Agent(
                user_username=username,
                autonomy_tier=tier,
                is_enabled=enable,
                status="idle",
                config=config,
                state={},
                consecutive_failures=0,
            )
            db.session.add(agent_row)
        db.session.commit()
        click.echo(
            f"Agent for '{username}' saved: tier={tier} enabled={enable} "
            f"model={model} api_url={api_url}"
        )


@agent.command("list")
def list_agents() -> None:
    """List registered agents."""
    app = create_app()
    with app.app_context():
        rows = Agent.query.order_by(Agent.user_username).all()
        if not rows:
            click.echo("No agents registered.")
            return
        header = (
            f"{'username':<20} {'tier':<12} {'enabled':<8} {'status':<10} "
            f"{'last_run_at':<20} {'next_run_at':<20} {'fails':>5}"
        )
        click.echo(header)
        click.echo("-" * len(header))
        for row in rows:
            click.echo(
                f"{row.user_username:<20} {row.autonomy_tier:<12} "
                f"{str(bool(row.is_enabled)):<8} {row.status:<10} "
                f"{_fmt_dt(row.last_run_at):<20} {_fmt_dt(row.next_run_at):<20} "
                f"{row.consecutive_failures or 0:>5}"
            )


def _fmt_dt(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else "-"


@agent.command("run-once")
@click.argument("username")
def run_once_command(username) -> None:
    """Run one synchronous visit for USERNAME and print the trace."""
    app = create_app()
    with app.app_context():
        exists = Agent.query.filter_by(user_username=username).first()
        if exists is None:
            raise click.ClickException(f"No agent registered for user '{username}'")

        try:
            run = run_once(username, trigger="manual")
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

        click.echo(f"run {run.id}: status={run.status} trigger={run.trigger}")
        calls = ToolCall.query.filter_by(run_id=run.id).order_by(ToolCall.id).all()
        for index, call in enumerate(calls, start=1):
            outcome = "ok" if call.ok else f"error={call.error}"
            click.echo(
                f"  action {index}: {call.name} -> {outcome} "
                f"({call.duration_ms or 0} ms)"
            )
        usage = run.token_usage or {}
        click.echo(
            f"turns={run.turn_count} actions={run.action_count} "
            f"tokens={usage.get('total_tokens', 0)} "
            f"(prompt={usage.get('prompt_tokens', 0)}, "
            f"completion={usage.get('completion_tokens', 0)})"
        )
        summary = _finish_summary(calls)
        if summary:
            click.echo(f"summary: {summary}")
        posts, comments = _creation_counts(calls)
        click.echo(f"created this run: posts={posts} comments={comments}")

        if run.status != "completed":
            if run.error_message:
                click.echo(f"error: {run.error_message}", err=True)
            sys.exit(1)


def _finish_summary(calls):
    for call in reversed(calls):
        if call.name == "finish":
            try:
                result = json.loads(call.result or "{}")
            except ValueError:
                return None
            return result.get("summary") or result.get("mood")
    return None


def _creation_counts(calls) -> tuple[int, int]:
    """Count successful writes by canonical tool name (no substring guesses:
    read_post/view_profile must not inflate the trace)."""
    posts = 0
    comments = 0
    for call in calls:
        if not call.ok:
            continue
        if call.name == "create_post":
            posts += 1
        elif call.name == "create_comment":
            comments += 1
    return posts, comments
