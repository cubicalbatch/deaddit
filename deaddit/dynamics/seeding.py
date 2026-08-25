"""Deterministic synthetic vote-history backfill for legacy content."""

from __future__ import annotations

import os
import random
from datetime import datetime, timedelta

from flask import current_app, has_app_context
from sqlalchemy import func

from deaddit.extensions import db
from deaddit.models import Comment, Post, User, Vote


def _now() -> datetime:
    """Wall-clock seam so tests can pin 'now' for full determinism."""
    return datetime.utcnow()


def _activity_weights() -> tuple[list[str], list[int]]:
    """Historic activity (post + comment counts) per user, aligned lists."""
    activity: dict[str, int] = {}
    for username, count in db.session.query(Post.user, func.count(Post.id)).group_by(
        Post.user
    ):
        activity[username] = activity.get(username, 0) + count
    for username, count in db.session.query(
        Comment.user, func.count(Comment.id)
    ).group_by(Comment.user):
        activity[username] = activity.get(username, 0) + count

    usernames = [row[0] for row in db.session.query(User.username).all()]
    weights = [max(activity.get(name, 0), 0) for name in usernames]
    return usernames, weights


def _pick_voters(
    rng: random.Random,
    pool: list[tuple[str, int]],
    count: int,
) -> list[str]:
    """Weighted sampling without replacement (weights = historic activity).

    Falls back to uniform picks while the remaining pool has zero total weight.
    """
    picked: list[str] = []
    for _ in range(count):
        total = sum(weight for _, weight in pool)
        if total <= 0:
            idx = rng.randrange(len(pool))
        else:
            threshold = rng.uniform(0, total)
            acc = 0.0
            idx = len(pool) - 1
            for i, (_, weight) in enumerate(pool):
                acc += weight
                if threshold < acc:
                    idx = i
                    break
        name, weight = pool.pop(idx)
        picked.append(name)
    return picked


def _backfill_item(
    rng: random.Random,
    item: Post | Comment,
    kind: str,
    capacity: int,
    voter_pool: list[tuple[str, int]],
    dry_run: bool,
) -> int:
    """Create synthetic votes for one item; returns number of rows created."""
    score = int(item.upvote_count or 0)

    # Long-tail extra votes: geometric-ish draw bounded by remaining capacity.
    k_max = (capacity - abs(score)) // 2
    k = 0
    while k < k_max and rng.random() < 0.5:
        k += 1
    if score == 0 and k == 0 and k_max >= 1:
        # Every feasible item must receive at least one vote row (n >= 2 for
        # S == 0: one up + one down keeps SUM(value) == 0 exactly) or re-runs
        # would re-process it forever.
        k = 1

    n = abs(score) + 2 * k  # parity keeps (n + score) even => integer up-count
    up = (n + score) // 2
    down = n - up

    # Author excluded: nobody ever votes on their own content.
    pool = [(name, weight) for name, weight in voter_pool if name != item.user]
    voters = _pick_voters(rng, pool, n)
    rng.shuffle(voters)
    values = [1] * up + [-1] * down

    base = item.created_at or _now()
    window = max((_now() - base).total_seconds(), 0.0)

    for index, (voter, value) in enumerate(zip(voters, values, strict=True)):
        # Half of the votes land inside the first 20% of the age window.
        span = window * 0.2 if index < n // 2 else window
        created_at = base + timedelta(seconds=rng.uniform(0, span))
        if dry_run:
            continue
        vote = Vote(
            voter=voter,
            value=value,
            source="backfill",
            created_at=created_at,
        )
        if kind == "post":
            vote.post_id = item.id
        else:
            vote.comment_id = item.id
        db.session.add(vote)

    if not dry_run:
        item.score = score
        item.vote_count = n
        item.upvote_count = score
    return n


def _production_db_path(instance_path: str) -> str:
    return os.path.abspath(os.path.join(instance_path, "deaddit.db"))


def _resolves_to_production(uri: object, instance_path: str) -> bool:
    """True when a sqlite URI points at <instance_path>/deaddit.db."""
    prefix = "sqlite:///"
    if not isinstance(uri, str) or not uri.startswith(prefix):
        return False
    path = uri[len(prefix) :]
    if not path or path == ":memory:":
        return False
    if path.startswith("/"):
        resolved = os.path.abspath(path)
    else:
        resolved = os.path.abspath(os.path.join(instance_path, path))
    return resolved == _production_db_path(instance_path)


def backfill_history(
    batch_size=500, seed=42, dry_run=False, allow_production=False
) -> dict:
    """Backfill deterministic synthetic vote history for legacy content.

    Items with any existing Vote rows are skipped entirely (idempotency).
    Items whose |upvote_count| exceeds the voter capacity are reported under
    "unbackfilled_infeasible" and left untouched.

    Refuses to run against the production database (<instance>/deaddit.db)
    unless allow_production=True.
    """
    if (
        not allow_production
        and has_app_context()
        and _resolves_to_production(
            current_app.config.get("SQLALCHEMY_DATABASE_URI"),
            current_app.instance_path,
        )
    ):
        raise RuntimeError(
            "refusing to backfill production without allow_production=True"
        )
    report = {
        "posts_backfilled": 0,
        "comments_backfilled": 0,
        "votes_created": 0,
        "skipped_already_voted": 0,
        "unbackfilled_infeasible": [],
    }

    user_count = db.session.query(func.count(User.username)).scalar() or 0
    capacity = user_count - 1
    if capacity <= 0:
        return report
    usernames, weights = _activity_weights()
    voter_pool = list(zip(usernames, weights, strict=True))

    voted_post_ids = {
        row[0]
        for row in db.session.query(Vote.post_id).filter(Vote.post_id.isnot(None))
    }
    voted_comment_ids = {
        row[0]
        for row in db.session.query(Vote.comment_id).filter(Vote.comment_id.isnot(None))
    }

    pending = 0
    for kind, model, voted_ids in (
        ("post", Post, voted_post_ids),
        ("comment", Comment, voted_comment_ids),
    ):
        for item in model.query.order_by(model.id).all():
            if item.id in voted_ids:
                report["skipped_already_voted"] += 1
                continue

            score = int(item.upvote_count or 0)
            if abs(score) > capacity:
                report["unbackfilled_infeasible"].append(
                    {"kind": kind, "id": item.id, "score": score}
                )
                continue

            rng = random.Random(f"{kind}:{item.id}")
            created = _backfill_item(rng, item, kind, capacity, voter_pool, dry_run)
            report[f"{kind}s_backfilled"] += 1
            report["votes_created"] += created

            pending += 1
            if pending >= batch_size:
                pending = 0
                if not dry_run:
                    db.session.commit()

    if not dry_run:
        db.session.commit()
    return report
