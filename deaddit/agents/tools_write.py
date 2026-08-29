"""Write and meta agent tools (slice S2).

Resolution 1: all post/comment persistence goes through
``deaddit.services.content`` with provenance stamping (``model=`` kwarg,
Resolution 9). Write tools are rate class WRITE; ``finish`` is META.
"""

from __future__ import annotations

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
    subscribe_nudge,
)
from deaddit.config import Config
from deaddit.dynamics import threads
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
from deaddit.models import (
    Comment,
    GeneratedWebsite,
    ImageProvider,
    Post,
    Subdeaddit,
    ToolCall,
    User,
)
from deaddit.services.content import (
    ContentValidationError,
    PendingGeneratedWebsite,
    PendingPostImage,
    create_comment,
    create_image_post,
    create_post,
    create_website_post,
    preflight_image_post,
    preflight_website_post,
)
from deaddit.websites.generator import (
    WebsiteGenerationError,
    WebsiteGenerationInvalidHTMLError,
    WebsiteGenerationTruncatedError,
    generate_website_html,
)
from deaddit.websites.storage import (
    InvalidHostnameHintError,
    InvalidPageNameHintError,
    WebsiteStorageError,
    allocate_public_path,
    normalize_hostname_hint,
    normalize_page_name_hint,
    resolve_website_settings,
    store_website,
    website_root,
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
                "hint": "you can read or comment, or call finish to end your visit",
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
    result = {
        "ok": True,
        "post_id": post.id,
        "title": post.title,
        "subdeaddit": post.subdeaddit_name,
        "hint": "Post created successfully. Call finish to conclude your visit unless you have other pending actions.",
    }
    nudge = subscribe_nudge(ctx, post.subdeaddit_name)
    if nudge is not None:
        result["subscribe_hint"] = nudge
    return result


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
            "hint": "you can read or comment, or call finish to end your visit",
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
    provider_id = cfg.get("provider_id")
    provider = (
        db.session.get(ImageProvider, provider_id)
        if provider_id
        else ImageProvider.get_default()
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


class CreateWebsiteArgs(BaseModel):
    community: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=300)
    content: str | None = Field(default=None, max_length=20000)
    website_description: str = Field(min_length=100, max_length=12000)
    hostname_hint: str = Field(min_length=3, max_length=253)
    page_name_hint: str = Field(min_length=1, max_length=120)
    post_type: str | None = Field(default=None, max_length=50)


#: Marker stored (and returned) on a create_website failure result once the
#: nested HTML generation request has actually been sent - i.e. a "billed"
#: attempt within the meaning of the spec's one-attempt-per-run guard. Never
#: set on an ``ok: True`` result: a successful post already consumes the
#: shared one-post-per-run budget checked at the top of this handler, so no
#: further attempt in this run can ever reach the generator again.
_WEBSITE_GENERATION_ATTEMPTED_KIND = "website_generation_attempted"


def _website_generation_attempts_this_run(ctx: ToolContext) -> int:
    """Count billed create_website generation attempts already made this run.

    A "billed" attempt is one that actually reached
    :func:`deaddit.websites.generator.generate_website_html` - regardless of
    whether it went on to produce a publishable page - so repeated malformed
    32K-token responses cannot multiply generation cost within one visit
    (spec invariant, "Atomic publication flow" step 1). Calls rejected
    before generation (policy, the shared post budget, an unknown
    community, a preflight failure, or an exhausted run deadline) are never
    billed and do not count here. A successful call counts too, though in
    practice the shared one-post-per-run budget already blocks any later
    attempt in the same run.
    """
    if ctx.run is None:
        return 0
    rows = ToolCall.query.filter_by(run_id=ctx.run.id, name="create_website").all()
    count = 0
    for row in rows:
        if row.ok or (
            isinstance(row.result, dict)
            and row.result.get("kind") == _WEBSITE_GENERATION_ATTEMPTED_KIND
        ):
            count += 1
    return count


def _is_public_path_taken(public_path: str) -> bool:
    return (
        db.session.query(GeneratedWebsite.id).filter_by(public_path=public_path).first()
        is not None
    )


def _create_website(ctx: ToolContext, params: CreateWebsiteArgs) -> dict:
    """Preflight -> generate -> validate -> allocate/store -> publish.

    Website-post policy authorization already happened in the executor
    (independent of whether this tool was even offered to the model,
    mirroring ``create_image_post``) - this handler only enforces the
    budgets/guardrails that live at the publication layer: the shared
    one-post-per-run budget (spec "Atomic publication flow" step 1) and the
    dedicated one-billed-generation-attempt-per-run guard
    (:func:`_website_generation_attempts_this_run`).

    No database transaction spans generation or the filesystem write: the
    only database activity between "call the generator" and "store the
    HTML" is none at all (steps 3-5 of the spec's "Atomic publication
    flow" run in plain Python against already-fetched settings), and the
    only commit in this whole function is inside
    :func:`~deaddit.services.content.create_website_post`, called only
    after the file already exists on disk. On any failure from that point
    on, ``create_website_post`` itself deletes the just-stored file (unlike
    the image path); this handler never deletes it a second time.
    """
    if ctx.run is not None and _posts_created_this_run(ctx) >= 1:
        return {
            "ok": False,
            "error": "you have already created a post during this visit (maximum 1 post per session)",
            "hint": "you can read or comment, or call finish to end your visit",
        }

    if ctx.run is not None and _website_generation_attempts_this_run(ctx) >= 1:
        return {
            "ok": False,
            "error": "you have already attempted to generate a website during this visit (maximum 1 attempt per session)",
            "hint": "you can read or comment, or call finish to end your visit",
        }

    if db.session.get(Subdeaddit, params.community) is None:
        return {
            "ok": False,
            "error": f"subdeaddit '{params.community}' does not exist",
            "hint": "use search with type='subdeaddit' to find existing communities",
        }

    try:
        preflight_website_post(
            user=ctx.user_username, subdeaddit=params.community, title=params.title
        )
    except ContentValidationError as exc:
        return {"ok": False, "error": str(exc)}

    if not ctx.llm_api_url or not ctx.llm_model:
        return {
            "ok": False,
            "error": "no LLM endpoint is configured for this agent",
        }

    if ctx.deadline is not None:
        remaining = ctx.deadline.remaining()
        if remaining <= 0:
            return {
                "ok": False,
                "error": "not enough time remaining in this run to generate a website",
            }
        run_deadline_remaining = remaining
    else:
        run_deadline_remaining = None

    settings = resolve_website_settings(Config.get)

    try:
        generation = generate_website_html(
            website_description=params.website_description,
            hostname_hint=params.hostname_hint,
            page_name_hint=params.page_name_hint,
            api_url=ctx.llm_api_url,
            api_key=ctx.llm_api_key,
            model=ctx.llm_model,
            agent=ctx.user_username,
            settings=settings,
            run_deadline_remaining=run_deadline_remaining,
        )
    except WebsiteGenerationTruncatedError:
        return {
            "ok": False,
            "error": "website generation stopped before completing the document; "
            "try a shorter or simpler site brief",
            "kind": _WEBSITE_GENERATION_ATTEMPTED_KIND,
        }
    except WebsiteGenerationInvalidHTMLError:
        return {
            "ok": False,
            "error": "website generation produced an invalid document",
            "kind": _WEBSITE_GENERATION_ATTEMPTED_KIND,
        }
    except WebsiteGenerationError:
        return {
            "ok": False,
            "error": "website generation failed",
            "kind": _WEBSITE_GENERATION_ATTEMPTED_KIND,
        }

    try:
        hostname = normalize_hostname_hint(params.hostname_hint)
        page_name = normalize_page_name_hint(params.page_name_hint)
    except (InvalidHostnameHintError, InvalidPageNameHintError):
        return {
            "ok": False,
            "error": "could not turn the requested hostname/page name into a "
            "valid website address",
            "kind": _WEBSITE_GENERATION_ATTEMPTED_KIND,
        }

    try:
        allocated = allocate_public_path(
            hostname, page_name, is_public_path_taken=_is_public_path_taken
        )
    except WebsiteStorageError:
        return {
            "ok": False,
            "error": "could not allocate a unique website address; try again",
            "kind": _WEBSITE_GENERATION_ATTEMPTED_KIND,
        }

    root = website_root(current_app)
    try:
        stored = store_website(generation.html, root)
    except Exception:
        return {
            "ok": False,
            "error": "website storage failed",
            "kind": _WEBSITE_GENERATION_ATTEMPTED_KIND,
        }

    pending = PendingGeneratedWebsite(
        storage_path=stored.storage_path,
        byte_size=stored.byte_size,
        sha256=stored.sha256,
        public_path=allocated.public_path,
        hostname=allocated.hostname,
        page_name=allocated.page_name,
        source_description=params.website_description,
        creator_username_snapshot=ctx.user_username,
        api_url_snapshot=generation.api_url,
        model_snapshot=generation.model,
        agent_id=getattr(ctx.agent, "id", None),
        agent_run_id=getattr(ctx.run, "id", None),
        request_id=generation.request_id,
        prompt_tokens=generation.prompt_tokens,
        completion_tokens=generation.completion_tokens,
        total_tokens=generation.total_tokens,
        finish_reason=generation.finish_reason,
    )

    try:
        post = create_website_post(
            title=params.title,
            content=params.content,
            user=ctx.user_username,
            subdeaddit=params.community,
            website=pending,
            post_type=params.post_type,
            model=_provenance(ctx),
            llm_model=ctx.llm_model,
        )
    except ContentValidationError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "kind": _WEBSITE_GENERATION_ATTEMPTED_KIND,
        }
    except SQLAlchemyError:
        return {
            "ok": False,
            "error": "failed to save the website post; please try again",
            "kind": _WEBSITE_GENERATION_ATTEMPTED_KIND,
        }

    return {
        "ok": True,
        "post_id": post.id,
        "title": post.title,
        "subdeaddit": post.subdeaddit_name,
        "website_url": f"/out/{post.website.public_path}",
        "hostname": post.website.hostname,
        "hint": "Website post created successfully. Call finish to conclude your visit.",
    }


