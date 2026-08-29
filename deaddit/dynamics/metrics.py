"""PlatformDaily rollups and health metrics (Phase D6, plan §8).

The nightly job (:func:`run_nightly_rollup`, registered in
:mod:`deaddit.runtime.nightly`) folds one UTC day of raw truth into a single
``PlatformDaily`` row, idempotently:

- **Engagement** counts come from ``ActivityEvent`` rows (the raw truth,
  :mod:`deaddit.dynamics.activity`).
- **Spend** joins the LLM-3 ledger on ``day = date(created_at)`` with its
  exact conventions: token sums COALESCE to 0, but ``llm_cost_usd`` is NULL
  when NO priced attempt exists that day — unpriced attempts are never faked
  to $0; when some attempts are priced, SUM skips the NULLs.
- **Provenance** stays intact per Resolution 9: post/comment counts are
  bucketed by model marker into 'agent:*' vs 'seed' vs other and stored as
  ``provenance_json``. Rollups aggregate over BOTH agent-authored and
  seeded/backfilled rows — nothing filters by model. Vote rows add a separate
  four-way ``votes`` source split (simulated, agent, human, backfill); durable
  rows are counted once, so same-value no-ops and insert-only collisions do not
  become activity.
- **Cost semantics** remain LLM-only: ``cost_per_engagement`` is priced
  LLM spend divided by posts plus comments, never simulated votes. The additive
  ``tokens_per_content_action`` metric uses LLM input plus output tokens over
  posts plus comments; simulator work consumes no LLM tokens.
- **Health trio** is computed from content tables for that day:
  median thread depth of that day's comments; per-thread dissent share
  (fraction of that day's comments in the thread scored <= 0) averaged over
  threads active that day; per-subdeaddit participation Gini averaged over
  subs active that day.

Raw-SQL spot-checks the dashboards must agree with (acceptance criterion)::

    -- Engagement columns equal:
    SELECT COUNT(*) posts FROM activity_event
     WHERE event_type='post' AND date(occurred_at)=:day;   -- etc.

    -- Active agents / actions_per_active equal:
    SELECT COUNT(DISTINCT username), COUNT(*) FROM activity_event
     WHERE date(occurred_at)=:day;

    -- Spend columns equal:
    SELECT COALESCE(SUM(prompt_tokens),0),
           COALESCE(SUM(completion_tokens),0),
           SUM(estimated_cost)
      FROM llm_usage WHERE date(created_at)=:day;

    -- cost_per_engagement equals llm_cost_usd / NULLIF(posts+comments, 0).

Publication interface for AgenticCore (plan §Interface Contracts 5): call
:func:`health_snapshot` — dissent_share_avg and per-sub Gini series are the
feedback signals core treats as prompt-side levers.
"""

from __future__ import annotations

import json
import logging
import statistics
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import case, func

from deaddit.extensions import db
from deaddit.models import (
    ActivityEvent,
    Comment,
    LLMUsage,
    PlatformDaily,
    Post,
    Vote,
    VoteSimulationHourly,
)
logger = logging.getLogger(__name__)


#: Durable vote provenance values. Unknown historical values are deliberately
#: not folded into a known bucket, so a malformed row cannot masquerade as a
#: supported source.
VOTE_SOURCES = ("simulated", "agent", "human", "backfill")

#: Persisted counters owned by ``VoteSimulationHourly``. Keep this list
#: additive and in model order so the daily JSON is stable for API consumers.
SIMULATED_VOTING_COUNTERS = (
    "ticks",
    "errors",
    "active_proposals",
    "archive_proposals",
    "revival_proposals",
    "inserted_votes",
    "switched_votes",
    "upvotes",
    "downvotes",
    "cap_skips",
    "min_gap_skips",
    "no_voter_skips",
    "guardrail_skips",
)


def _vote_source_counts(start: datetime, end: datetime) -> dict[str, int]:
    """Count durable votes created during the UTC day by canonical source.

    ``Vote`` rows are the provenance source of truth rather than
    ``ActivityEvent`` rows: successful inserts and direction switches have a
    durable row, while same-value re-votes and insert-only collisions do not.
    """
    counts = {source: 0 for source in VOTE_SOURCES}
    rows = (
        db.session.query(Vote.source, func.count(Vote.id))
        .filter(Vote.created_at >= start, Vote.created_at < end)
        .group_by(Vote.source)
        .all()
    )
    for source, count in rows:
        if source in counts:
            counts[source] = int(count)
    return counts


