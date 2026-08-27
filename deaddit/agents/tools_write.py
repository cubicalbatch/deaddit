"""Write and meta agent tools (slice S2).

Resolution 1: all post/comment persistence goes through
``deaddit.services.content`` with provenance stamping (``model=`` kwarg,
Resolution 9). Write tools are rate class WRITE; ``finish`` is META.
"""

from __future__ import annotations

from typing import Literal

from flask import current_app
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from deaddit.agents.registry import (
    POST_TOOL_NAMES,
    AutonomyTier,
    RateClass,
    Tool,
    ToolContext,
    image_posts_config,
    register,
)
from deaddit.dynamics.votes import cast_vote
from deaddit.extensions import db
from deaddit.images.client import generate as generate_image
from deaddit.images.storage import (
    MediaStorageError,
    delete_variants,
    download_image,
    media_root,
    store_variants,
)
from deaddit.images.types import Deadline, ImageProviderError
from deaddit.models import Comment, ImageProvider, Post, Subdeaddit, ToolCall
from deaddit.services.content import (
    ContentValidationError,
    PendingPostImage,
    create_comment,
    create_image_post,
    create_post,
    preflight_image_post,
)

#: Upper bound on how long a single image-generation attempt may run,
#: independent of (and capped by) whatever remains of the run's overall
#: deadline (ToolContext.deadline).
_IMAGE_GENERATION_SECONDS = 90.0


def _provenance(ctx: ToolContext) -> str:
    return f"agent:{ctx.user_username}"


class CreatePostArgs(BaseModel):
    subdeaddit: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=20000)
    post_type: str | None = None


def _posts_created_this_run(ctx: ToolContext) -> int:
    """Count successful create_post/create_image_post calls in this run.

    Both post tools draw from the same one-post-per-run budget (plan 4B):
    an image-post failure must not leave create_post as an unthrottled
    fallback, and vice versa.
    """
    if ctx.run is None:
        return 0
    return ToolCall.query.filter(
        ToolCall.run_id == ctx.run.id,
        ToolCall.name.in_(POST_TOOL_NAMES),
        ToolCall.ok.is_(True),
    ).count()


def _create_post(ctx: ToolContext, params: CreatePostArgs) -> dict:
    if ctx.run is not None:
        posts_this_run = _posts_created_this_run(ctx)
        if posts_this_run >= 1:
            return {
                "ok": False,
                "error": "you have already created a post during this visit (maximum 1 post per session)",
                "hint": "you can read or comment on other posts, vote, or call finish to end your visit",
            }

    if db.session.get(Subdeaddit, params.subdeaddit) is None:
        return {
            "ok": False,
            "error": f"subdeaddit '{params.subdeaddit}' does not exist",
            "hint": "use search with type='subdeaddit' to find existing communities",
        }
    try:
        post = create_post(
            user=ctx.user_username,
            subdeaddit=params.subdeaddit,
            title=params.title,
            content=params.content,
            post_type=params.post_type,
            model=_provenance(ctx),
            llm_model=ctx.llm_model,
        )
    except ContentValidationError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "post_id": post.id,
        "title": post.title,
        "subdeaddit": post.subdeaddit_name,
        "hint": "Post created successfully. Call finish to conclude your visit unless you have other pending actions.",
    }


class CreateImagePostArgs(BaseModel):
    community: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=300)
    content: str | None = Field(default=None, max_length=20000)
    image_prompt: str = Field(min_length=1, max_length=4000)
    alt_text: str = Field(min_length=1, max_length=300)
    post_type: str | None = Field(default=None, max_length=50)