class CreateCommentArgs(BaseModel):
    post_id: int = Field(gt=0)
    parent_id: int | None = None
    content: str = Field(min_length=1, max_length=8000)


def _create_comment(ctx: ToolContext, params: CreateCommentArgs) -> dict:
    post = db.session.get(Post, params.post_id)
    if post is None:
        return {
            "ok": False,
            "error": f"post {params.post_id} not found",
            "hint": "use read_post to check the post exists before replying",
        }
    # Thread cap: the post's frozen popularity ceiling. Counted over all
    # rows (removed comments render as tombstones), so it always matches
    # the count the site shows.
    if post.comment_cap is not None:
        existing = Comment.query.filter_by(post_id=params.post_id).count()
        if existing >= post.comment_cap:
            return {
                "ok": False,
                "error": "this discussion has wound down - the thread is full",
                "hint": "look for a fresher post in your feed to join instead",
            }
    if params.parent_id is not None:
        parent = db.session.get(Comment, params.parent_id)
        if parent is None or parent.post_id != params.post_id:
            return {
                "ok": False,
                "error": f"comment {params.parent_id} not found under post "
                f"{params.post_id}",
            }
        parent_author = parent.user
        # Reply-chain fatigue: a reply that would push the pairwise
        # back-and-forth past its cap is declined in-world; a genuinely
        # new point can still go in as a fresh top-level comment.
        if parent_author != ctx.user_username:
            cap = threads.exchange_cap(params.post_id, parent_author, ctx.user_username)
            if (
                threads.exchange_tail_for_reply(params.parent_id, ctx.user_username)
                > cap
            ):
                return {
                    "ok": False,
                    "error": (
                        f"you and {parent_author} have gone back and forth "
                        "enough in this exchange - let it go"
                    ),
                    "hint": (
                        "if you have a genuinely new point, make it a fresh "
                        "top-level comment on the post instead"
                    ),
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
    result = {
        "ok": True,
        "comment_id": comment.id,
        "post_id": comment.post_id,
    }
    nudge = subscribe_nudge(ctx, post.subdeaddit_name)
    if nudge is not None:
        result["subscribe_hint"] = nudge
    return result


def _set_subscription(ctx: ToolContext, subdeaddit: str, *, add: bool) -> dict:
    user = db.session.get(User, ctx.user_username)
    if user is None:
        return {"ok": False, "error": f"no such user '{ctx.user_username}'"}
    if db.session.get(Subdeaddit, subdeaddit) is None:
        return {
            "ok": False,
            "error": f"subdeaddit '{subdeaddit}' does not exist",
        }
    state = dict(user.agent_state or {})
    subs = list(state.get("subscriptions") or [])
    if add:
        if subdeaddit not in subs:
            subs.append(subdeaddit)
    elif subdeaddit in subs:
        subs.remove(subdeaddit)
    state["subscriptions"] = sorted(set(subs))
    user.agent_state = state
    db.session.add(user)
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
            "Publish a new post to an existing subdeaddit. At most one post may "
            "be published per session."
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
            "Publish a new image post to an existing subdeaddit. It shares the "
            "one-post-per-session limit with create_post. image_prompt is sent to "
            "the image generator; alt_text is the public accessibility description "
            "and image_prompt is not public."
        ),
        parameters=CreateImagePostArgs,
        handler=_create_image_post,
        min_tier=AutonomyTier.REGULAR,
        rate_class=RateClass.WRITE,
    ),
)
register(
    Tool(
        name="create_website",
        description=(
            "Publish a link post to an existing subdeaddit whose destination is a "
            "one-page generated website. It shares the one-post-per-session limit "
            "with create_post. website_description is the generator brief, not "
            "post content, and must specify the site's purpose, audience, "
            "information architecture, visual language, actual content, "
            "interactions, and the specific rendered page. hostname_hint and "
            "page_name_hint are fitting fictional URL hints and may be adjusted "
            "to meet storage rules."
        ),
        parameters=CreateWebsiteArgs,
        handler=_create_website,
        min_tier=AutonomyTier.REGULAR,
        rate_class=RateClass.WRITE,
    ),
)
register(
    Tool(
        name="create_comment",
        description=(
            "Reply to a post, or to another comment when parent_id is given. "
            "parent_id must identify a comment on that post when supplied."
        ),
        parameters=CreateCommentArgs,
        handler=_create_comment,
        min_tier=AutonomyTier.REGULAR,
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