def _simulation_counter_totals(start: datetime, end: datetime) -> dict[str, Any]:
    """Sum simulator counters for all modes in one UTC day.

    Hourly rows are mode-keyed, so a daily rollup must include both shadow and
    live work (and retain a per-mode split for operators comparing rollout
    phases). Missing rows and NULL-like values are represented as zero.
    """
    totals = {name: 0 for name in SIMULATED_VOTING_COUNTERS}
    by_mode: dict[str, dict[str, int]] = {}
    rows = (
        db.session.query(VoteSimulationHourly)
        .filter(
            VoteSimulationHourly.hour >= start,
            VoteSimulationHourly.hour < end,
        )
        .order_by(VoteSimulationHourly.mode, VoteSimulationHourly.hour)
        .all()
    )
    for row in rows:
        mode_totals = by_mode.setdefault(
            str(row.mode),
            {name: 0 for name in SIMULATED_VOTING_COUNTERS},
        )
        for name in SIMULATED_VOTING_COUNTERS:
            value = int(getattr(row, name) or 0)
            totals[name] += value
            mode_totals[name] += value
    totals["by_mode"] = by_mode
    return totals

logger = logging.getLogger(__name__)

#: Model-marker buckets (Resolution 9 provenance).
AGENT_PREFIX = "agent:"
SEED_MARKER = "seed"


def gini_coefficient(values: list[float]) -> float | None:
    """Standard Gini coefficient; None for empty or all-zero populations."""
    vals = sorted(float(v) for v in values)
    n = len(vals)
    if n == 0:
        return None
    total = sum(vals)
    if total == 0:
        return None
    cumulative = sum((i + 1) * v for i, v in enumerate(vals))
    return (2.0 * cumulative) / (n * total) - (n + 1.0) / n


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day)
    return start, start + timedelta(days=1)


def _event_counts(start: datetime, end: datetime) -> dict[str, int]:
    rows = (
        db.session.query(ActivityEvent.event_type, func.count(ActivityEvent.id))
        .filter(
            ActivityEvent.occurred_at >= start,
            ActivityEvent.occurred_at < end,
        )
        .group_by(ActivityEvent.event_type)
        .all()
    )
    counts = {str(kind): int(n) for kind, n in rows}
    return {
        "posts": counts.get("post", 0),
        "comments": counts.get("comment", 0),
        "votes": counts.get("vote", 0),
        "reports": counts.get("report", 0),
        "total": sum(counts.values()),
    }


def _active_agents(start: datetime, end: datetime) -> int:
    row = (
        db.session.query(func.count(func.distinct(ActivityEvent.username)))
        .filter(
            ActivityEvent.occurred_at >= start,
            ActivityEvent.occurred_at < end,
            ActivityEvent.username.isnot(None),
        )
        .one()
    )
    return int(row[0])


def _spend(day: date) -> dict[str, float | int | None]:
    row = (
        db.session.query(
            func.coalesce(func.sum(LLMUsage.prompt_tokens), 0),
            func.coalesce(func.sum(LLMUsage.completion_tokens), 0),
            func.sum(LLMUsage.estimated_cost),
        )
        .filter(func.date(LLMUsage.created_at) == day.isoformat())
        .one()
    )
    return {
        "tokens_in": int(row[0]),
        "tokens_out": int(row[1]),
        "cost_usd": float(row[2]) if row[2] is not None else None,
    }


def _provenance(start: datetime, end: datetime) -> dict:
    """Return content provenance, vote-source counts, and simulator totals.

    The ``posts`` and ``comments`` mappings are intentionally unchanged from
    Resolution 9. Additive ``votes`` and ``simulated_voting`` mappings keep
    vote provenance and worker health separate from content provenance.
    """

    def _bucket(model: str | None) -> str:
        if model is None:
            return "other"
        if model.startswith(AGENT_PREFIX):
            return "agent"
        if model == SEED_MARKER:
            return "seed"
        return "other"

    breakdown: dict[str, dict[str, int]] = {
        "posts": {"agent": 0, "seed": 0, "other": 0},
        "comments": {"agent": 0, "seed": 0, "other": 0},
    }
    for model, n in (
        db.session.query(Post.model, func.count(Post.id))
        .filter(Post.created_at >= start, Post.created_at < end)
        .group_by(Post.model)
        .all()
    ):
        breakdown["posts"][_bucket(model)] += int(n)
    for model, n in (
        db.session.query(Comment.model, func.count(Comment.id))
        .filter(Comment.created_at >= start, Comment.created_at < end)
        .group_by(Comment.model)
        .all()
    ):
        breakdown["comments"][_bucket(model)] += int(n)
    breakdown["votes"] = _vote_source_counts(start, end)
    breakdown["simulated_voting"] = _simulation_counter_totals(start, end)
    return breakdown


