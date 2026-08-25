"""Deterministic synthetic vote-history backfill and history seeding."""

from __future__ import annotations

import logging
import os
import random
import time
from datetime import datetime, timedelta

from flask import current_app, has_app_context
from sqlalchemy import func

from deaddit.config import Config
from deaddit.extensions import db
from deaddit.models import Comment, Post, Setting, Subdeaddit, User, Vote
from deaddit.services.content import (
    create_comment,
    create_post,
    create_subdeaddit,
    create_user,
)


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
    max_votes: int | None = None,
) -> int:
    """Create synthetic votes for one item; returns number of rows created."""
    score = int(item.score)

    # Long-tail extra votes: geometric-ish draw bounded by remaining capacity.
    k_max = (capacity - abs(score)) // 2
    if max_votes is not None:
        # Attention ceiling (Phase D5): cap total synthetic votes per item.
        k_max = min(k_max, (max_votes - abs(score)) // 2)
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
    Items whose |score| exceeds the voter capacity are reported under
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

            score = int(item.score)
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


# --- Phase D5: deterministic history seeding ---

logger = logging.getLogger(__name__)

SEED_MODEL = "seed"
_MAX_SEED_POSTS = 400
_EVENING_WEIGHT = 2.5  # hour-of-day multiplier for 18-23h
_GAP_BASE = 0.5
_GAP_U_MIN = 0.05

_FIRST_NAMES = [
    "Ava", "Bram", "Cleo", "Dmitri", "Esme", "Finn", "Greta", "Hugo",
    "Ines", "Jonas", "Kira", "Lars", "Mira", "Nils", "Odette", "Pavel",
    "Quinn", "Rosa", "Sven", "Talia", "Ulf", "Vera", "Wren", "Xavi",
]
_OCCUPATIONS = [
    "cartographer", "barista", "beekeeper", "archivist", "luthier",
    "hydrologist", "actuary", "florist", "typesetter", "audiologist",
    "brewer", "locksmith",
]
_INTERESTS = [
    "urban cycling", "mycology", "chess", "sourdough", "birdwatching",
    "retro computing", "kayaking", "astronomy", "pottery", "board games",
    "trail running", "fermentation",
]
_BIOS = [
    "{occupation} by day, mostly here for the {interest} threads.",
    "Recovering {occupation}. I post about {interest} more than is healthy.",
    "{occupation} who unwinds with {interest} and long walks.",
]
_SUBDEADDIT_BANK = [
    ("askdeaddit", "The place to ask deaddit anything and everything."),
    ("quietthoughts", "Slow, reflective text posts. No hot takes."),
    ("mechanicalkeyboards", "Clacks, thocks, and artisan keycaps."),
    ("slowliving", "Deliberate living, analog hobbies, less noise."),
    ("amateurtelescopes", "Backyard astronomy for patient people."),
    ("broodencraft", "Wild yeast, long ferments, good crusts."),
    ("papermapping", "Hand-drawn maps and cartography oddities."),
    ("foundaudio", "Field recordings and tape archaeology."),
]
_TITLE_ADJECTIVES = [
    "slow", "odd", "quiet", "brutalist", "velvet", "hollow", "electric",
    "feral", "patient", "damp",
]
_TITLE_NOUNS = [
    "morning", "typewriter", "garden", "commute", "archive", "signal",
    "kettle", "horizon", "notebook", "lantern",
]
_TITLE_TEMPLATES = [
    "Why does everyone suddenly own a {noun}?",
    "{adj} {noun} appreciation post",
    "My {noun} broke and honestly I feel fine about it",
    "Unpopular opinion: every {noun} peaked in 2019",
    "What's your {adj} {noun} routine?",
    "Found a {adj} {noun} at the flea market today",
]
_POST_BODIES = [
    "I have been thinking about this for weeks and finally wrote it down. "
    "Would love to hear how other people handle a {noun} like mine.",
    "Longtime lurker, first post. My approach to the {adj} {noun} problem "
    "changed completely after last winter, details inside.",
    "No big thesis here, just sharing a {adj} little {noun} story from "
    "the weekend. Photos do not do it justice.",
    "Serious question for the regulars: is the {adj} {noun} trend over, "
    "or am I just looking in the wrong places?",
]
_COMMENT_TEXTS = [
    "This is exactly the kind of {noun} content I subscribe for.",
    "Strongly disagree about the {adj} part, but upvoting anyway.",
    "Same experience here, though my {noun} never recovered.",
    "Saving this thread. The {adj} {noun} advice is gold.",
    "Can you share more details? I am new to {noun} things.",
    "Underrated comment. The {adj} angle gets ignored too often.",
    "I laughed way too hard at this.",
    "Following this, please post an update later.",
]


def _ensure_seed_setting(key: str, default: str) -> str:
    """Persist the default when the DB row is absent; return effective value."""
    raw = Setting.get_value(key)
    if raw is None or raw == "":
        raw = Config.get(key) or default
        Setting.set_value(key, str(raw), Config.DESCRIPTIONS.get(key))
    return str(raw)


def _hour_weights() -> list[float]:
    return [(_EVENING_WEIGHT if 18 <= h <= 23 else 1.0) for h in range(24)]


def _plan_community(
    seed: int,
    existing_usernames: set[str],
    window_start: datetime,
    now: datetime,
) -> tuple[list[dict], int]:
    """Plan ~24 persona users backdated into the window; skip collisions."""
    span = max((now - window_start).total_seconds(), 1.0)
    planned: list[dict] = []
    skipped = 0
    for i, first in enumerate(_FIRST_NAMES):
        rng = random.Random(f"{seed}:user:{i}")
        username = f"{first.lower()}{i:02d}"
        if username in existing_usernames:
            skipped += 1
            continue
        occupation = rng.choice(_OCCUPATIONS)
        interests = rng.sample(_INTERESTS, k=3)
        users_rng_offset = timedelta(seconds=span * (rng.random() ** 0.7))
        planned.append(
            {
                "username": username,
                "age": 19 + rng.randrange(50),
                "gender": "Female" if i % 2 == 0 else "Male",
                "bio": rng.choice(_BIOS).format(
                    occupation=occupation, interest=interests[0]
                ),
                "interests": interests,
                "occupation": occupation,
                "education": rng.choice(["high school", "college", "self-taught"]),
                "writing_style": rng.choice(
                    ["verbose", "terse", "conversational", "dry"]
                ),
                "personality_traits": rng.sample(
                    ["curious", "stubborn", "cheerful", "anxious", "wry"], k=2
                ),
                "created_at": window_start + users_rng_offset,
            }
        )
    return planned, skipped


def _plan_subdeaddits(
    seed: int,
    existing_subdeaddits: set[str],
    window_start: datetime,
    now: datetime,
) -> tuple[list[dict], int]:
    """Plan ~6 subdeaddits from the bank; skip names that already exist."""
    span = max((now - window_start).total_seconds(), 1.0)
    planned: list[dict] = []
    skipped = 0
    for j, (name, description) in enumerate(_SUBDEADDIT_BANK[:6]):
        rng = random.Random(f"{seed}:sub:{j}")
        if name in existing_subdeaddits:
            skipped += 1
            continue
        planned.append(
            {
                "name": name,
                "description": description,
                "created_at": window_start
                + timedelta(seconds=span * rng.random()),
            }
        )
    return planned, skipped


def _plan_timeline(
    seed: int,
    days: int,
    window_start: datetime,
    now: datetime,
    usernames: list[str],
    sub_names: list[str],
) -> list[dict]:
    """Plan posts (power-law arrivals, evening-weighted hours) + comments.

    Each post dict carries its per-post comments already ordered so that
    parents precede children and every timestamp respects strict causality:
    ``post.created_at < comment.created_at <= now``, child after parent.
    """
    master = random.Random(f"d5:{seed}")
    arrivals: list[datetime] = []
    cursor = window_start
    while len(arrivals) < _MAX_SEED_POSTS:
        # Power-law inter-arrival, tail-bounded so ~14 days lands in the
        # 100-400 post band for every seed (median gap ~0.5h).
        u = _GAP_U_MIN + (1 - _GAP_U_MIN) * master.random()
        cursor = cursor + timedelta(hours=_GAP_BASE * u ** -1.2)
        if cursor > now:
            break
        arrivals.append(cursor)

    weights = _hour_weights()
    posts: list[dict] = []
    comment_ordinal = 0
    for n, arrival in enumerate(arrivals, start=1):
        rng = random.Random(f"{seed}:post:{n}")
        hour = rng.choices(range(24), weights=weights, k=1)[0]
        post_time = arrival.replace(
            hour=hour,
            minute=rng.randrange(60),
            second=rng.randrange(60),
            microsecond=0,
        )
        post_time = min(max(post_time, window_start), now)
        adjective, noun = rng.choice(_TITLE_ADJECTIVES), rng.choice(_TITLE_NOUNS)
        remaining = (now - post_time).total_seconds()
        count = 0 if remaining < 120 else min(8, int(8 * rng.random() ** 1.6))
        entry = {
            "n": n,
            "title": rng.choice(_TITLE_TEMPLATES).format(adj=adjective, noun=noun),
            "body": rng.choice(_POST_BODIES).format(adj=adjective, noun=noun),
            "author": rng.choice(usernames),
            "subdeaddit": rng.choice(sub_names),
            "created_at": post_time,
            "comments": [],
        }
        chain: list[dict] = []  # generated comment specs for this post
        for _ in range(count):
            comment_ordinal += 1
            rc = random.Random(f"{seed}:comment:{comment_ordinal}")
            nestable = [spec for spec in chain if spec["depth"] < 3]
            if nestable and rc.random() < 0.4:
                parent = rc.choice(nestable[-5:])
                depth = parent["depth"] + 1
                parent_time = parent["created_at"]
            else:
                parent, depth, parent_time = None, 1, post_time
            lo = parent_time + timedelta(seconds=60)
            if lo >= now:
                break  # no room left for strictly-causal replies
            hi = min(lo + timedelta(hours=12), now)
            created_at = lo + (hi - lo) * rc.random()
            spec = {
                "m": comment_ordinal,
                "author": rc.choice(usernames),
                "text": rc.choice(_COMMENT_TEXTS).format(adj=adjective, noun=noun),
                "created_at": created_at,
                "depth": depth,
                "parent": parent,
            }
            chain.append(spec)
            entry["comments"].append(spec)
        posts.append(entry)
    return posts


def _persist_community(planned_users: list[dict], planned_subs: list[dict]) -> None:
    for spec in planned_users:
        create_user(model=SEED_MODEL, **spec)
    for spec in planned_subs:
        create_subdeaddit(post_types=["text"], **spec)


def _persist_content(posts: list[dict]) -> tuple[list[int], list[int]]:
    """Create posts then their nested comments; returns seeded id lists."""
    post_ids: list[int] = []
    comment_ids: list[int] = []
    for entry in posts:
        post = create_post(
            title=entry["title"],
            content=entry["body"],
            user=entry["author"],
            subdeaddit=entry["subdeaddit"],
            model=SEED_MODEL,
            created_at=entry["created_at"],
        )
        post_ids.append(post.id)
        for spec in entry["comments"]:
            parent_id = spec["parent"]["id"] if spec["parent"] else None
            comment = create_comment(
                post_id=post.id,
                content=spec["text"],
                user=spec["author"],
                parent_id=parent_id,
                model=SEED_MODEL,
                created_at=spec["created_at"],
            )
            spec["id"] = comment.id
            comment_ids.append(comment.id)
    return post_ids, comment_ids


def _vote_pass(
    seed: int,
    p: float,
    vote_max: int,
    post_ids: list[int],
    comment_ids: list[int],
    batch_size: int,
) -> int:
    """Bernoulli(p) per seeded item; synthesize votes via _backfill_item."""
    user_count = db.session.query(func.count(User.username)).scalar() or 0
    capacity = user_count - 1
    if capacity <= 0:
        return 0
    usernames, weights = _activity_weights()
    voter_pool = list(zip(usernames, weights, strict=True))
    votes_created = 0
    pending = 0
    for kind, ids in (("post", post_ids), ("comment", comment_ids)):
        for ordinal, item_id in enumerate(ids, start=1):
            hit_rng = random.Random(f"{seed}:vote:{kind}:{ordinal}")
            if hit_rng.random() >= p:
                continue
            item = db.session.get(Post if kind == "post" else Comment, item_id)
            if item is None:
                continue
            votes_rng = random.Random(f"{seed}:votes:{kind}:{ordinal}")
            # Plausible target attention: long-tail positive, small negative
            # tail, bounded by both the attention ceiling and the voter pool.
            bound = min(vote_max, capacity)
            target = int(bound * votes_rng.random() ** 3)
            if votes_rng.random() < 0.08:
                target = -(1 + int(4 * votes_rng.random()))
            item.score = target
            votes_created += _backfill_item(
                votes_rng,
                item,
                kind,
                capacity,
                voter_pool,
                dry_run=False,
                max_votes=vote_max,
            )
            pending += 1
            if pending >= batch_size:
                pending = 0
                db.session.commit()
    db.session.commit()
    return votes_created


def seed_history(
    days=14,
    seed=42,
    batch_size=500,
    dry_run=False,
    allow_production=False,
    now=None,
) -> dict:
    """Deterministically fabricate a plausible content history.

    Creates users/subdeaddits on a fresh install, then power-law post
    arrivals with evening-weighted hours and nested comments across the
    ``[now - days, now]`` window, all via the content service with
    ``model="seed"`` provenance. A Bernoulli vote pass reuses the D1
    backfill machinery with a per-item attention ceiling. Refuses to run
    against the production database unless allow_production=True.
    """
    started = time.perf_counter()
    if (
        not allow_production
        and has_app_context()
        and _resolves_to_production(
            current_app.config.get("SQLALCHEMY_DATABASE_URI"),
            current_app.instance_path,
        )
    ):
        raise RuntimeError(
            "refusing to seed history into production without allow_production=True"
        )

    now = now or _now()
    window_start = now - timedelta(days=days)

    vote_max = int(_ensure_seed_setting("SEED_VOTE_MAX", "150"))
    probability = float(_ensure_seed_setting("SEED_VOTE_PROBABILITY", "1.0"))
    decay_days = float(_ensure_seed_setting("SEED_DECAY_DAYS", "30"))

    anchor_raw = Setting.get_value("SEED_ANCHOR_AT")
    if anchor_raw:
        anchor = datetime.fromisoformat(str(anchor_raw))
    else:
        anchor = now
        if not dry_run:
            Config.set("SEED_ANCHOR_AT", now.isoformat())

    elapsed_days = max((now - anchor).total_seconds(), 0.0) / 86400.0
    p = (
        probability * max(0.0, 1.0 - elapsed_days / decay_days)
        if decay_days > 0
        else 0.0
    )
    if p <= 0:
        logger.warning(
            "SEED_VOTE_PROBABILITY decayed to 0; no fabricated votes written"
        )

    fresh_install = (db.session.query(func.count(User.username)).scalar() or 0) == 0
    existing_usernames = {
        row[0] for row in db.session.query(User.username).all()
    }
    existing_subs = {
        row[0] for row in db.session.query(Subdeaddit.name).all()
    }

    planned_users, skipped_users = _plan_community(
        seed, existing_usernames, window_start, now
    ) if fresh_install else ([], 0)
    planned_subs, skipped_subs = _plan_subdeaddits(
        seed, existing_subs, window_start, now
    )

    author_pool = sorted(existing_usernames | {u["username"] for u in planned_users})
    sub_pool = sorted(existing_subs | {s["name"] for s in planned_subs})
    planned_posts = (
        _plan_timeline(seed, days, window_start, now, author_pool, sub_pool)
        if author_pool and sub_pool
        else []
    )

    report = {
        "users_created": len(planned_users),
        "subdeaddits_created": len(planned_subs),
        "posts_created": sum(1 for _ in planned_posts),
        "comments_created": sum(len(e["comments"]) for e in planned_posts),
        "votes_created": 0,
        "skipped_existing_users": skipped_users,
        "skipped_existing_subdeaddits": skipped_subs,
        "window_days": days,
        "seed": seed,
        "vote_probability_effective": p,
        "anchor": anchor.isoformat(),
        "dry_run": dry_run,
        "elapsed_seconds": 0.0,
    }

    if dry_run:
        report["projected"] = {
            "users": report["users_created"],
            "subdeaddits": report["subdeaddits_created"],
            "posts": report["posts_created"],
            "comments": report["comments_created"],
        }
        report["elapsed_seconds"] = round(time.perf_counter() - started, 4)
        return report

    _persist_community(planned_users, planned_subs)
    post_ids, comment_ids = _persist_content(planned_posts)

    if p > 0:
        report["votes_created"] = _vote_pass(
            seed, p, vote_max, post_ids, comment_ids, batch_size
        )

    from deaddit.dynamics.karma import recompute_scores_and_karma

    recompute_scores_and_karma()
    db.session.commit()

    report["elapsed_seconds"] = round(time.perf_counter() - started, 4)
    return report
