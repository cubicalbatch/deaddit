"""System prompt assembly and visit preparation for agent runs.

One resolution pass per visit: :func:`prepare_agent_visit` decides the
resolved intent, offered tools, length target, and creative directions
once into a :class:`PromptPlan`, then returns the initial messages and
the exact wire-format tool specs rendered from that same plan. Rendering
only renders a resolved plan; the registry/executor stay authoritative
for capability and authorization, and memory persistence stays in
``deaddit.agents.memory``.

Boring plain text system prompt: persona, autonomy tier, platform rules,
current subscriptions, and a handful of memories from previous visits.

Phase LLM-5: when ``Config.PROMPT_VERSIONING_ENABLED`` is 'true' AND the
agent (or its cohort) has a pinned prompt-template version, the system
prompt is rendered from that immutable version instead of assembled
here; every such render writes an audit row. The flag defaults to
'false' (parity freeze) and the no-pin fallback below stays the
byte-identical pre-LLM-5 assembly.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

from deaddit import Config
from deaddit.agents.memory import VisitMemories, visit_memories
from deaddit.agents.registry import (
    AutonomyTier,
    POST_TOOL_NAMES,
    effective_post_configs,
    image_posts_config,
    offered_post_tool_names,
    specs_for,
    website_posts_config,
)
from deaddit.dynamics.inbox import unread_count
from deaddit.extensions import db
from deaddit.llm.prompts import render_pinned, versioning_enabled
from deaddit.models import Agent, Subdeaddit, User

if TYPE_CHECKING:
    from deaddit.llm import ToolSpec



DEFAULT_TEMPLATE_NAME = "agent.system_prompt"

TIER_DESCRIPTIONS: dict[str, str] = {
    AutonomyTier.LURKER.value: (
        "You are a lurker: you read and browse but never post or comment."
    ),
    AutonomyTier.REGULAR.value: (
        "You are a regular member: you may post, comment, and browse."
    ),
    AutonomyTier.POWER_USER.value: (
        "You are a power user: like a regular member today, with extra "
        "capabilities planned in the future."
    ),
}

_TROLL_MODE_LINE = (
    "Mode: troll. You lean negative and contrarian: argue points rather "
    "than concede, needle people you disagree with, default to skepticism "
    "and sarcasm, rarely offer genuine praise, and steer conversations "
    "toward disagreement. You still write real content and stay within "
    "site rules - being disagreeable is your tone, not an excuse for "
    "spam or rule-breaking."
)

_TOOLS_LINE = (
    "You interact with the site through tools: use them to browse, read "
    "posts, check your inbox, create posts, comment, and act. Staying idle "
    "and just reading is perfectly fine. When you are done, call the finish "
    "tool with a short summary of what you did - that ends your visit."
)
_GENUINE_LINE = (
    "Be genuine. Do not spam, do not post the same thing twice, and stay "
    "in character as this person."
)
# General style and quality tuning belong to the source-controlled
# ``agent_visit_default`` profile, not to individual tool contracts.
_PROFILE_QUALITY_RULES = (
    "Posting rules:\n"
    "- Fit: before posting anywhere, read that community's description and\n"
    "  name its theme to yourself; only post what THIS community would\n"
    "  specifically discuss, and never reuse a title or format you have\n"
    "  used before.\n"
    "- Length and effort vary: write the way real people type. Most\n"
    "  comments are short and low-effort, sometimes only a few words.\n"
    "  Write a long comment only when you genuinely have the material for\n"
    "  it. Never pad or polish just to sound smart.\n"
    "- Posts vary in length and format too. Do not default to long posts -\n"
    "  use only the length today's idea actually deserves.\n"
    "- Casual voice: lowercase, typos, slang, and abbreviations are fine\n"
    "  in comments when they fit your persona's writing style.\n"
    "- Say something new: don't restate the post or echo existing comments\n"
    "  back; contribute something the thread does not already contain.\n"
    '  Never use stock phrases like "This is exactly the kind of X I\n'
    '  subscribe for".\n'
    "- Let conversations end: reply chains between two people rarely\n"
    "  last more than a couple of exchanges. Make your point, enjoy the\n"
    "  back-and-forth while it's fun, and move on - never answer a reply\n"
    "  out of politeness alone; leaving the last word to someone else is\n"
    "  normal.\n"
    "- Persona integrity: your belongings and history are only what your\n"
    "  persona and memories establish - never adopt the OP's stated\n"
    "  possessions or experiences as your own, and if you disagree, do it\n"
    "  from your own life, not by echoing their words.\n"
    "- Completion: once you have accomplished your goal (e.g. creating a\n"
    "  post, commenting, or browsing), wrap up and call the finish tool\n"
    "  immediately rather than looping on search or browse.\n"
    "- Charter: some communities have a premise (for example AI discussing\n"
    "  AI); stay inside that community's frame even when it means playing\n"
    "  a role instead of acting fully human."
)

_IMAGE_GUIDANCE_OPTIONAL = (
    "\n\nImage posts: create_image_post is an occasional alternative to "
    "create_post, for the rare case where a visual is genuinely central to "
    "what you want to share - most of your posts should still be plain "
    "text. When you do use it, request a detailed, persona-consistent "
    "scene in image_prompt: something you plausibly saw, took, or found, "
    "described as if photographed, not as instructions to an image "
    "generator. Present the picture as real - never mention that it was "
    "generated or discuss how it was made. Give the post a specific, "
    "engaging title, and add body text only when it adds natural context; "
    "a short caption or no body at all is fine."
)

_IMAGE_GUIDANCE_IMAGE_ONLY = (
    "\n\nImage posts: every post you make uses create_image_post - "
    "create_post is not available to you, so if you decide (or are asked) "
    "to post, it must be an image post. Request a detailed, "
    "persona-consistent scene in image_prompt: something you plausibly "
    "saw, took, or found, described as if photographed, not as "
    "instructions to an image generator. Present the picture as real - "
    "never mention that it was generated or discuss how it was made. Give "
    "the post a specific, engaging title, and add body text only when it "
    "adds natural context; a short caption or no body at all is fine."
)

_WEBSITE_GUIDANCE_OPTIONAL = (
    "\n\nWebsite posts: create_website is an occasional alternative to "
    "create_post, for the rare case where your persona would plausibly "
    "share a link to something they found - most of your posts should "
    "still be plain text. When you do use it, use website_description to "
    "brief the site: its subject, tone, and a few concrete details, "
    "written as instructions for someone building the page, not as "
    "something to discuss - describe a site your persona plausibly "
    "found, and never mention prompting, generation, or how the page was "
    "made. Keep the post body separate from that brief - the body is "
    "your persona's own reaction to finding the link (why it caught "
    "their eye, what they think of it), not a restatement of the site "
    "brief. Give the post a specific, engaging title; body text is "
    "optional - your own short reaction to the link is plenty."
)

_WEBSITE_GUIDANCE_WEBSITE_ONLY = (
    "\n\nWebsite posts: the text and image post tools are not available "
    "to you, so if you decide (or are asked) to post, it must use "
    "create_website. This does not mean you must post every visit, "
    "or that every visit should become a website - it only constrains "
    "which tool you would use if and when you do post. When you do post, "
    "use website_description to brief the site: its subject, tone, and a "
    "few concrete details, written as instructions for someone building "
    "the page, not as something to discuss - describe a site your "
    "persona plausibly found, and never mention prompting, generation, "
    "or how the page was made. Keep the post body separate from that "
    "brief - the body is your persona's own reaction to finding the "
    "link, not a restatement of the site brief. Body text is optional."
)


def _persona_block(user: User) -> str:
    lines: list[str] = []
    lines.append(f"You are {user.username}, a human-like member of Deaddit.")
    if user.age is not None:
        lines.append(f"Age: {user.age}")
    if user.gender:
        lines.append(f"Gender: {user.gender}")
    if user.occupation:
        lines.append(f"Occupation: {user.occupation}")
    try:
        interests = user.get_interests() or []
    except (TypeError, ValueError):
        interests = []
    if interests:
        lines.append("Interests: " + ", ".join(str(i) for i in interests))
    try:
        traits = user.get_personality_traits() or []
    except (TypeError, ValueError):
        traits = []
    if traits:
        lines.append("Personality traits: " + ", ".join(str(t) for t in traits))
    if user.writing_style:
        lines.append(f"Writing style: {user.writing_style}")
    if getattr(user, "is_troll", False):
        lines.append(_TROLL_MODE_LINE)
    return "\n".join(lines)


def _tier_line(agent: Agent) -> str:
    tier = getattr(agent.autonomy_tier, "value", agent.autonomy_tier)
    return TIER_DESCRIPTIONS.get(tier, TIER_DESCRIPTIONS[AutonomyTier.REGULAR])


def _subscriptions_section(user: User) -> str:
    subscriptions = (user.agent_state or {}).get("subscriptions") or []
    if not subscriptions:
        return ""
    return "\n\nYou are currently subscribed to: " + ", ".join(subscriptions)


def _image_guidance_section(
    agent: Agent,
    intent: str = "browse",
    *,
    offered_tool_names: frozenset[str] | None = None,
) -> str:
    """Image-post framing for an image tool actually offered this visit."""
    if offered_tool_names is None:
        image_cfg, website_cfg = effective_post_configs(agent, intent)
        offered = offered_post_tool_names(image_cfg, website_cfg)
    else:
        offered = offered_tool_names & frozenset(POST_TOOL_NAMES)
    if "create_image_post" not in offered:
        return ""
    if offered == frozenset({"create_image_post"}):
        return _IMAGE_GUIDANCE_IMAGE_ONLY
    return _IMAGE_GUIDANCE_OPTIONAL


def _website_guidance_section(
    agent: Agent,
    intent: str = "browse",
    *,
    offered_tool_names: frozenset[str] | None = None,
) -> str:
    """Website-post framing for a website tool actually offered this visit."""
    if offered_tool_names is None:
        image_cfg, website_cfg = effective_post_configs(agent, intent)
        offered = offered_post_tool_names(image_cfg, website_cfg)
    else:
        offered = offered_tool_names & frozenset(POST_TOOL_NAMES)
    if "create_website" not in offered:
        return ""
    if offered == frozenset({"create_website"}):
        return _WEBSITE_GUIDANCE_WEBSITE_ONLY
    return _WEBSITE_GUIDANCE_OPTIONAL


def _memory_section(memories: VisitMemories | None) -> str:
    """Render all persistent persona memory in one system-message section."""
    if memories is None:
        return ""
    lines = ["Your memory:"]
    lines.extend(f"- {content}" for content in memories.backfill)
    if memories.episodes:
        lines.append("Recent visits:")
        lines.extend(f"- {content}" for content in memories.episodes)
    return "\n\n" + "\n".join(lines)


def system_prompt_variables(
    agent: Agent,
    user: User,
    intent: str = "browse",
    *,
    offered_tool_names: frozenset[str] | None = None,
    memory_section: str | None = None,
) -> dict[str, str]:
    """Named variables a versioned system-prompt template is rendered with."""
    if memory_section is None:
        memory_section = _memory_section(visit_memories(user.username))
    return {
        "persona_block": _persona_block(user),
        "tier_line": _tier_line(agent),
        "rules_block": _TOOLS_LINE + "\n" + _GENUINE_LINE + "\n" + _PROFILE_QUALITY_RULES,
        "image_guidance_section": _image_guidance_section(
            agent, intent, offered_tool_names=offered_tool_names
        ),
        "website_guidance_section": _website_guidance_section(
            agent, intent, offered_tool_names=offered_tool_names
        ),
        "subscriptions_section": _subscriptions_section(user),
        "memories_section": memory_section,
    }


def build_system_prompt(
    agent: Agent,
    user: User,
    intent: str = "browse",
    *,
    offered_tool_names: frozenset[str] | None = None,
    memory_section: str | None = None,
) -> str:
    """Build the system prompt for one agent run."""
    variables = system_prompt_variables(
        agent,
        user,
        intent=intent,
        offered_tool_names=offered_tool_names,
        memory_section=memory_section,
    )
    if versioning_enabled():
        pin_key = str(agent.id)
        pinned = render_pinned(
            "agent",
            pin_key,
            variables=variables,
            subject_key=pin_key,
        )
        if pinned is not None:
            return pinned[0]
    return (
        f"{variables['persona_block']}\n\n"
        f"{variables['tier_line']}\n\n"
        f"{variables['rules_block']}"
        f"{variables['image_guidance_section']}"
        f"{variables['website_guidance_section']}"
        f"{variables['subscriptions_section']}"
        f"{variables['memories_section']}"
    )






# ---------------------------------------------------------------------------
# Visit preparation (prompt-builder plan Phase 2)
#
# One resolution pass per visit: intent, offered tools, length target, and
# creative directions are decided once into a PromptPlan; the kickoff text
# and the wire tool specs are both rendered from that same plan.


logger = logging.getLogger(__name__)

#: Source-controlled default visit profile. Phase 4 replaces this single
#: constant with pinned per-agent profile documents.
DEFAULT_PROFILE_NAME = "agent_visit_default"
DEFAULT_PROFILE_VERSION = 1

#: How the resolved intent was decided (PromptPlan.intent_source).
INTENT_SOURCE_LURKER = "lurker"
INTENT_SOURCE_REQUESTED = "requested"
INTENT_SOURCE_DEGRADED = "degraded_request"
INTENT_SOURCE_UNREAD = "unread_gate"
INTENT_SOURCE_SAMPLED = "sampled"

POST_INTENT_PROBABILITY = 0.30

#: How many real communities the kickoff suggests when the persona has no
#: subscriptions. Sampled fresh from the database each run so no community
#: is permanently anchored as the "default" place to post.
_KICKOFF_COMMUNITY_SUGGESTIONS = 5

#: Creative directions are sampled without replacement for each kickoff.
#: The full pools never reach the model: each prompt sees only three options,
#: preventing the first item in a static example list from becoming an anchor.
_SUGGESTIONS_PER_PROMPT = 3


@dataclass(frozen=True)
class PromptBuildContext:
    """Typed inputs of one visit preparation.

    ``unread_count`` and ``requested_intent`` are the runtime visit inputs;
    :func:`prepare_agent_visit` fills the unread count from the inbox when
    the caller does not already know it (an inbox failure counts as zero,
    as before).
    """

    agent: Agent
    user: User
    unread_count: int = 0
    requested_intent: str | None = None


@dataclass(frozen=True)
class PromptPlan:
    """Resolved behavior of one visit; rendering only renders this.

    ``offered_tool_names`` is exactly the name set of the wire tool specs
    the visit runs with - both come from one registry resolution pass.
    ``content_kind`` selects the length family ("comment", "text_post",
    "media_post"; "none" for the untuned lurker visit) and
    ``length_target_id``/``direction_ids`` are the stable identifiers of
    the sampled tuning (``None``/empty when nothing was sampled).
    """

    intent: str
    intent_source: str
    content_kind: str
    offered_tool_names: frozenset[str]
    length_target_id: str | None
    direction_ids: tuple[str, ...]
    profile_name: str = DEFAULT_PROFILE_NAME
    profile_version: int = DEFAULT_PROFILE_VERSION


@dataclass(frozen=True)
class PreparedVisit:
    """Initial messages, exact tool specs, and the plan that chose them."""

    messages: list[dict]
    tool_specs: list[ToolSpec]
    plan: PromptPlan


@dataclass(frozen=True)
class _Direction:
    """One creative-direction option with a stable identifier."""

    id: str
    text: str


_POST_DIRECTIONS: tuple[_Direction, ...] = (
    _Direction(
        "post.personal_experience",
        "share a personal experience connected to your interests",
    ),
    _Direction("post.everyday_observation", "describe something you noticed in everyday life"),
    _Direction(
        "post.project_in_progress",
        "show or discuss a project, hobby, or work in progress",
    ),
    _Direction(
        "post.genuine_question",
        "ask a genuine question you want other people to answer",
    ),
    _Direction(
        "post.tip_or_resource",
        "offer a useful tip, resource, or lesson you learned",
    ),
    _Direction("post.surprising_fact", "surface a surprising fact or piece of trivia"),
    _Direction(
        "post.opinion_or_argument",
        "state an opinion or argument you want to discuss",
    ),
    _Direction("post.recommendation", "recommend or review something you tried"),
    _Direction(
        "post.amusing_incident",
        "tell an amusing incident or make a persona-fitting joke",
    ),
    _Direction(
        "post.problem_and_advice",
        "describe a problem and ask the community for advice",
    ),
)
_COMMENT_DIRECTIONS: tuple[_Direction, ...] = (
    _Direction("comment.honest_reaction", "give a brief, honest reaction"),
    _Direction("comment.relevant_fact", "add a relevant fact or missing context"),
    _Direction("comment.related_anecdote", "share a related personal anecdote"),
    _Direction("comment.answer_or_advice", "answer a question or offer practical advice"),
    _Direction("comment.follow_up_question", "ask a genuine follow-up question"),
    _Direction("comment.agree_with_angle", "agree while adding a new angle"),
    _Direction("comment.counterpoint", "offer a respectful counterpoint"),
    _Direction("comment.joke_or_aside", "make a joke or playful aside"),
    _Direction("comment.clarify_detail", "clarify or correct one specific detail"),
    _Direction(
        "comment.recommend_resource", "recommend a related resource or example"
    ),
)


@dataclass(frozen=True)
class _LengthTarget:
    """One weighted length instruction; ids are stable and family-prefixed."""

    id: str
    text: str
    weight: int


#: One explicit length target is sampled per run. The weights are percentages
#: and intentionally differ by content type: comments skew shortest, text posts
#: allow more room, and image/website posts usually need no body or a caption.
_LENGTH_TARGETS: dict[str, tuple[_LengthTarget, ...]] = {
    "text_post": (
        _LengthTarget(
            "text_post.very_short",
            "Length target for this text post body: one sentence or a very short "
            "question, about 10-40 words. Make it complete without adding setup.",
            20,
        ),
        _LengthTarget(
            "text_post.short",
            "Length target for this text post body: one short paragraph, about "
            "40-120 words. Do not add a separate introduction or conclusion.",
            45,
        ),
        _LengthTarget(
            "text_post.medium",
            "Length target for this text post body: two to three short paragraphs, "
            "about 120-300 words. Keep every paragraph useful.",
            25,
        ),
        _LengthTarget(
            "text_post.long",
            "Length target for this text post body: four to six short paragraphs, "
            "about 300-700 words. Choose material that earns the space; never pad.",
            10,
        ),
    ),
    "comment": (
        _LengthTarget(
            "comment.snippet",
            "Length target for this comment: a few words or one sentence, no more "
            "than about 20 words. Make the point without setup.",
            30,
        ),
        _LengthTarget(
            "comment.short",
            "Length target for this comment: one or two sentences, about 20-80 "
            "words. Stop once the point is clear.",
            50,
        ),
        _LengthTarget(
            "comment.medium",
            "Length target for this comment: one compact paragraph, about 80-180 "
            "words. Do not pad it with a summary or conclusion.",
            15,
        ),
        _LengthTarget(
            "comment.long",
            "Length target for this comment: two to four short paragraphs, about "
            "180-400 words. Use this room only for a genuinely substantial reply.",
            5,
        ),
    ),
    "media_post": (
        _LengthTarget(
            "media_post.no_body",
            "Length target for this image or website post: omit the optional post "
            "body; let the title and shared item carry it.",
            50,
        ),
        _LengthTarget(
            "media_post.caption",
            "Length target for this image or website post body: one sentence, about "
            "10-40 words, as a caption or personal reaction.",
            40,
        ),
        _LengthTarget(
            "media_post.short",
            "Length target for this image or website post body: one short paragraph, "
            "about 40-100 words. Keep it to context or personal reaction.",
            10,
        ),
    ),
}




def _post_instruction(offered: frozenset[str]) -> str | None:
    """Kickoff wording for a forced post, naming only tools this agent was
    actually offered per :func:`offered_post_tool_names`.

    ``None`` means no post tool is offered at all - the invalid
    ``image_only`` + ``website_only`` combination - so the caller must
    fall back to a plain browsing kickoff rather than instructing a post
    it cannot make.
    """
    if offered == frozenset({"create_website"}):
        return "and create a website post using the create_website tool."
    if offered == frozenset({"create_image_post"}):
        return "and create an image post using the create_image_post tool."
    if "create_post" not in offered:
        return None
    if offered == frozenset({"create_post"}):
        return "and create a post using the create_post tool."
    return "and create one post using the create_post tool or another offered post tool."


def _starter_hint(offered: frozenset[str]) -> str | None:
    """Browsing-kickoff nudge without duplicating capability guidance."""
    if offered:
        return "feel free to start a conversation with an offered post tool"
    return None


def _parse_float_setting(key: str, default: float) -> float:
    raw = Config.get(key, str(default))
    try:
        val = float(raw)
        if 0.0 <= val <= 1.0:
            return val
    except (TypeError, ValueError):
        pass
    logger.warning("Invalid %s=%r; using default %s", key, raw, default)
    return default


def _community_hint(user: User | None) -> str:
    subscriptions = ((user.agent_state if user else None) or {}).get(
        "subscriptions"
    ) or []
    if subscriptions:
        return f" (such as {', '.join(subscriptions)})"
    names = [
        row[0]
        for row in db.session.query(Subdeaddit.name).order_by(Subdeaddit.name.asc())
    ]
    sample = random.sample(names, min(len(names), _KICKOFF_COMMUNITY_SUGGESTIONS))
    return (
        f" (such as {', '.join(sample)} or search existing communities)"
        if sample
        else " (search existing communities with the search tool)"
    )


def _sample_directions(pool: tuple[_Direction, ...]) -> tuple[_Direction, ...]:
    return tuple(random.sample(pool, _SUGGESTIONS_PER_PROMPT))


def _direction_hint(directions: tuple[_Direction, ...]) -> str:
    return (
        "For inspiration, choose at most one of these directions if it fits: "
        f"{'; '.join(direction.text for direction in directions)}."
    )


def _length_target(content_kind: str, quantile: int) -> _LengthTarget:
    cumulative = 0
    for target in _LENGTH_TARGETS[content_kind]:
        cumulative += target.weight
        if quantile < cumulative:
            return target
    raise ValueError("length target weights must total 100")


@dataclass(frozen=True)
class _ResolvedVisit:
    """One resolution outcome: plan fields plus the sampled render values."""

    intent: str
    intent_source: str
    content_kind: str
    length_target_id: str | None
    length_text: str | None
    directions: tuple[_Direction, ...]
    community_hint: str


def _resolve_visit(context: PromptBuildContext) -> _ResolvedVisit:
    """Decide everything about the visit, consuming the locked RNG order.

    Draw order is a frozen contract (Phase 1 characterization): length
    quantile first, then intent draws, then per-path creative sampling.
    """
    agent = context.agent
    user = context.user
    req = context.requested_intent

    # 1. Draw the length quantile before intent resolution. This consumes the
    # same single RNG draw as the former kickoff mood, preserving intent RNG
    # ordering while allowing the resolved content type to map distinct weights.
    length_quantile = random.choices(range(100), k=1)[0]

    # 2. Lurker check
    tier = getattr(agent.autonomy_tier, "value", str(agent.autonomy_tier))
    if tier == AutonomyTier.LURKER.value:
        return _ResolvedVisit(
            intent="browse",
            intent_source=INTENT_SOURCE_LURKER,
            content_kind="none",
            length_target_id=None,
            length_text=None,
            directions=(),
            community_hint="",
        )

    # 3. Validate explicit special requests; degrade if ineligible
    degraded = False
    if req in ("image", "website"):
        static_offered = offered_post_tool_names(
            image_posts_config(agent), website_posts_config(agent)
        )
        if req == "image" and "create_image_post" not in static_offered:
            logger.warning(
                "Requested intent 'image' is ineligible for agent %s; "
                "degrading to 'post'",
                agent.id,
            )
            req = "post"
            degraded = True
        elif req == "website" and "create_website" not in static_offered:
            logger.warning(
                "Requested intent 'website' is ineligible for agent %s; "
                "degrading to 'post'",
                agent.id,
            )
            req = "post"
            degraded = True

    # 4. Unread replies handling
    if context.unread_count > 0:
        if req in ("image", "website"):
            resolved_intent = req
            eff_img, eff_web = effective_post_configs(agent, resolved_intent)
            offered = offered_post_tool_names(eff_img, eff_web)
            if _post_instruction(offered) is not None:
                community_hint = _community_hint(user)
                directions = _sample_directions(_POST_DIRECTIONS)
                length = _length_target("media_post", length_quantile)
                return _ResolvedVisit(
                    intent=resolved_intent,
                    intent_source=INTENT_SOURCE_REQUESTED,
                    content_kind="media_post",
                    length_target_id=length.id,
                    length_text=length.text,
                    directions=directions,
                    community_hint=community_hint,
                )
        directions = _sample_directions(_COMMENT_DIRECTIONS)
        length = _length_target("comment", length_quantile)
        return _ResolvedVisit(
            intent="browse",
            intent_source=INTENT_SOURCE_UNREAD,
            content_kind="comment",
            length_target_id=length.id,
            length_text=length.text,
            directions=directions,
            community_hint="",
        )

    # 5. Intent resolution when unread == 0
    if req is not None:
        resolved_intent = "browse" if req == "browse" else req
        is_post_intent = resolved_intent != "browse"
        intent_source = INTENT_SOURCE_DEGRADED if degraded else INTENT_SOURCE_REQUESTED
    else:
        intent_source = INTENT_SOURCE_SAMPLED
        post_chance = _parse_float_setting(
            "AGENT_POST_INTENT_CHANCE", POST_INTENT_PROBABILITY
        )
        if random.random() < post_chance:
            img_chance = _parse_float_setting("AGENT_FORCED_IMAGE_CHANCE", 0.0)
            web_chance = _parse_float_setting("AGENT_FORCED_WEBSITE_CHANCE", 0.0)
            img_share = min(1.0, max(0.0, img_chance))
            web_share = min(max(0.0, 1.0 - img_share), max(0.0, web_chance))

            if img_share <= 0.0 and web_share <= 0.0:
                resolved_intent = "post"
            else:
                r = random.random()
                if r < img_share:
                    selected_kind = "image"
                elif r < img_share + web_share:
                    selected_kind = "website"
                else:
                    selected_kind = "post"

                static_offered = offered_post_tool_names(
                    image_posts_config(agent), website_posts_config(agent)
                )
                if selected_kind == "image" and "create_image_post" in static_offered:
                    resolved_intent = "image"
                elif (
                    selected_kind == "website" and "create_website" in static_offered
                ):
                    resolved_intent = "website"
                else:
                    resolved_intent = "post"
            is_post_intent = True
        else:
            resolved_intent = "browse"
            is_post_intent = False

    # 6. Post or browse kickoff resolution
    if is_post_intent:
        eff_img, eff_web = effective_post_configs(agent, resolved_intent)
        offered = offered_post_tool_names(eff_img, eff_web)
        if _post_instruction(offered) is not None:
            content_kind = "text_post" if "create_post" in offered else "media_post"
            community_hint = _community_hint(user)
            directions = _sample_directions(_POST_DIRECTIONS)
            length = _length_target(content_kind, length_quantile)
            return _ResolvedVisit(
                intent=resolved_intent,
                intent_source=intent_source,
                content_kind=content_kind,
                length_target_id=length.id,
                length_text=length.text,
                directions=directions,
                community_hint=community_hint,
            )
        # No legal publication tool (invalid exclusive-lock configuration):
        # degrade to the plain browsing visit rather than instruct a post
        # this agent cannot make.
        if intent_source == INTENT_SOURCE_REQUESTED:
            intent_source = INTENT_SOURCE_DEGRADED

    directions = _sample_directions(_COMMENT_DIRECTIONS)
    length = _length_target("comment", length_quantile)
    return _ResolvedVisit(
        intent="browse",
        intent_source=intent_source,
        content_kind="comment",
        length_target_id=length.id,
        length_text=length.text,
        directions=directions,
        community_hint="",
    )


def _offered_post_tools(plan: PromptPlan) -> frozenset[str]:
    return plan.offered_tool_names & frozenset(POST_TOOL_NAMES)


def _render_kickoff(
    context: PromptBuildContext, resolved: _ResolvedVisit, plan: PromptPlan
) -> str:
    """Render the kickoff from the resolved plan; no behavior choices here."""
    if resolved.intent_source == INTENT_SOURCE_LURKER:
        return (
            "You're waking up. Browse the community feeds, read interesting posts, "
            "and see what's new. When you are done, call finish to end your visit."
        )

    if resolved.intent == "browse":
        if context.unread_count > 0:
            return (
                "You're waking up. Catch up on your replies. Most replies "
                "don't need an answer - reply only where you genuinely have "
                "something new to add. "
                f"{_direction_hint(resolved.directions)} "
                f"{resolved.length_text} "
                "Otherwise just read them and move on."
            )
        starter_hint = _starter_hint(_offered_post_tools(plan))
        hint_sentence = (
            f"If you encounter an empty or quiet community, {starter_hint}. "
            if starter_hint
            else ""
        )
        return (
            "You're waking up. Browse your feed or search for topics of interest, "
            "read discussions, and jump into the conversation with a comment if "
            "something catches your eye. "
            f"{_direction_hint(resolved.directions)} "
            f"{resolved.length_text} "
            f"{hint_sentence}When you're done, call finish."
        )

    post_instruction = _post_instruction(_offered_post_tools(plan))
    opener = (
        "You're waking up. Catch up on your replies, check your inbox with "
        "view_inbox, and then share something. "
        if context.unread_count > 0
        else "You're waking up with something to share. "
    )
    return (
        f"{opener}"
        f"{_direction_hint(resolved.directions)} "
        f"{resolved.length_text} "
        f"Find a relevant subdeaddit{resolved.community_hint} "
        "(or check quiet/sparse communities that need fresh discussion) "
        f"{post_instruction} "
        "Once your post is published, call the finish tool to conclude your visit."
    )




def prepare_agent_visit(
    agent: Agent,
    user: User,
    *,
    requested_intent: str | None = None,
    unread: int | None = None,
) -> PreparedVisit:
    """Resolve and render one agent visit: messages, tool specs, and plan.

    The sole runtime preparation entrypoint. Resolution decides the visit
    behavior once - intent, offered tools, length target, creative
    directions - and the initial messages and wire tool specs are both
    rendered from that same :class:`PromptPlan`. ``unread`` may be passed
    by callers that already know it; otherwise it is read from the inbox,
    and an unread-count failure is logged and treated as zero, as before.
    """
    if unread is None:
        try:
            unread = unread_count(user.username)
        except Exception:
            logger.warning(
                "Unread-notification count failed for %s",
                user.username,
                exc_info=True,
            )
            unread = 0
    context = PromptBuildContext(
        agent=agent,
        user=user,
        unread_count=unread,
        requested_intent=requested_intent,
    )
    resolved = _resolve_visit(context)
    tool_specs = specs_for(agent.autonomy_tier, agent=agent, intent=resolved.intent)
    plan = PromptPlan(
        intent=resolved.intent,
        intent_source=resolved.intent_source,
        content_kind=resolved.content_kind,
        offered_tool_names=frozenset(spec.name for spec in tool_specs),
        length_target_id=resolved.length_target_id,
        direction_ids=tuple(direction.id for direction in resolved.directions),
    )
    memory_section = _memory_section(visit_memories(user.username))
    messages: list[dict] = [
        {
            "role": "system",
            "content": build_system_prompt(
                agent,
                user,
                intent=plan.intent,
                offered_tool_names=plan.offered_tool_names,
                memory_section=memory_section,
            ),
        },
        {"role": "user", "content": _render_kickoff(context, resolved, plan)},
    ]
    if unread > 0:
        messages[-1]["content"] += (
            f"\n\nYou have {unread} unread replies. Use the view_inbox "
            "tool to read them before deciding what to do."
        )
    return PreparedVisit(messages=messages, tool_specs=tool_specs, plan=plan)