def _comment_depths(post_ids: list[int]) -> dict[int, int]:
    """Depth (chain length from the post root) per comment via one fetch."""
    depths: dict[int, int] = {}
    if not post_ids:
        return depths
    rows = (
        db.session.query(Comment.id, Comment.parent_id)
        .filter(Comment.post_id.in_(post_ids))
        .all()
    )
    parents: dict[int, int | None] = dict(rows)

    def _depth(cid: int) -> int:
        seen: list[int] = []
        depth = 0
        current: int | None = cid
        while True:
            if current in depths:
                depth += depths[current]
                break
            seen.append(current)
            parent = parents.get(current)
            if parent is None:
                break
            depth += 1
            current = parent
        for node in seen:
            depths[node] = depth
        return depth

    return {cid: _depth(cid) for cid in parents}


def _health_trio(start: datetime, end: datetime) -> dict[str, float | None]:
    """Median thread depth, avg dissent share, avg participation Gini."""
    day_posts = (
        Post.query.filter(Post.created_at >= start, Post.created_at < end)
        .with_entities(Post.id)
        .all()
    )
    comment_post_ids = [
        row[0]
        for row in db.session.query(Comment.post_id.distinct()).filter(
            Comment.created_at >= start, Comment.created_at < end
        )
    ]
    touched = sorted({p[0] for p in day_posts} | set(comment_post_ids))

    # Median thread depth over ALL comments live in threads touched today.
    depths = list(_comment_depths(touched).values())
    median_depth = statistics.median(depths) if depths else None

    # Dissent share per thread active today, then averaged across threads.
    shares: list[float] = []
    for post_id in comment_post_ids:
        total, negative = (
            db.session.query(
                func.count(Comment.id),
                func.coalesce(func.sum(case((Comment.score <= 0, 1), else_=0)), 0),
            )
            .filter(
                Comment.post_id == post_id,
                Comment.created_at >= start,
                Comment.created_at < end,
            )
            .one()
        )
        if total:
            shares.append(int(negative) / int(total))
    dissent_avg = statistics.fmean(shares) if shares else None

    # Participation Gini per sub active today, averaged across subs.
    ginis: list[float] = []
    sub_names = (
        [
            row[0]
            for row in db.session.query(Post.subdeaddit_name).filter(
                Post.id.in_(touched)
            )
        ]
        if touched
        else []
    )
    from deaddit.dynamics.degeneracy import _participation_by_user

    for sub in sorted(set(sub_names)):
        counts = _participation_by_user(sub, start)
        value = gini_coefficient(list(counts.values()))
        if value is not None:
            ginis.append(value)
    gini_avg = statistics.fmean(ginis) if ginis else None

    return {
        "median_thread_depth": median_depth,
        "dissent_share_avg": dissent_avg,
        "gini_participation_avg": gini_avg,
    }


def rollup_day(day: date) -> PlatformDaily:
    """Compute (or recompute) the PlatformDaily row for one UTC day.

    Idempotent: an existing row for ``day`` is overwritten in place.
    """
    start, end = _day_bounds(day)
    events = _event_counts(start, end)
    agents = _active_agents(start, end)
    spend = _spend(day)
    health = _health_trio(start, end)

    engagement = events["posts"] + events["comments"]
    cost = spend["cost_usd"]
    cost_per_engagement = (
        cost / engagement if cost is not None and engagement > 0 else None
    )

    row = db.session.get(PlatformDaily, day)
    if row is None:
        row = PlatformDaily(day=day)
        db.session.add(row)
    row.posts = events["posts"]
    row.comments = events["comments"]
    row.votes = events["votes"]
    row.reports = events["reports"]
    row.active_agents = agents
    row.actions_per_active = round(events["total"] / agents, 4) if agents else None
    row.llm_tokens_in = spend["tokens_in"]
    row.llm_tokens_out = spend["tokens_out"]
    row.llm_cost_usd = cost
    row.cost_per_engagement = (
        round(cost_per_engagement, 8) if cost_per_engagement is not None else None
    )
    row.median_thread_depth = health["median_thread_depth"]
    row.dissent_share_avg = health["dissent_share_avg"]
    row.gini_participation_avg = health["gini_participation_avg"]
    row.provenance_json = json.dumps(_provenance(start, end))
    db.session.commit()
    logger.info("platform rollup committed for %s", day.isoformat())
    return row


