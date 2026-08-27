"""AC-P3 offline parity measurement harness (Phase 3, slice 1).

Pure-SQL measurement over any copy of the deaddit SQLite schema (tables:
post, comment, agent_run, tool_call, llm_usage). No Flask app, no ORM,
no network, no LLM calls — this module only reads.

Implements the agentic-core.md Phase 3 acceptance metrics:

    Acceptance: 14 consecutive days with agents as primary content source:
    (a) agent-originated posts+comments/day within +/-30% of trailing legacy
        baseline;
    (b) duplicate-suppression rejection rate < 10% of write attempts
        (indicates loop health, visible via ToolCall aggregates);
    (c) fewer than 5% of runs ending ``failed``;
    (d) admin sign-off on content quality sampling >= 20 items.

Criterion (a) is evaluated over a sliding observation window (owner decision
19: longest continuous autonomous window, target >=24h floor 6h); the
baseline is the trailing non-agent content rate strictly before the window.
Criterion (b) counts duplicate-suppression rejections by substring match on
the stored ToolCall result text. Criterion (d) is served by
:func:`build_sample_packet`, which deterministically samples >=20 agent items
for reviewer scoring.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# Provenance marker per roadmap Res 9: agents stamp model = "agent:<name>".
AGENT_MARKER_LIKE = "agent:%"

# executor._check_duplicate rejection message; counted via LIKE '%<marker>%'
# against the raw stored tool_call.result text.
DUP_REJECTION_MARKER = "too similar to your earlier content"

#: Observation window length in hours when neither bound is passed explicitly
#: (owner decision 19: target >=24h autonomous window).
WINDOW_HOURS = 24

_DT_FMT = "%Y-%m-%d %H:%M:%S"

_REPO_ROOT = Path(__file__).resolve().parents[2]

_WRITE_TOOLS = ("create_post", "create_image_post", "create_comment")


def _fmt(dt: datetime) -> str:
    """Format a naive UTC datetime as the SQLite DateTime column string."""
    return dt.strftime(_DT_FMT)


def connect_ro(db_path: str | None = None) -> sqlite3.Connection:
    """Open a read-only connection to a deaddit SQLite database.

    ``db_path=None`` defaults to ``<repo>/instance/deaddit.db``. The URI
    ``mode=ro`` guarantees the harness can never mutate the database under
    measurement.
    """
    if db_path is None:
        db_path = str(_REPO_ROOT / "instance" / "deaddit.db")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_bound(value: str | None) -> datetime | None:
    return datetime.strptime(value, _DT_FMT) if value else None


def compute_report(
    conn: sqlite3.Connection,
    *,
    window_start: str | None = None,
    window_end: str | None = None,
    baseline_days: int = 7,
) -> dict:
    """Compute all AC-P3 gate metrics over one observation window.

    Bounds are naive UTC datetimes as ``"YYYY-MM-DD HH:MM:SS"`` strings
    (matching the SQLite DateTime columns). When both are omitted they are
    derived: ``end`` = MAX(created_at) across post/comment, ``start`` =
    ``end - WINDOW_HOURS``. The half-open interval ``[window_start,
    window_end)`` is used for every in-window count so adjacent windows do
    not double-count boundary rows.

    Returns a dict with keys ``window``, ``baseline``, ``agent``,
    ``volume``, ``criterion_a/b/c`` and ``llm_spend`` (see contract in
    refactor/agentic-core.md Phase 3 / owner decision 19).
    """
    start = _parse_bound(window_start)
    end = _parse_bound(window_end)
    if end is None:
        row = conn.execute(
            "SELECT MAX(m) AS m FROM ("
            "SELECT MAX(created_at) AS m FROM post "
            "UNION ALL SELECT MAX(created_at) FROM comment)"
        ).fetchone()
        if row["m"] is None:
            raise ValueError(
                "cannot derive window: post/comment tables contain no rows"
            )
        end = datetime.strptime(row["m"], _DT_FMT)
    if start is None:
        start = end - timedelta(hours=WINDOW_HOURS)
    hours = (end - start).total_seconds() / 3600.0
    ws, we = _fmt(start), _fmt(end)

    # --- Baseline (criterion a denominator): trailing legacy content rate.
    # NON-agent rows strictly before the window start.
    base_posts = conn.execute(
        "SELECT COUNT(*) FROM post WHERE created_at < :ws "
        "AND (model IS NULL OR model NOT LIKE :marker)",
        {"ws": ws, "marker": AGENT_MARKER_LIKE},
    ).fetchone()[0]
    base_comments = conn.execute(
        "SELECT COUNT(*) FROM comment WHERE created_at < :ws "
        "AND (model IS NULL OR model NOT LIKE :marker)",
        {"ws": ws, "marker": AGENT_MARKER_LIKE},
    ).fetchone()[0]
    baseline_total_per_day = (base_posts + base_comments) / baseline_days

    # --- Agent output inside the window (criterion a numerator).
    # Post.model LIKE 'agent:%' OR Comment.model LIKE 'agent:%'.
    agent_posts = conn.execute(
        "SELECT COUNT(*) FROM post WHERE model LIKE :marker "
        "AND created_at >= :ws AND created_at < :we",
        {"marker": AGENT_MARKER_LIKE, "ws": ws, "we": we},
    ).fetchone()[0]
    agent_comments = conn.execute(
        "SELECT COUNT(*) FROM comment WHERE model LIKE :marker "
        "AND created_at >= :ws AND created_at < :we",
        {"marker": AGENT_MARKER_LIKE, "ws": ws, "we": we},
    ).fetchone()[0]
    agent_total_per_day = (agent_posts + agent_comments) / (hours / 24.0)

    # Criterion (a): within +/-30% of trailing legacy baseline. Indeterminate
    # (pass None) when the baseline rate is zero — no legacy reference exists.
    ratio = (
        agent_total_per_day / baseline_total_per_day
        if baseline_total_per_day > 0
        else None
    )
    criterion_a_pass = None if ratio is None else 0.7 <= ratio <= 1.3

    # Criterion (b): duplicate-suppression rejection rate over write attempts
    # (loop-health signal, ToolCall aggregates). Rejections matched by
    # substring on the raw stored result text.
    _write_tool_placeholders = ", ".join("?" * len(_WRITE_TOOLS))
    attempts = conn.execute(
        f"SELECT COUNT(*) FROM tool_call WHERE name IN ({_write_tool_placeholders}) "
        "AND created_at >= ? AND created_at < ?",
        (*_WRITE_TOOLS, ws, we),
    ).fetchone()[0]
    dup_rejections = conn.execute(
        f"SELECT COUNT(*) FROM tool_call WHERE name IN ({_write_tool_placeholders}) "
        "AND created_at >= ? AND created_at < ? "
        "AND result LIKE '%' || ? || '%'",
        (*_WRITE_TOOLS, ws, we, DUP_REJECTION_MARKER),
    ).fetchone()[0]
    dup_rate = dup_rejections / attempts if attempts else None
    criterion_b_pass = None if attempts == 0 else dup_rate < 0.10

    # Criterion (c): failed-run share among TERMINAL runs ('running' rows have
    # not ended yet and must stay out of both numerator and denominator).
    terminal_runs = conn.execute(
        "SELECT COUNT(*) FROM agent_run WHERE status IN ('completed', 'failed') "
        "AND started_at >= ? AND started_at < ?",
        (ws, we),
    ).fetchone()[0]
    failed_runs = conn.execute(
        "SELECT COUNT(*) FROM agent_run WHERE status = 'failed' "
        "AND started_at >= ? AND started_at < ?",
        (ws, we),
    ).fetchone()[0]
    fail_rate = failed_runs / terminal_runs if terminal_runs else None
    criterion_c_pass = None if terminal_runs == 0 else fail_rate < 0.05

    # Volume dashboard query (plan Phase-3): per-day rates over the window,
    # distinct authors active in it, and per-source provenance split.
    vol_posts = conn.execute(
        "SELECT COUNT(*) FROM post WHERE created_at >= ? AND created_at < ?",
        (ws, we),
    ).fetchone()[0]
    vol_comments = conn.execute(
        "SELECT COUNT(*) FROM comment WHERE created_at >= ? AND created_at < ?",
        (ws, we),
    ).fetchone()[0]
    distinct_authors = conn.execute(
        "SELECT COUNT(DISTINCT user) FROM ("
        "SELECT user FROM post WHERE created_at >= :ws AND created_at < :we "
        "UNION ALL "
        "SELECT user FROM comment WHERE created_at >= :ws AND created_at < :we)",
        {"ws": ws, "we": we},
    ).fetchone()[0]

    # LLM spend accounting: SUM ignores NULLs in SQL, so estimated_cost stays
    # None when every row has unknown price (never faked as $0).
    usage = conn.execute(
        "SELECT COUNT(*) AS attempts, "
        "SUM(status = 'ok') AS ok_attempts, "
        "SUM(status = 'failed') AS failed_attempts, "
        "SUM(prompt_tokens) AS prompt_tokens, "
        "SUM(completion_tokens) AS completion_tokens, "
        "SUM(total_tokens) AS total_tokens, "
        "SUM(estimated_cost) AS estimated_cost "
        "FROM llm_usage WHERE created_at >= ? AND created_at < ?",
        (ws, we),
    ).fetchone()

    return {
        "window": {"start": ws, "end": we, "hours": hours},
        "baseline": {
            "days": baseline_days,
            "posts_per_day": base_posts / baseline_days,
            "comments_per_day": base_comments / baseline_days,
            "total_per_day": baseline_total_per_day,
        },
        "agent": {
            "posts_per_day": agent_posts / (hours / 24.0),
            "comments_per_day": agent_comments / (hours / 24.0),
            "total_per_day": agent_total_per_day,
            "ratio": ratio,
        },
        "volume": {
            "posts_per_day": vol_posts * 24.0 / hours,
            "comments_per_day": vol_comments * 24.0 / hours,
            "distinct_active_authors": distinct_authors,
            "source_split": {
                "agent_posts": agent_posts,
                "legacy_posts": vol_posts - agent_posts,
                "agent_comments": agent_comments,
                "legacy_comments": vol_comments - agent_comments,
            },
        },
        "criterion_a": {
            "ratio": ratio,
            "low_bound": 0.7,
            "high_bound": 1.3,
            "pass": criterion_a_pass,
        },
        "criterion_b": {
            "write_attempts": attempts,
            "duplicate_rejections": dup_rejections,
            "rate": dup_rate,
            "threshold": 0.10,
            "pass": criterion_b_pass,
        },
        "criterion_c": {
            "terminal_runs": terminal_runs,
            "failed_runs": failed_runs,
            "rate": fail_rate,
            "threshold": 0.05,
            "pass": criterion_c_pass,
        },
        "llm_spend": {
            "attempts": usage["attempts"],
            "ok_attempts": usage["ok_attempts"],
            "failed_attempts": usage["failed_attempts"],
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "total_tokens": usage["total_tokens"],
            "estimated_cost": usage["estimated_cost"],
        },
    }


_RUBRIC_DIMENSIONS = [
    "topical fit to subdeaddit",
    "persona consistency",
    "originality",
    "conversational context (comments address parent/post)",
    "language quality",
]

_REDFLAGS = [
    "spam-wave phrasing",
    "near-duplicate of another sample",
    "off-topic reply",
]


def _snippet(text: str | None, limit: int = 160) -> str:
    """First ``limit`` chars with newlines collapsed to spaces."""
    collapsed = " ".join((text or "").split())
    return collapsed[:limit]


def build_sample_packet(
    conn: sqlite3.Connection,
    *,
    seed: int,
    min_items: int = 20,
    window_start: str | None = None,
    window_end: str | None = None,
) -> str:
    """Build a deterministic markdown reviewer-sampling packet (criterion d).

    Candidates are agent-stamped rows (model LIKE 'agent:%') whose
    created_at falls in ``[window_start, window_end)`` — same derivation as
    :func:`compute_report` when bounds are omitted. Selection is fully
    deterministic: candidates sorted by ``(kind, id)`` with 'comment' <
    'post', then ``random.Random(seed).sample`` picks indices which are
    re-sorted ascending, so the same seed always yields the same packet.

    Raises ``ValueError`` when fewer than ``min_items`` candidates exist.
    """
    start = _parse_bound(window_start)
    end = _parse_bound(window_end)
    if end is None:
        row = conn.execute(
            "SELECT MAX(m) AS m FROM ("
            "SELECT MAX(created_at) AS m FROM post "
            "UNION ALL SELECT MAX(created_at) FROM comment)"
        ).fetchone()
        if row["m"] is None:
            raise ValueError(
                "cannot derive window: post/comment tables contain no rows"
            )
        end = datetime.strptime(row["m"], _DT_FMT)
    if start is None:
        start = end - timedelta(hours=WINDOW_HOURS)
    ws, we = _fmt(start), _fmt(end)

    posts = [
        dict(row, kind="post")
        for row in conn.execute(
            "SELECT id, title, user, subdeaddit_name, created_at, model, content "
            "FROM post WHERE model LIKE :marker "
            "AND created_at >= :ws AND created_at < :we ORDER BY id",
            {"marker": AGENT_MARKER_LIKE, "ws": ws, "we": we},
        )
    ]
    comments = [
        dict(row, kind="comment")
        for row in conn.execute(
            "SELECT id, content, user, post_id, created_at, model "
            "FROM comment WHERE model LIKE :marker "
            "AND created_at >= :ws AND created_at < :we ORDER BY id",
            {"marker": AGENT_MARKER_LIKE, "ws": ws, "we": we},
        )
    ]
    # Fixed kind order: 'comment' sorts before 'post'.
    candidates = sorted(comments + posts, key=lambda c: (c["kind"], c["id"]))
    if len(candidates) < min_items:
        raise ValueError(
            f"only {len(candidates)} agent candidates in window, need {min_items}"
        )
    rng = random.Random(seed)
    picked = sorted(rng.sample(range(len(candidates)), min(min_items, len(candidates))))
    selected = [candidates[i] for i in picked]

    lines = [
        "# AC-P3 Reviewer Sampling Packet",
        "",
        f"- Window: [{ws}, {we})",
        f"- Seed: {seed}",
        f"- Candidate pool: {len(candidates)} agent items",
        "- Selection method: deterministic random.Random(seed) sample of "
        "(kind, id)-sorted candidate indices, re-sorted ascending",
        "",
        "## RUBRIC (score each dimension 0-2)",
        "",
    ]
    for i, dim in enumerate(_RUBRIC_DIMENSIONS, 1):
        lines.append(f"{i}. {dim}: ____/2")
    lines += ["", "### Red flags (check any that apply)", ""]
    lines += [f"- [ ] {flag}" for flag in _REDFLAGS]
    lines += ["", "## Scoring sheet", ""]

    for n, item in enumerate(selected, 1):
        if item["kind"] == "post":
            snippet = item["title"] or item.get("content")
            link = f"/d/{item['subdeaddit_name']}"
            extra = ""
        else:
            snippet = item["content"]
            link = f"(comment on post_id={item['post_id']})"
            extra = f", post_id={item['post_id']}"
        lines.append(
            f"{n}. id={item['id']} kind={item['kind']} author={item['user']} "
            f"subdeaddit={item.get('subdeaddit_name', '-')}{extra} "
            f"link={link}\n   snippet: {_snippet(snippet)}\n"
            "   scores: fit=__ persona=__ originality=__ context=__ language=__ "
            "| red flags: spam-wave=[ ] near-dup=[ ] off-topic=[ ]"
        )
    lines.append("")
    return "\n".join(lines)