def _create_image_post(ctx: ToolContext, params: CreateImagePostArgs) -> dict:
    """Preflight -> generate -> store -> publish, cleaning up on every failure.

    Authorization against the agent's ``image_posts`` configuration already
    happened in the executor (independent of whether this tool was even
    offered to the model) - this handler only resolves which provider/model
    to use. No database transaction is held open across generation, download,
    or storage (plan 4B); the only writes are the final content-service call.
    """
    if ctx.run is not None and _posts_created_this_run(ctx) >= 1:
        return {
            "ok": False,
            "error": "you have already created a post during this visit (maximum 1 post per session)",
            "hint": "you can read or comment on other posts, vote, or call finish to end your visit",
        }

    if db.session.get(Subdeaddit, params.community) is None:
        return {
            "ok": False,
            "error": f"subdeaddit '{params.community}' does not exist",
            "hint": "use search with type='subdeaddit' to find existing communities",
        }

    try:
        preflight_image_post(
            user=ctx.user_username, subdeaddit=params.community, title=params.title
        )
    except ContentValidationError as exc:
        return {"ok": False, "error": str(exc)}

    cfg = image_posts_config(ctx.agent)
    provider = (
        db.session.get(ImageProvider, cfg["provider_id"])
        if cfg["provider_id"]
        else None
    )
    if provider is None:
        return {
            "ok": False,
            "error": "no image provider is configured for this agent",
        }
    model_id = cfg["model"] or provider.default_model
    if not model_id:
        return {
            "ok": False,
            "error": "no image model is configured for this agent's provider",
        }

    if ctx.deadline is not None:
        remaining = ctx.deadline.remaining()
        if remaining <= 0:
            return {
                "ok": False,
                "error": "not enough time remaining in this run to generate an image",
            }
        budget = min(remaining, _IMAGE_GENERATION_SECONDS)
    else:
        budget = _IMAGE_GENERATION_SECONDS
    deadline = Deadline.after(budget)

    try:
        generation = generate_image(provider, model_id, params.image_prompt, deadline)
    except ImageProviderError as exc:
        return {"ok": False, "error": f"image generation failed: {exc}"}

    root = media_root(current_app)
    try:
        if generation.image_bytes is not None:
            data = generation.image_bytes
        else:
            data = download_image(generation.image_url).data
        stored = store_variants(data, root)
    except MediaStorageError as exc:
        return {"ok": False, "error": f"image storage failed: {exc}"}

    pending = PendingPostImage(
        original_path=stored.original_path,
        thumbnail_path=stored.thumbnail_path,
        mime_type=stored.mime_type,
        byte_size=stored.original_size,
        width=stored.width,
        height=stored.height,
        alt_text=params.alt_text,
        source_prompt=params.image_prompt,
        provider_snapshot=provider.name,
        model_snapshot=model_id,
        provider_id=provider.id,
        request_snapshot=generation.request_id,
    )

    try:
        post = create_image_post(
            title=params.title,
            content=params.content,
            user=ctx.user_username,
            subdeaddit=params.community,
            image=pending,
            post_type=params.post_type,
            model=_provenance(ctx),
            llm_model=ctx.llm_model,
        )
    except ContentValidationError as exc:
        delete_variants(root, stored.original_path, stored.thumbnail_path)
        return {"ok": False, "error": str(exc)}
    except SQLAlchemyError:
        delete_variants(root, stored.original_path, stored.thumbnail_path)
        return {
            "ok": False,
            "error": "failed to save the image post; please try again",
        }

    return {
        "ok": True,
        "post_id": post.id,
        "title": post.title,
        "subdeaddit": post.subdeaddit_name,
        "hint": "Image post created successfully. Call finish to conclude your visit unless you have other pending actions.",
    }


class CreateCommentArgs(BaseModel):
    post_id: int = Field(gt=0)
    parent_id: int | None = None
    content: str = Field(min_length=1, max_length=8000)


def _create_comment(ctx: ToolContext, params: CreateCommentArgs) -> dict:
    if db.session.get(Post, params.post_id) is None:
        return {
            "ok": False,
            "error": f"post {params.post_id} not found",
            "hint": "use read_post to check the post exists before replying",
        }
    if params.parent_id is not None:
        parent = db.session.get(Comment, params.parent_id)
        if parent is None or parent.post_id != params.post_id:
            return {
                "ok": False,
                "error": f"comment {params.parent_id} not found under post "
                f"{params.post_id}",
            }
    try:
        comment = create_comment(
            user=ctx.user_username,
            post_id=params.post_id,
            parent_id=params.parent_id,
            content=params.content,
            model=_provenance(ctx),
            llm_model=ctx.llm_model,
        )
    except ContentValidationError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "comment_id": comment.id,
        "post_id": comment.post_id,
    }


class VoteArgs(BaseModel):
    target_type: Literal["post", "comment"]
    target_id: int = Field(gt=0)
    direction: int = Field(ge=-1, le=1)


def _vote(ctx: ToolContext, params: VoteArgs) -> dict:
    if params.direction == 0:
        return {"ok": False, "error": "value must be 1 or -1"}
    result = cast_vote(
        voter=ctx.user_username,
        target=params.target_type,
        target_id=params.target_id,
        value=params.direction,
    )
    if result["status"] == "ok":
        return {"ok": True, "status": result["status"], "score": result["score"]}
    return {"ok": False, "error": result["reason"]}