def run_nightly_rollup(now: datetime | None = None) -> PlatformDaily:
    """Registered nightly entry point: roll up YESTERDAY (UTC)."""
    now = now or datetime.utcnow()
    return rollup_day((now - timedelta(days=1)).date())


def daily_series(days: int = 30) -> list[PlatformDaily]:
    """Ascending rollup rows for the trailing ``days`` days (admin tab)."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).date()
    return (
        PlatformDaily.query.filter(PlatformDaily.day >= cutoff)
        .order_by(PlatformDaily.day.asc())
        .all()
    )


def daily_metric_row(row: PlatformDaily) -> dict[str, Any]:
    """Expose additive metrics persisted in a daily row.

    ``PlatformDaily`` intentionally remains schema-stable. New simulator and
    vote-provenance values live in its JSON payload, while this helper gives
    admin/API callers a typed, null-safe view. Older rows naturally return
    zero-valued counters.
    """
    try:
        payload = json.loads(row.provenance_json or "{}")
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    raw_votes = payload.get("votes", {})
    raw_simulation = payload.get("simulated_voting", {})
    if not isinstance(raw_votes, dict):
        raw_votes = {}
    if not isinstance(raw_simulation, dict):
        raw_simulation = {}
    vote_sources = {
        source: int(raw_votes.get(source, 0)) for source in VOTE_SOURCES
    }
    simulation = {
        name: int(raw_simulation.get(name, 0))
        for name in SIMULATED_VOTING_COUNTERS
    }
    by_mode = raw_simulation.get("by_mode", {})
    simulation["by_mode"] = by_mode if isinstance(by_mode, dict) else {}

    content_actions = int(row.posts or 0) + int(row.comments or 0)
    token_total = int(row.llm_tokens_in or 0) + int(row.llm_tokens_out or 0)
    return {
        "day": row.day,
        "vote_sources": vote_sources,
        "simulated_voting": simulation,
        "tokens_per_content_action": (
            round(token_total / content_actions, 4) if content_actions else None
        ),
    }


def daily_metrics(days: int = 30) -> list[dict[str, Any]]:
    """Return additive vote, simulator, and token metrics for daily rows."""
    return [daily_metric_row(row) for row in daily_series(days)]


def sub_gini_series(window_days: int = 7) -> dict[str, float]:
    """Per-subdeaddit participation Gini over the trailing window.

    Half of the AgenticCore publication contract (with dissent_share_avg
    above): core treats high-Gini subs as prompt-side diversity signals.
    """
    from deaddit.dynamics.degeneracy import _participation_by_user

    cutoff = datetime.utcnow() - timedelta(days=window_days)
    out: dict[str, float] = {}
    for (sub,) in db.session.query(Post.subdeaddit_name).distinct():
        counts = _participation_by_user(sub, cutoff)
        value = gini_coefficient(list(counts.values()))
        if value is not None:
            out[sub] = round(value, 4)
    return out


def health_snapshot(days: int = 7) -> dict:
    """AgenticCore-facing snapshot: rollup series plus additive metrics.

    ``cost_per_engagement`` remains the LLM-token-funded cost of content
    production. ``tokens_per_content_action`` is reported separately, and
    simulated voting contributes neither metric.
    """
    series = daily_series(days)
    return {
        "days": days,
        "series": [
            {
                "day": row.day.isoformat(),
                "active_agents": row.active_agents,
                "actions_per_active": row.actions_per_active,
                "llm_cost_usd": row.llm_cost_usd,
                "cost_per_engagement": row.cost_per_engagement,
                "tokens_per_content_action": metric["tokens_per_content_action"],
                "vote_sources": metric["vote_sources"],
                "simulated_voting": metric["simulated_voting"],
                "dissent_share_avg": row.dissent_share_avg,
                "gini_participation_avg": row.gini_participation_avg,
            }
            for row in series
            for metric in (daily_metric_row(row),)
        ],
        "sub_gini": sub_gini_series(window_days=days),
    }
