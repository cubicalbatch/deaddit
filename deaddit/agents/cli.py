"""Click commands for managing autonomous agents."""

import json
import logging
import sys

import click

from deaddit import Config, create_app
from deaddit.agents.cohort import (
    COHORT_SPEC_VERSION,
    CohortSpecError,
    load_spec,
    spec_summary,
)
from deaddit.agents.loop import DEFAULT_CONFIG, run_once
from deaddit.agents.memory import backfill_persona_history
from deaddit.agents.parity import build_sample_packet, compute_report, connect_ro
from deaddit.agents.registry import AutonomyTier
from deaddit.extensions import db
from deaddit.llm import CapabilityError, ensure_tools_allowed
from deaddit.llm import capabilities as _capabilities
from deaddit.models import Agent, ToolCall, User

logger = logging.getLogger(__name__)


def _make_app(db_uri=None):
    """Build the app; DB_URI override routes every query away from instance/."""
    if db_uri:
        return create_app({"SQLALCHEMY_DATABASE_URI": db_uri})
    return create_app()


@click.group()
@click.option(
    "--db",
    "db_uri",
    envvar="DEADDIT_DB_URI",
    default=None,
    help="SQLAlchemy database URI override (applies to all subcommands).",
)
@click.pass_context
def agent(ctx, db_uri) -> None:
    """Manage autonomous agents."""
    ctx.ensure_object(dict)
    ctx.obj["db_uri"] = db_uri


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
@click.pass_context
def create(ctx, username, tier, api_url, model, min_delay, max_delay, enable) -> None:
    """Register or update an agent for an existing user persona."""
    app = _make_app((ctx.obj or {}).get("db_uri"))
    with app.app_context():
        _upsert_agent(username, tier, api_url, model, min_delay, max_delay, enable)


def _upsert_agent(
    username,
    tier,
    api_url,
    model,
    min_delay,
    max_delay,
    enable,
    extra_config=None,
) -> Agent:
    """Probe the endpoint then insert/update one Agent row; returns the row."""
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
    if extra_config:
        config.update(extra_config)

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
    return agent_row


