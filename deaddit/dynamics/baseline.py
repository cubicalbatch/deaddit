"""Read-only baseline observability for simulated-voting rollout.

The public contract is :func:`compute_report`: it reads an already-open SQLite
connection and returns a JSON-serializable report with stable, deterministic
ordering.  It never imports Flask or uses the application's write-capable
SQLAlchemy session.  :func:`connect_ro` opens the database with SQLite's
``mode=ro`` URI and ``query_only`` safeguard, so the CLI cannot create or alter
rows.

Run outcome buckets are mutually exclusive:

* ``content-producing``: a run with at least one successful content write
  (``create_post``, ``create_image_post``, ``create_website``, or
  ``create_comment``), unless the run itself is failed;
* ``vote-only``: a non-failed run with no successful content write and at least
  one successful ``vote`` call;
* ``browse-only``: every other non-failed run, including an idle run with no
  successful tool calls;
* ``failed``: a run whose stored status is ``failed``.  A failed run is always
  counted here even if it persisted a successful tool call before failing.

A vote-tool attempt is every stored ``ToolCall(name='vote')``.  A successful
call has ``ok`` true.  Durable rows, direction, and source are measured from
canonical ``Vote`` rows.  The current schema does not retain a history of
same-row direction switches, so ``same_target_source_collisions`` deliberately
reports *excess calls beyond one durable voter/target row* for repeated
target/source groups as a conservative collision signal; it does not alter or
infer vote state.

Deterministic fixture/snapshot seed
-----------------------------------
Later simulator tests should build a fresh SQLite file from the ORM schema,
insert fixtures in explicit order using seed ``20260828`` (or another named,
recorded seed), and use fixed UTC ``created_at`` values rather than wall-clock
time.  Snapshot the resulting file only after committing and checkpointing its
WAL.  Pass that immutable copy to ``deaddit dynamics baseline-report --db``
(and, when replaying a historical point, ``--as-of``).  Do not seed the
application's ``instance/deaddit.db``; test fixtures and snapshots are
project-owned temporary files.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from deaddit.dynamics.ranking import post_rank_key

_REPORT_VERSION = 1
_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
_DATETIME_SECONDS_FORMAT = "%Y-%m-%d %H:%M:%S"
_REPO_ROOT = Path(__file__).resolve().parents[2]

_CONTENT_TOOLS = frozenset(
    {"create_post", "create_image_post", "create_website", "create_comment"}
)


def connect_ro(db_path: str | None = None) -> sqlite3.Connection:
    """Open a deaddit SQLite database in a strictly read-only mode."""
    if db_path is None:
        db_path = str(_REPO_ROOT / "instance" / "deaddit.db")
    path = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("T", " ", 1)
    if text.endswith("Z"):
        text = text[:-1]
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        for fmt in (_DATETIME_FORMAT, _DATETIME_SECONDS_FORMAT):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    return None


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.strftime(_DATETIME_FORMAT)


def _timestamp(value: object) -> str:
    parsed = _parse_datetime(value)
    return _format_datetime(parsed) or ""


def _max_timestamp(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute(
        """
        SELECT MAX(value) AS value FROM (
            SELECT started_at AS value FROM agent_run
            UNION ALL SELECT finished_at FROM agent_run
            UNION ALL SELECT created_at FROM tool_call
            UNION ALL SELECT created_at FROM llm_usage
            UNION ALL SELECT created_at FROM post
            UNION ALL SELECT created_at FROM comment
            UNION ALL SELECT created_at FROM vote
        )
        """
    ).fetchone()
    return _parse_datetime(row["value"] if row else None)


def _token_usage(value: object) -> tuple[int, int, int]:
    usage = _json_object(value)
    prompt = usage.get("prompt_tokens", usage.get("prompt", 0))
    completion = usage.get("completion_tokens", usage.get("completion", 0))
    total = usage.get("total_tokens", usage.get("total"))
    try:
        prompt_i = max(0, int(prompt or 0))
    except (TypeError, ValueError):
        prompt_i = 0
    try:
        completion_i = max(0, int(completion or 0))
    except (TypeError, ValueError):
        completion_i = 0
    try:
        total_i = max(0, int(total)) if total is not None else prompt_i + completion_i
    except (TypeError, ValueError):
        total_i = prompt_i + completion_i
    return prompt_i, completion_i, total_i


def _target_key(target: str, target_id: int) -> str:
    return f"{target}:{target_id}"


def _parse_vote_target(arguments: object) -> tuple[str, int, int | None] | None:
    args = _json_object(arguments)
    target = args.get("target_type", args.get("target"))
    target_id = args.get("target_id", args.get("id"))
    direction = args.get("direction", args.get("value"))
    if target not in {"post", "comment"}:
        return None
    try:
        target_id_i = int(target_id)
    except (TypeError, ValueError):
        return None
    try:
        direction_i = int(direction) if direction is not None else None
    except (TypeError, ValueError):
        direction_i = None
    return target, target_id_i, direction_i


def _summary(values: list[float | int]) -> dict[str, float | int | None]:
    """Return deterministic nearest-rank descriptive statistics."""
    if not values:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "p95": None,
        }
    ordered = sorted(values)
    count = len(ordered)
    rank = max(1, math.ceil(count * 0.95)) - 1
    mean = sum(ordered) / count
    median = (
        ordered[(count - 1) // 2]
        if count % 2
        else (ordered[count // 2 - 1] + ordered[count // 2]) / 2
    )
    return {
        "count": count,
        "min": ordered[0],
        "max": ordered[-1],
        "mean": round(mean, 6),
        "median": median,
        "p95": ordered[rank],
    }


def _target_distribution(rows: list[sqlite3.Row], counts: dict[int, int]) -> dict:
    values = [counts.get(int(row["id"]), 0) for row in rows]
    zero_count = sum(value == 0 for value in values)
    return {
        "target_count": len(rows),
        "votes_total": sum(values),
        "zero_vote_count": zero_count,
        "zero_vote_fraction": round(zero_count / len(values), 6) if values else None,
        "votes_per_target": _summary(values),
        "counts": [
            {"target_id": int(row["id"]), "votes": counts.get(int(row["id"]), 0)}
            for row in rows
        ],
    }


def _concentration(entries: dict[str, int], *, top_k: int = 10) -> dict:
    total = sum(entries.values())
    ranked = sorted(entries.items(), key=lambda item: (-item[1], item[0]))
    return {
        "total_votes": total,
        "unique": len(entries),
        "hhi": round(sum((count / total) ** 2 for count in entries.values()), 6)
        if total
        else None,
        "top": [
            {
                "key": key,
                "votes": count,
                "share": round(count / total, 6) if total else None,
            }
            for key, count in ranked[:top_k]
        ],
    }


def _load_runs(conn: sqlite3.Connection) -> tuple[dict, list[dict]]:
    runs = conn.execute(
        "SELECT id, status, persona_username, token_usage FROM agent_run ORDER BY id"
    ).fetchall()
    calls = conn.execute(
        "SELECT id, run_id, name, arguments, ok FROM tool_call ORDER BY id"
    ).fetchall()
    calls_by_run: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for call in calls:
        calls_by_run[int(call["run_id"])].append(call)

    names = ("content-producing", "vote-only", "browse-only", "failed")
    buckets = {
        name: {
            "runs": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        for name in names
    }
    call_counts = {"attempts": 0, "successful_calls": 0, "failed_calls": 0}
    parsed_calls: list[dict] = []
    for run in runs:
        run_calls = calls_by_run.get(int(run["id"]), [])
        successful_names = {str(call["name"]) for call in run_calls if bool(call["ok"])}
        if run["status"] == "failed":
            bucket = "failed"
        elif successful_names & _CONTENT_TOOLS:
            bucket = "content-producing"
        elif any(call["name"] == "vote" and bool(call["ok"]) for call in run_calls):
            bucket = "vote-only"
        else:
            bucket = "browse-only"
        prompt, completion, total = _token_usage(run["token_usage"])
        bucket_data = buckets[bucket]
        bucket_data["runs"] += 1
        bucket_data["prompt_tokens"] += prompt
        bucket_data["completion_tokens"] += completion
        bucket_data["total_tokens"] += total
        for call in run_calls:
            if call["name"] != "vote":
                continue
            call_counts["attempts"] += 1
            if bool(call["ok"]):
                call_counts["successful_calls"] += 1
            else:
                call_counts["failed_calls"] += 1
            target = _parse_vote_target(call["arguments"])
            if target is not None:
                parsed_calls.append(
                    {
                        "call_id": int(call["id"]),
                        "run_id": int(run["id"]),
                        "voter": str(run["persona_username"]),
                        "target": target[0],
                        "target_id": target[1],
                        "direction": target[2],
                        "ok": bool(call["ok"]),
                    }
                )
    total_tokens = sum(data["total_tokens"] for data in buckets.values())
    return {
        "total_runs": len(runs),
        "total_tokens": total_tokens,
        "buckets": buckets,
        "vote_tool": call_counts,
    }, parsed_calls


def _load_votes(conn: sqlite3.Connection) -> tuple[list[dict], dict[str, dict]]:
    rows = conn.execute(
        """
        SELECT id, voter, post_id, comment_id, value, source, created_at
        FROM vote ORDER BY id
        """
    ).fetchall()
    votes: list[dict] = []
    by_key: dict[str, dict] = {}
    for row in rows:
        target = "post" if row["post_id"] is not None else "comment"
        target_id = int(row["post_id"] or row["comment_id"])
        item = {
            "id": int(row["id"]),
            "voter": str(row["voter"]),
            "target": target,
            "target_id": target_id,
            "value": int(row["value"]),
            "source": str(row["source"] or "unknown"),
            "created_at": _parse_datetime(row["created_at"]),
        }
        votes.append(item)
        by_key[f"{item['voter']}|{_target_key(target, target_id)}"] = item
    return votes, by_key


def _collision_report(
    parsed_calls: list[dict], votes_by_key: dict[str, dict]
) -> tuple[list[dict], int]:
    groups: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    for call in parsed_calls:
        vote = votes_by_key.get(
            f"{call['voter']}|{_target_key(call['target'], call['target_id'])}"
        )
        source = str(vote["source"]) if vote else "unknown"
        key = (call["voter"], call["target"], call["target_id"], source)
        group = groups.setdefault(
            key,
            {
                "attempts": 0,
                "successful_calls": 0,
                "failed_calls": 0,
                "durable_rows": 1 if vote else 0,
            },
        )
        group["attempts"] += 1
        if call["ok"]:
            group["successful_calls"] += 1
        else:
            group["failed_calls"] += 1

    collisions = []
    for (voter, target, target_id, source), group in sorted(groups.items()):
        collision_calls = (
            max(0, group["attempts"] - group["durable_rows"])
            if group["attempts"] > 1
            else 0
        )
        if collision_calls:
            collisions.append(
                {
                    "voter": voter,
                    "target": target,
                    "target_id": target_id,
                    "source": source,
                    **group,
                    "collision_calls": collision_calls,
                }
            )
    return collisions, sum(item["collision_calls"] for item in collisions)


def _hot_feed(rows: list[sqlite3.Row], as_of: datetime, top_k: int) -> list[dict]:
    ranked = []
    for row in rows:
        created = _parse_datetime(row["created_at"])
        if created is None:
            continue
        score = int(row["score"] or 0)
        key = post_rank_key("hot", score=score, created_at=created, now=as_of)
        ranked.append((key, int(row["id"]), row))
    ranked.sort(key=lambda item: (-item[0], -item[1]))
    output = []
    for position, (key, _post_id, row) in enumerate(ranked[:top_k], start=1):
        output.append(
            {
                "rank": position,
                "post_id": int(row["id"]),
                "title": row["title"],
                "author": row["user"],
                "community": row["subdeaddit_name"],
                "score": int(row["score"] or 0),
                "vote_count": int(row["vote_count"] or 0),
                "created_at": _timestamp(row["created_at"]),
                "hot_key": round(key, 9),
            }
        )
    return output


def compute_report(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
    top_k: int = 10,
) -> dict:
    """Compute the complete Phase 0 baseline report without writing state.

    ``as_of`` is a UTC datetime string (ISO 8601 or SQLite's datetime form).
    When omitted, it is derived from the latest timestamp in the database, not
    from the wall clock, which makes repeated runs byte-identical.  All output
    lists are explicitly sorted and all floating-point display values are
    rounded to six decimal places (hot ranking keys to nine).
    """
    if top_k < 0:
        raise ValueError("top_k must be non-negative")
    if as_of:
        snapshot = _parse_datetime(as_of)
        if snapshot is None:
            raise ValueError("as_of must be an ISO 8601 datetime")
    else:
        snapshot = _max_timestamp(conn)
    if snapshot is None:
        snapshot = datetime(1970, 1, 1)

    posts = conn.execute(
        """
        SELECT id, title, user, subdeaddit_name, score, vote_count, created_at
        FROM post ORDER BY id
        """
    ).fetchall()
    comments = conn.execute(
        """
        SELECT id, post_id, user, score, vote_count, created_at
        FROM comment ORDER BY id
        """
    ).fetchall()
    votes, votes_by_key = _load_votes(conn)
    runs, parsed_calls = _load_runs(conn)

    post_vote_counts: dict[int, int] = defaultdict(int)
    comment_vote_counts: dict[int, int] = defaultdict(int)
    community_votes: dict[str, int] = defaultdict(int)
    author_votes: dict[str, int] = defaultdict(int)
    latencies: list[float] = []
    post_by_id = {int(row["id"]): row for row in posts}
    comment_by_id = {int(row["id"]): row for row in comments}
    for vote in votes:
        target_row = (
            post_by_id.get(vote["target_id"])
            if vote["target"] == "post"
            else comment_by_id.get(vote["target_id"])
        )
        if target_row is None:
            continue
        if vote["target"] == "post":
            post_vote_counts[vote["target_id"]] += 1
            community = target_row["subdeaddit_name"]
        else:
            comment_vote_counts[vote["target_id"]] += 1
            parent = post_by_id.get(int(target_row["post_id"]))
            community = parent["subdeaddit_name"] if parent else "unknown"
        author_votes[str(target_row["user"])] += 1
        community_votes[str(community)] += 1
        created = _parse_datetime(target_row["created_at"])
        arrived = vote["created_at"]
        if created is not None and arrived is not None:
            latencies.append(round((arrived - created).total_seconds(), 6))

    by_hour: dict[tuple[str, str], int] = defaultdict(int)
    by_day: dict[tuple[str, str], int] = defaultdict(int)
    direction_attempts: dict[str, int] = defaultdict(int)
    for call in parsed_calls:
        direction = call["direction"]
        if direction == 1:
            direction_attempts["up"] += 1
        elif direction == -1:
            direction_attempts["down"] += 1
    for vote in votes:
        arrived = vote["created_at"]
        if arrived is None:
            continue
        persona = vote["voter"]
        by_hour[(arrived.strftime("%Y-%m-%d %H:00:00"), persona)] += 1
        by_day[(arrived.strftime("%Y-%m-%d"), persona)] += 1

    direction = {
        "up": sum(vote["value"] == 1 for vote in votes),
        "down": sum(vote["value"] == -1 for vote in votes),
    }
    source_split: dict[str, int] = defaultdict(int)
    for vote in votes:
        source_split[vote["source"]] += 1
    source_split = dict(sorted(source_split.items()))
    collisions, collision_count = _collision_report(parsed_calls, votes_by_key)
    hot_rows = posts

    return {
        "schema_version": _REPORT_VERSION,
        "snapshot_at": _format_datetime(snapshot),
        "runs": runs,
        "votes": {
            "tool_attempts": runs["vote_tool"]["attempts"],
            "successful_tool_calls": runs["vote_tool"]["successful_calls"],
            "failed_tool_calls": runs["vote_tool"]["failed_calls"],
            "durable_vote_rows": len(votes),
            "durable_voters": len({vote["voter"] for vote in votes}),
            "durable_targets": len(
                {_target_key(vote["target"], vote["target_id"]) for vote in votes}
            ),
            "direction_split": direction,
            "attempt_direction_split": dict(sorted(direction_attempts.items())),
            "source_split": source_split,
            "same_target_source_collisions": collisions,
            "collision_call_count": collision_count,
        },
        "distributions": {
            "posts": _target_distribution(posts, post_vote_counts),
            "comments": _target_distribution(comments, comment_vote_counts),
            "vote_arrival_latency_seconds": _summary(latencies),
            "persona_vote_rate": {
                "hour": [
                    {"bucket": bucket, "persona": persona, "votes": count}
                    for (bucket, persona), count in sorted(by_hour.items())
                ],
                "day": [
                    {"bucket": bucket, "persona": persona, "votes": count}
                    for (bucket, persona), count in sorted(by_day.items())
                ],
            },
            "upvote_share": round(direction["up"] / len(votes), 6) if votes else None,
            "downvote_share": round(direction["down"] / len(votes), 6)
            if votes
            else None,
            "community_concentration": _concentration(community_votes),
            "author_concentration": _concentration(author_votes),
        },
        "hot_feed": _hot_feed(hot_rows, snapshot, top_k),
    }


def render_text(report: dict) -> str:
    """Render a stable line-oriented view of :func:`compute_report`."""
    runs = report["runs"]
    votes = report["votes"]
    distributions = report["distributions"]
    lines = [
        f"schema_version: {report['schema_version']}",
        f"snapshot_at: {report['snapshot_at']}",
        f"runs: {runs['total_runs']} tokens={runs['total_tokens']}",
    ]
    for bucket in ("content-producing", "vote-only", "browse-only", "failed"):
        values = runs["buckets"][bucket]
        lines.append(
            f"run_bucket {bucket}: runs={values['runs']} "
            f"prompt_tokens={values['prompt_tokens']} "
            f"completion_tokens={values['completion_tokens']} "
            f"total_tokens={values['total_tokens']}"
        )
    lines.append(
        "vote_tools: "
        f"attempts={votes['tool_attempts']} "
        f"successful_calls={votes['successful_tool_calls']} "
        f"failed_calls={votes['failed_tool_calls']}"
    )
    lines.append(
        "durable_votes: "
        f"rows={votes['durable_vote_rows']} voters={votes['durable_voters']} "
        f"targets={votes['durable_targets']} collisions={votes['collision_call_count']}"
    )
    lines.append(
        f"direction_split: {json.dumps(votes['direction_split'], sort_keys=True)}"
    )
    lines.append(f"source_split: {json.dumps(votes['source_split'], sort_keys=True)}")
    for kind in ("posts", "comments"):
        item = distributions[kind]
        lines.append(
            f"{kind}: targets={item['target_count']} votes={item['votes_total']} "
            f"zero_vote_fraction={item['zero_vote_fraction']} "
            f"distribution={json.dumps(item['votes_per_target'], sort_keys=True)}"
        )
    lines.append(
        "vote_arrival_latency_seconds: "
        + json.dumps(distributions["vote_arrival_latency_seconds"], sort_keys=True)
    )
    lines.append(
        "persona_vote_rate: "
        + json.dumps(distributions["persona_vote_rate"], sort_keys=True)
    )
    lines.append(
        "shares: "
        + json.dumps(
            {
                "upvote": distributions["upvote_share"],
                "downvote": distributions["downvote_share"],
            },
            sort_keys=True,
        )
    )
    lines.append(
        "community_concentration: "
        + json.dumps(distributions["community_concentration"], sort_keys=True)
    )
    lines.append(
        "author_concentration: "
        + json.dumps(distributions["author_concentration"], sort_keys=True)
    )
    lines.append("same_target_source_collisions:")
    for collision in votes["same_target_source_collisions"]:
        lines.append("  " + json.dumps(collision, sort_keys=True))
    lines.append("hot_feed:")
    for item in report["hot_feed"]:
        lines.append("  " + json.dumps(item, sort_keys=True))
    return "\n".join(lines)
