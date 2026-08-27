"""Read-only agent tools (slice S2).

All tiers (including lurker), rate class READ. Reads query the ORM directly;
writes are not allowed here.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from flask import current_app
from pydantic import BaseModel, Field
from sqlalchemy import func

from deaddit.agents.registry import (
    AutonomyTier,
    RateClass,
    Tool,
    ToolContext,
    register,
)
from deaddit.dynamics.inbox import get_inbox, mark_inbox_read
from deaddit.extensions import db
from deaddit.images.storage import media_root, resolve_media_path
from deaddit.llm import describe_image, is_vision_capable
from deaddit.models import Comment, Post, PostImage, Subdeaddit, User

logger = logging.getLogger(__name__)

_MAX_COMMENT_DEPTH = 6


def _utcnow() -> datetime:
    return datetime.utcnow()


def _excerpt(text: str | None, limit: int) -> str:
    """Whitespace-collapsed, length-capped excerpt of a longer text."""
    return " ".join((text or "").split())[:limit]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class BrowseFeedArgs(BaseModel):
    subdeaddit: str | None = None
    sort: Literal["new", "hot", "top"] = "new"
    limit: int = Field(default=10, ge=1, le=25)


def _age_hours(created: datetime | None) -> float:
    if created is None:
        return 0.0
    return max((_utcnow() - created).total_seconds() / 3600, 0.0)


def _image_post_ids(post_ids: list[int]) -> set[int]:
    """IDs among ``post_ids`` that have a stored image, in one query.

    Used by summary views so that surfacing ``has_image`` never costs an
    extra per-post query (PostImage.post_id is its primary key).
    """
    if not post_ids:
        return set()
    rows = (
        db.session.query(PostImage.post_id)
        .filter(PostImage.post_id.in_(post_ids))
        .all()
    )
    return {row[0] for row in rows}


def _has_image(post: Post, image_post_ids: set[int]) -> bool:
    """A summary never claims an image for a removed or imageless post."""
    return post.id in image_post_ids and not post.removed


def _post_summary(post: Post, comment_count: int, has_image: bool) -> dict:
    return {
        "id": post.id,
        "title": post.title,
        "subdeaddit": post.subdeaddit_name,
        "author": post.user,
        "score": post.score,
        "age_hours": round(_age_hours(post.created_at), 2),
        "comment_count": comment_count,
        "excerpt": _excerpt(post.content, 200),
        "has_image": has_image,
    }


def _browse_feed(ctx: ToolContext, params: BrowseFeedArgs) -> dict:
    user = db.session.get(User, ctx.user_username)
    subscriptions = (
        list((user.agent_state or {}).get("subscriptions") or []) if user else []
    )
    targets: list[str] = []
    if params.subdeaddit is not None:
        targets.append(params.subdeaddit)
    else:
        # Bias toward subscribed communities first.
        for name in subscriptions:
            if name not in targets:
                targets.append(name)

    pool: dict[int, Post] = {}
    for name in targets:
        rows = (
            Post.query.filter_by(subdeaddit_name=name)
            .order_by(Post.created_at.desc())
            .limit(100)
            .all()
        )
        for post in rows:
            pool[post.id] = post

    def _sort_key(post: Post):
        age = max(_age_hours(post.created_at), 1.0)
        return {
            "new": post.created_at or datetime.min,
            "top": -post.score,
            "hot": -(post.score + 1) / age,
        }[params.sort]

    posts = sorted(pool.values(), key=_sort_key, reverse=params.sort == "new")[
        : params.limit
    ]

    counts: dict[int, int] = {}
    if posts:
        counts = dict(
            db.session.query(Comment.post_id, func.count(Comment.id))
            .filter(Comment.post_id.in_([p.id for p in posts]))
            .group_by(Comment.post_id)
            .all()
        )
    image_post_ids = _image_post_ids([p.id for p in posts])
    result: dict[str, object] = {
        "posts": [
            _post_summary(p, counts.get(p.id, 0), _has_image(p, image_post_ids))
            for p in posts
        ]
    }
    if not posts:
        target_name = params.subdeaddit or "this feed"
        result["hint"] = (
            f"No posts found in {target_name}. This community is currently empty or quiet. "
            "You are welcome to kick off the first discussion by creating a post with create_post!"
        )
    elif len(posts) <= 2:
        result["hint"] = (
            "This community has very few posts. Starting a new discussion with "
            "create_post is welcome if you have a relevant topic."
        )
    return result


class ReadPostArgs(BaseModel):
    post_id: int = Field(gt=0)
    comment_sort: Literal["top", "new"] = "top"
    reply_limit: int = Field(default=10, ge=1, le=30)


def _comment_order(sort: str):
    if sort == "new":
        return (Comment.created_at.desc(), Comment.id.desc())
    return (Comment.score.desc(), Comment.created_at.asc())


def _comment_node(comment: Comment) -> dict:
    return {
        "id": comment.id,
        "parent_id": comment.parent_id,
        "author": comment.user,
        "content": (comment.content or "")[:500],
        "score": comment.score,
        "created_at": _iso(comment.created_at),
        "replies": [],
    }


def _build_comment_tree(post_id: int, params: ReadPostArgs) -> list[dict]:
    order = _comment_order(params.comment_sort)
    roots = (
        Comment.query.filter_by(post_id=post_id, parent_id=None)
        .order_by(*order)
        .limit(params.reply_limit)
        .all()
    )
    top_level: list[dict] = []
    frontier: list[tuple[dict, int]] = []

    for comment in roots:
        node = _comment_node(comment)
        top_level.append(node)
        frontier.append((node, 1))

    while frontier:
        parent_node, depth = frontier.pop()
        if depth >= _MAX_COMMENT_DEPTH:
            continue
        children = (
            Comment.query.filter_by(parent_id=parent_node["id"])
            .order_by(*order)
            .limit(params.reply_limit)
            .all()
        )
        for child in children:
            child_node = _comment_node(child)
            parent_node["replies"].append(child_node)
            frontier.append((child_node, depth + 1))
    return top_level


def _load_image_description(ctx: ToolContext, post: Post) -> dict:
    """Bounded image description for a non-removed image post (plan 5B).

    Vision is attempted only when the reading agent's already-resolved
    endpoint/model has a stored ``supports_vision=True`` verdict. False,
    unknown, a missing file, or any nested vision failure all degrade to
    the complete stored source prompt - reading a post must never fail
    because image description failed.
    """
    image = post.image
    fallback = {
        "present": True,
        "description": image.source_prompt,
        "description_source": "generation_prompt",
    }
    if not (ctx.llm_api_url and ctx.llm_model):
        return fallback
    try:
        if not is_vision_capable(ctx.llm_api_url, ctx.llm_model):
            return fallback
        read_timeout = 30.0
        if ctx.deadline is not None:
            remaining = ctx.deadline.remaining()
            if remaining <= 0:
                return fallback
            read_timeout = min(read_timeout, remaining)
        root = media_root(current_app)
        image_bytes = resolve_media_path(root, image.original_path).read_bytes()
        description = describe_image(
            image_bytes,
            api_url=ctx.llm_api_url,
            model=ctx.llm_model,
            api_key=ctx.llm_api_key,
            agent=ctx.user_username,
            read_timeout=read_timeout,
        )
    except Exception:
        logger.warning(
            "image description failed for post %s; falling back to source prompt",
            post.id,
            exc_info=True,
        )
        return fallback
    return {
        "present": True,
        "description": description,
        "description_source": "vision",
    }


def _read_post(ctx: ToolContext, params: ReadPostArgs) -> dict:
    post = db.session.get(Post, params.post_id)
    if post is None:
        return {"ok": False, "error": "post not found"}
    post_dict: dict = {
        "id": post.id,
        "title": post.title,
        "subdeaddit": post.subdeaddit_name,
        "author": post.user,
        "content": (post.content or "")[:2000],
        "score": post.score,
        "created_at": _iso(post.created_at),
        "comments": _build_comment_tree(post.id, params),
    }
    if post.image is not None and not post.removed:
        post_dict["image"] = _load_image_description(ctx, post)
    return {"ok": True, "post": post_dict}


class SearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    type: Literal["post", "subdeaddit", "user"] = "post"
    limit: int = Field(default=10, ge=1, le=15)


def _search(ctx: ToolContext, params: SearchArgs) -> dict:
    needle = f"%{params.query}%"
    if params.type == "post":
        rows = (
            Post.query.filter(
                db.or_(Post.title.ilike(needle), Post.content.ilike(needle))
            )
            .order_by(Post.created_at.desc())
            .limit(params.limit)
            .all()
        )
        image_post_ids = _image_post_ids([p.id for p in rows])
        results = [
            {
                "id": p.id,
                "title": p.title,
                "subdeaddit": p.subdeaddit_name,
                "excerpt": _excerpt(p.content, 200),
                "score": p.score,
                "has_image": _has_image(p, image_post_ids),
            }
            for p in rows
        ]
    elif params.type == "subdeaddit":
        rows = (
            Subdeaddit.query.filter(
                db.or_(
                    Subdeaddit.name.ilike(needle),
                    Subdeaddit.description.ilike(needle),
                )
            )
            .order_by(Subdeaddit.name.asc())
            .limit(params.limit)
            .all()
        )
        results = [
            {"name": s.name, "description": _excerpt(s.description, 200)} for s in rows
        ]
    else:
        rows = (
            User.query.filter(
                db.or_(User.username.ilike(needle), User.bio.ilike(needle))
            )
            .order_by(User.username.asc())
            .limit(params.limit)
            .all()
        )
    out: dict[str, object] = {"results": results}
    if not results and params.type == "subdeaddit":
        popular = [
            s.name
            for s in Subdeaddit.query.order_by(Subdeaddit.name.asc()).limit(8).all()
        ]
        pop_list = ", ".join(popular) if popular else ""
        out["hint"] = (
            f"No subdeaddits matched '{params.query}'. "
            f"Some existing communities you can participate in include: {pop_list}."
        )
    elif not results:
        out["hint"] = f"No {params.type} results found matching '{params.query}'."
    return out


class ViewInboxArgs(BaseModel):
    unread_only: bool = True


def _view_inbox(ctx: ToolContext, params: ViewInboxArgs) -> dict:
    data = get_inbox(ctx.user_username, unread_only=params.unread_only, limit=50)
    mark_inbox_read(
        ctx.user_username, ids=[item["id"] for item in data["items"]]
    )  # commits internally
    return {"items": data["items"], "unread": data["unread"]}


class ViewProfileArgs(BaseModel):
    username: str | None = None


def _view_profile(ctx: ToolContext, params: ViewProfileArgs) -> dict:
    username = params.username or ctx.user_username
    user = db.session.get(User, username)
    posts = (
        Post.query.filter_by(user=username)
        .order_by(Post.created_at.desc())
        .limit(10)
        .all()
    )
    comments = (
        Comment.query.filter_by(user=username)
        .order_by(Comment.created_at.desc())
        .limit(10)
        .all()
    )
    image_post_ids = _image_post_ids([p.id for p in posts])
    return {
        "username": username,
        "bio": _excerpt(user.bio if user else None, 500),
        "exists": user is not None,
        "post_count": Post.query.filter_by(user=username).count(),
        "comment_count": Comment.query.filter_by(user=username).count(),
        "posts": [
            {
                "id": p.id,
                "title": p.title,
                "subdeaddit": p.subdeaddit_name,
                "excerpt": _excerpt(p.content, 200),
                "created_at": _iso(p.created_at),
                "has_image": _has_image(p, image_post_ids),
            }
            for p in posts
        ],
        "comments": [
            {
                "id": c.id,
                "post_id": c.post_id,
                "excerpt": _excerpt(c.content, 200),
                "created_at": _iso(c.created_at),
            }
            for c in comments
        ],
    }


register(
    Tool(
        name="browse_feed",
        description=(
            "Browse recent posts, optionally within one subdeaddit. Sort by "
            "newest, hottest (score vs. age), or top score."
        ),
        parameters=BrowseFeedArgs,
        handler=_browse_feed,
        min_tier=AutonomyTier.LURKER,
        rate_class=RateClass.READ,
    ),
)
register(
    Tool(
        name="read_post",
        description=(
            "Read a full post and its nested replies. Replies are "
            "depth-limited and bodies truncated."
        ),
        parameters=ReadPostArgs,
        handler=_read_post,
        min_tier=AutonomyTier.LURKER,
        rate_class=RateClass.READ,
    ),
)
register(
    Tool(
        name="search",
        description="Search posts, subdeaddits, or users by keyword.",
        parameters=SearchArgs,
        handler=_search,
        min_tier=AutonomyTier.LURKER,
        rate_class=RateClass.READ,
    ),
)
register(
    Tool(
        name="view_inbox",
        description=(
            "See replies to your posts and comments. Pass unread_only=false "
            "to see everything."
        ),
        parameters=ViewInboxArgs,
        handler=_view_inbox,
        min_tier=AutonomyTier.LURKER,
        rate_class=RateClass.READ,
    ),
)
register(
    Tool(
        name="view_profile",
        description=(
            "View a user's bio and recent activity. Defaults to your own "
            "profile when no username is given."
        ),
        parameters=ViewProfileArgs,
        handler=_view_profile,
        min_tier=AutonomyTier.LURKER,
        rate_class=RateClass.READ,
    ),
)