@agent.command("create-cohort")
@click.option(
    "--spec",
    "spec_path",
    required=True,
    type=click.Path(exists=True),
    help="Path to a cohort spec JSON file.",
)
@click.option(
    "--backfill-memory/--no-backfill-memory",
    default=True,
    show_default=True,
    help="Backfill each persona's pre-agent history into memory episodes.",
)
@click.option(
    "--enable/--no-enable",
    default=False,
    show_default=True,
    help="Enable the cohort for scheduling (default: create disabled).",
)
@click.pass_context
def create_cohort(ctx, spec_path, backfill_memory, enable) -> None:
    """Create every agent in a validated cohort spec (disabled unless --enable)."""
    try:
        spec = load_spec(spec_path)
    except CohortSpecError as exc:
        raise click.ClickException(str(exc)) from exc

    summary = spec_summary(spec)
    endpoint = spec["endpoint"]
    api_url = endpoint["api_url"]
    model = endpoint["model"]
    app = _make_app((ctx.obj or {}).get("db_uri"))
    with app.app_context():
        # One probe gate for the whole cohort, before any agent row exists.
        try:
            ensure_tools_allowed(api_url, model, auto_probe=True)
        except CapabilityError as exc:
            raise click.ClickException(
                f"Endpoint {api_url} / model {model} does not support tools: {exc}"
            ) from exc
        evidence = _capabilities.LAST_PROBE_EVIDENCE

        rows = []
        for entry in spec["agents"]:
            extra_config = None
            if "daily_request_ceiling" in entry:
                extra_config = {"daily_request_ceiling": entry["daily_request_ceiling"]}
            row = _upsert_agent(
                entry["username"],
                entry["tier"],
                api_url,
                model,
                entry["min_delay"],
                entry["max_delay"],
                enable,
                extra_config=extra_config,
            )
            episodes = 0
            warning = None
            if backfill_memory:
                try:
                    episodes = int(
                        backfill_persona_history(
                            entry["username"], api_url=api_url, model=model
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "Backfill failed for '%s': %s", entry["username"], exc
                    )
                    warning = f"backfill failed: {exc}"
            rows.append((entry, row, episodes, warning))

        click.echo(
            f"Cohort v{COHORT_SPEC_VERSION}: {summary['count']} agents "
            f"(tiers={summary['tiers']}) enabled={enable}"
        )
        for entry, _row, episodes, warning in rows:
            ceiling = entry.get("daily_request_ceiling", "-")
            line = (
                f"{entry['username']:<20} tier={entry['tier']:<10} "
                f"cadence={entry['min_delay']}-{entry['max_delay']}s "
                f"ceiling={ceiling} "
            )
            if warning:
                line += f"WARNING: {warning}"
            else:
                line += f"episodes={episodes}"
            click.echo(line.rstrip())
        if evidence:
            click.echo(f"probe evidence: {json.dumps(evidence, sort_keys=True)}")


@agent.command("list")
@click.pass_context
def list_agents(ctx) -> None:
    """List registered agents."""
    app = _make_app((ctx.obj or {}).get("db_uri"))
    with app.app_context():
        rows = Agent.query.order_by(Agent.user_username).all()
        if not rows:
            click.echo("No agents registered.")
            return
        header = (
            f"{'username':<20} {'tier':<12} {'enabled':<8} {'status':<10} "
            f"{'last_run_at':<20} {'next_run_at':<20} {'fails':>5}"
        )
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
@click.option(
    "--intent",
    type=click.Choice(["post", "browse"]),
    default=None,
    help="Force a specific intent (post or browse).",
)
@click.pass_context
def run_once_command(ctx, username, intent) -> None:
    """Run one synchronous visit for USERNAME and print the trace."""
    app = _make_app((ctx.obj or {}).get("db_uri"))
    with app.app_context():
        exists = Agent.query.filter_by(user_username=username).first()
        if exists is None:
            raise click.ClickException(f"No agent registered for user '{username}'")

        try:
            run = run_once(username, trigger="manual", force_intent=intent)
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
                result = (
                    call.result
                    if isinstance(call.result, dict)
                    else json.loads(call.result or "{}")
                )
            except (ValueError, TypeError):
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


def _verdict(pass_) -> str:
    """Render the tri-state gate verdict (True/False/None)."""
    return {True: "PASS", False: "FAIL", None: "INDETERMINATE"}[pass_]


def _pct(rate) -> str:
    return "n/a" if rate is None else f"{rate:.1%}"


@agent.command("parity-report")
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Path to a SQLite DB COPY (read-only); defaults to instance/deaddit.db",
)
@click.option(
    "--window-start",
    default=None,
    help="Window start 'YYYY-MM-DD HH:MM:SS' (default: end - 24h)",
)
@click.option(
    "--window-end",
    default=None,
    help="Window end 'YYYY-MM-DD HH:MM:SS' (default: MAX(created_at))",
)
@click.option("--baseline-days", type=int, default=7, show_default=True)
@click.option(
    "--json", "as_json", is_flag=True, help="Dump the raw report dict as JSON"
)
def parity_report(db_path, window_start, window_end, baseline_days, as_json) -> None:
    """Compute parity-gate criteria (a)-(c) over a window (pure SQL, read-only)."""
    conn = connect_ro(db_path)
    try:
        report = compute_report(
            conn,
            window_start=window_start,
            window_end=window_end,
            baseline_days=baseline_days,
        )
    finally:
        conn.close()
    if as_json:
        click.echo(json.dumps(report, indent=2, sort_keys=True))
        return

    w, b, a = report["window"], report["baseline"], report["agent"]
    ca, cb, cc = (
        report["criterion_a"],
        report["criterion_b"],
        report["criterion_c"],
    )
    v, s = report["volume"], report["volume"]["source_split"]
    ls = report["llm_spend"]
    ratio = "n/a" if a["ratio"] is None else f"{a['ratio']:.3f}"
    cost = "unknown" if ls["estimated_cost"] is None else f"${ls['estimated_cost']:.4f}"
    click.echo(f"window: [{w['start']}, {w['end']}) ({w['hours']:.1f}h)")
    click.echo(
        f"baseline: {b['days']}d trailing legacy rate: {b['posts_per_day']:.2f}"
        f" posts/day + {b['comments_per_day']:.2f} comments/day ="
        f" {b['total_per_day']:.2f} total/day"
    )
    click.echo(
        f"agent: {a['posts_per_day']:.2f} posts/day +"
        f" {a['comments_per_day']:.2f} comments/day ="
        f" {a['total_per_day']:.2f} total/day (ratio {ratio})"
    )
    click.echo(
        f"criterion a (volume within [{ca['low_bound']:.2f},"
        f" {ca['high_bound']:.2f}] of baseline): ratio={ratio}"
        f" -> {_verdict(ca['pass'])}"
    )
    click.echo(
        f"criterion b (duplicate rejections < 10% of write attempts):"
        f" {cb['duplicate_rejections']}/{cb['write_attempts']}"
        f" = {_pct(cb['rate'])} -> {_verdict(cb['pass'])}"
    )
    click.echo(
        f"criterion c (failed runs < 5% of terminal runs):"
        f" {cc['failed_runs']}/{cc['terminal_runs']}"
        f" = {_pct(cc['rate'])} -> {_verdict(cc['pass'])}"
    )
    click.echo(
        f"volume: {v['posts_per_day']:.2f} posts/day,"
        f" {v['comments_per_day']:.2f} comments/day,"
        f" {v['distinct_active_authors']} distinct active authors"
        f" (agent posts {s['agent_posts']}, legacy posts {s['legacy_posts']},"
        f" agent comments {s['agent_comments']}, legacy comments"
        f" {s['legacy_comments']})"
    )
    click.echo(
        f"llm_spend: {ls['attempts']} attempts ({ls['ok_attempts']} ok,"
        f" {ls['failed_attempts']} failed), tokens prompt={ls['prompt_tokens']}"
        f" completion={ls['completion_tokens']} total={ls['total_tokens']},"
        f" estimated_cost={cost}"
    )


@agent.command("sample-packet")
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Path to a SQLite DB COPY (read-only); defaults to instance/deaddit.db",
)
@click.option("--seed", type=int, required=True, help="Deterministic sampling seed")
@click.option("--items", "min_items", type=int, default=20, show_default=True)
@click.option("--window-start", default=None, help="'YYYY-MM-DD HH:MM:SS'")
@click.option("--window-end", default=None, help="'YYYY-MM-DD HH:MM:SS'")
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    default=None,
    help="Write the markdown packet here instead of stdout",
)
def sample_packet(db_path, seed, min_items, window_start, window_end, output) -> None:
    """Generate the deterministic reviewer-sampling packet (criterion d)."""
    conn = connect_ro(db_path)
    try:
        packet = build_sample_packet(
            conn,
            seed=seed,
            min_items=min_items,
            window_start=window_start,
            window_end=window_end,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()
    if output is None:
        click.echo(packet)
        return
    with open(output, "w", encoding="utf-8") as fh:
        fh.write(packet)
    click.echo(f"wrote {min_items} sampled items (seed {seed}) to {output}")