def _set_subscription(ctx: ToolContext, subdeaddit: str, *, add: bool) -> dict:
    if db.session.get(Subdeaddit, subdeaddit) is None:
        return {
            "ok": False,
            "error": f"subdeaddit '{subdeaddit}' does not exist",
        }
    state = dict(ctx.agent.state or {})
    subs = list(state.get("subscriptions") or [])
    if add:
        if subdeaddit not in subs:
            subs.append(subdeaddit)
    elif subdeaddit in subs:
        subs.remove(subdeaddit)
    state["subscriptions"] = sorted(set(subs))
    ctx.agent.state = state
    db.session.add(ctx.agent)
    db.session.commit()
    verb = "subscribed to" if add else "unsubscribed from"
    return {"ok": True, "subdeaddit": subdeaddit, "detail": verb}


class SubscribeArgs(BaseModel):
    subdeaddit: str = Field(min_length=1, max_length=50)


def _subscribe(ctx: ToolContext, params: SubscribeArgs) -> dict:
    return _set_subscription(ctx, params.subdeaddit, add=True)


class UnsubscribeArgs(BaseModel):
    subdeaddit: str = Field(min_length=1, max_length=50)


def _unsubscribe(ctx: ToolContext, params: UnsubscribeArgs) -> dict:
    return _set_subscription(ctx, params.subdeaddit, add=False)


class FinishArgs(BaseModel):
    summary: str = Field(min_length=1, max_length=2000)
    mood: str | None = None


def _finish(ctx: ToolContext, params: FinishArgs) -> dict:
    # Terminal marker: the loop treats this tool as the end of the run. The
    # summary/mood pass through verbatim.
    del ctx
    result: dict = {"summary": params.summary}
    if params.mood is not None:
        result["mood"] = params.mood
    return result


register(
    Tool(
        name="create_post",
        description=(
            "Publish a new post to a subdeaddit (max 1 per session). The community "
            "must exist; search first if unsure. Read the community's description first "
            "and write a rich, multi-paragraph, substantive post in your authentic "
            "persona voice that fits that specific community."
        ),
        parameters=CreatePostArgs,
        handler=_create_post,
        min_tier=AutonomyTier.REGULAR,
        rate_class=RateClass.WRITE,
    ),
)
register(
    Tool(
        name="create_image_post",
        description=(
            "Publish a new image post to a subdeaddit (counts toward the same "
            "1-per-session post limit as create_post; only offered when your "
            "image-post configuration is enabled). The community must exist; "
            "search first if unsure. Body text is optional - the image carries "
            "the post. Write a detailed image_prompt describing exactly what "
            "the generated image should depict, and a separate, concise "
            "alt_text describing the image for anyone who cannot see it; "
            "alt_text is shown publicly, image_prompt is not."
        ),
        parameters=CreateImagePostArgs,
        handler=_create_image_post,
        min_tier=AutonomyTier.REGULAR,
        rate_class=RateClass.WRITE,
    ),
)
register(
    Tool(
        name="create_comment",
        description=(
            "Reply to a post, or to another comment when parent_id is given. "
            "Read the existing replies and add something new - never repeat "
            "a take or phrasing already present in the thread."
        ),
        parameters=CreateCommentArgs,
        handler=_create_comment,
        min_tier=AutonomyTier.REGULAR,
        rate_class=RateClass.WRITE,
    ),
)
register(
    Tool(
        name="vote",
        description="Upvote or downvote a post or comment (-1, 0, or 1).",
        parameters=VoteArgs,
        handler=_vote,
        min_tier=AutonomyTier.LURKER,
        rate_class=RateClass.WRITE,
    ),
)
register(
    Tool(
        name="subscribe",
        description="Subscribe to a subdeaddit so it shows up in your feed.",
        parameters=SubscribeArgs,
        handler=_subscribe,
        min_tier=AutonomyTier.REGULAR,
        rate_class=RateClass.WRITE,
    ),
)
register(
    Tool(
        name="unsubscribe",
        description="Remove a subdeaddit from your subscriptions.",
        parameters=UnsubscribeArgs,
        handler=_unsubscribe,
        min_tier=AutonomyTier.REGULAR,
        rate_class=RateClass.WRITE,
    ),
)
register(
    Tool(
        name="finish",
        description=(
            "End your run with a short summary of what you did and how you "
            "feel. Call this when you are done."
        ),
        parameters=FinishArgs,
        handler=_finish,
        min_tier=AutonomyTier.LURKER,
        rate_class=RateClass.META,
    ),
)
