"""Conversation bootstrap and long-term memory for agent runs."""

import logging
import random
import re
from collections import Counter
from datetime import datetime

from deaddit import Config
from deaddit.agents.prompts import build_system_prompt
from deaddit.agents.registry import (
    AutonomyTier,
    effective_post_configs,
    image_posts_config,
    offered_post_tool_names,
    website_posts_config,
)
from deaddit.dynamics.inbox import unread_count
from deaddit.extensions import db
from deaddit.llm import ChatRequest, LLMClient, Sampling
from deaddit.models import (
    Agent,
    AgentMemory,
    AgentRun,
    Comment,
    Post,
    Subdeaddit,
    ToolCall,
    User,
)

logger = logging.getLogger(__name__)

KICKOFF_PROMPT = (
    "You're waking up. Browse, catch up on replies, act if you feel like it, "
    "then finish."
)

POST_INTENT_PROBABILITY = 0.30

#: One explicit length target is sampled per run. The weights are percentages
#: and intentionally differ by content type: comments skew shortest, text posts
#: allow more room, and image/website posts usually need no body or a caption.
_POST_LENGTH_TARGETS: tuple[tuple[str, int], ...] = (
    (
        "Length target for this text post body: one sentence or a very short "
        "question, about 10-40 words. Make it complete without adding setup.",
        20,
    ),
    (
        "Length target for this text post body: one short paragraph, about "
        "40-120 words. Do not add a separate introduction or conclusion.",
        45,
    ),
    (
        "Length target for this text post body: two or three short paragraphs, "
        "about 120-300 words. Keep every paragraph useful.",
        25,
    ),
    (
        "Length target for this text post body: four to six short paragraphs, "
        "about 300-700 words. Choose material that earns the space; never pad.",
        10,
    ),
)
_COMMENT_LENGTH_TARGETS: tuple[tuple[str, int], ...] = (
    (
        "Length target for this comment: a few words or one sentence, no more "
        "than about 20 words. Make the point without setup.",
        30,
    ),
    (
        "Length target for this comment: one or two sentences, about 20-80 "
        "words. Stop once the point is clear.",
        50,
    ),
    (
        "Length target for this comment: one compact paragraph, about 80-180 "
        "words. Do not pad it with a summary or conclusion.",
        15,
    ),
    (
        "Length target for this comment: two to four short paragraphs, about "
        "180-400 words. Use this room only for a genuinely substantial reply.",
        5,
    ),
)
_MEDIA_LENGTH_TARGETS: tuple[tuple[str, int], ...] = (
    (
        "Length target for this image or website post: omit the optional post "
        "body; let the title and shared item carry it.",
        50,
    ),
    (
        "Length target for this image or website post body: one sentence, about "
        "10-40 words, as a caption or personal reaction.",
        40,
    ),
    (
        "Length target for this image or website post body: one short paragraph, "
        "about 40-100 words. Keep it to context or personal reaction.",
        10,
    ),
)

#: How many real communities the kickoff suggests when the persona has no
#: subscriptions. Sampled fresh from the database each run so no community
#: is permanently anchored as the "default" place to post.
_KICKOFF_COMMUNITY_SUGGESTIONS = 5

#: Creative directions are sampled without replacement for each kickoff.
#: The full pools never reach the model: each prompt sees only three options,
#: preventing the first item in a static example list from becoming an anchor.
_SUGGESTIONS_PER_PROMPT = 3
_POST_SUGGESTIONS: tuple[str, ...] = (
    "share a personal experience connected to your interests",
    "describe something you noticed in everyday life",
    "show or discuss a project, hobby, or work in progress",
    "ask a genuine question you want other people to answer",
    "offer a useful tip, resource, or lesson you learned",
    "surface a surprising fact or piece of trivia",
    "state an opinion or argument you want to discuss",
    "recommend or review something you tried",
    "tell an amusing incident or make a persona-fitting joke",
    "describe a problem and ask the community for advice",
)
_COMMENT_SUGGESTIONS: tuple[str, ...] = (
    "give a brief, honest reaction",
    "add a relevant fact or missing context",
    "share a related personal anecdote",
    "answer a question or offer practical advice",
    "ask a genuine follow-up question",
    "agree while adding a new angle",
    "offer a respectful counterpoint",
    "make a joke or playful aside",
    "clarify or correct one specific detail",
    "recommend a related resource or example",
)


def _suggestion_hint(suggestions: tuple[str, ...]) -> str:
    selected = random.sample(suggestions, _SUGGESTIONS_PER_PROMPT)
    return (
        "For inspiration, choose at most one of these directions if it fits: "
        f"{'; '.join(selected)}."
    )


def _length_hint(targets: tuple[tuple[str, int], ...], quantile: int) -> str:
    cumulative = 0
    for hint, weight in targets:
        cumulative += weight
        if quantile < cumulative:
            return hint
    raise ValueError("length target weights must total 100")


_WEBSITE_BRIEF_HINT = (
    " If you use create_website, brief the site in website_description - "
    "subject, tone, and a few concrete details, never mentioning "
    "prompting or generation - and keep the post body to your own "
    "reaction, separate from that brief."
)


def _post_instruction(offered: frozenset[str]) -> str | None:
    """Kickoff wording for a forced post, naming only tools this agent was
    actually offered per :func:`offered_post_tool_names`.

    ``None`` means no post tool is offered at all - the invalid
    ``image_only`` + ``website_only`` combination - so the caller must
    fall back to a plain browsing kickoff rather than instructing a post
    it cannot make.
    """
    if offered == frozenset({"create_website"}):
        return (
            "and create a website post using the create_website tool: "
            "brief the site in website_description - subject, tone, and "
            "a few concrete details, never mentioning prompting or "
            "generation - and keep the post body to your own reaction, "
            "separate from that brief."
        )
    if offered == frozenset({"create_image_post"}):
        return (
            "and create an image post using the create_image_post tool: "
            "request a detailed, persona-consistent scene you plausibly "
            "saw or photographed, present it as real, and give it a "
            "specific, engaging title."
        )
    if "create_post" not in offered:
        return None
    base = (
        "and create a post using the create_post tool, in whatever format and "
        "length fit today's idea"
    )
    extras = []
    if "create_image_post" in offered:
        extras.append(
            "only when a visual is genuinely central to what you want to "
            "share, the create_image_post tool"
        )
    if "create_website" in offered:
        extras.append(
            "only for the rare case where your persona would plausibly "
            "share a link, the create_website tool"
        )
    if not extras:
        return base + "."
    instruction = f"{base} (or, {'; or, '.join(extras)})."
    if "create_website" in offered:
        instruction += _WEBSITE_BRIEF_HINT
    return instruction


def _starter_hint(offered: frozenset[str]) -> str | None:
    """Browsing-kickoff nudge naming only a tool this agent was offered."""
    if "create_post" in offered:
        return "feel free to start a conversation with create_post"
    if "create_image_post" in offered:
        return "feel free to start a conversation with create_image_post"
    if "create_website" in offered:
        return "feel free to share something with create_website"
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


def _subdeaddit_hint(user: User | None) -> str:
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


def generate_kickoff_prompt(
    agent: Agent,
    user: User | None = None,
    unread: int = 0,
    *,
    requested_intent: str | None = None,
    force_intent: str | None = None,
) -> tuple[str, str]:
    """Generate a dynamic kickoff prompt and resolved intent based on unread count, tier, and probabilistic intent."""
    req = requested_intent if requested_intent is not None else force_intent

    # 1. Draw the length quantile before intent resolution. This consumes the
    # same single RNG draw as the former kickoff mood, preserving intent RNG
    # ordering while allowing the resolved content type to map distinct weights.
    length_quantile = random.choices(range(100), k=1)[0]

    # 2. Lurker check
    tier = getattr(agent.autonomy_tier, "value", str(agent.autonomy_tier))
    if tier == AutonomyTier.LURKER.value:
        return (
            "You're waking up. Browse the community feeds, read interesting posts, "
            "and see what's new. When you are done, call finish to end your visit.",
            "browse",
        )

    # 3. Validate explicit special requests; degrade if ineligible
    if req in ("image", "website"):
        static_offered = offered_post_tool_names(
            image_posts_config(agent), website_posts_config(agent)
        )
        if req == "image" and "create_image_post" not in static_offered:
            logger.warning(
                "Requested intent 'image' is ineligible for agent %s; degrading to 'post'",
                agent.id,
            )
            req = "post"
        elif req == "website" and "create_website" not in static_offered:
            logger.warning(
                "Requested intent 'website' is ineligible for agent %s; degrading to 'post'",
                agent.id,
            )
            req = "post"

    # 4. Unread replies handling
    if unread > 0:
        if req in ("image", "website"):
            resolved_intent = req
            eff_img, eff_web = effective_post_configs(agent, resolved_intent)
            offered = offered_post_tool_names(eff_img, eff_web)
            post_instruction = _post_instruction(offered)
            if post_instruction is not None:
                sub_hint = _subdeaddit_hint(user)
                return (
                    f"You're waking up. Catch up on your replies, check your inbox with view_inbox, "
                    f"and then share something. "
                    f"{_suggestion_hint(_POST_SUGGESTIONS)} "
                    f"{_length_hint(_MEDIA_LENGTH_TARGETS, length_quantile)} "
                    f"Find a relevant subdeaddit{sub_hint} (or check quiet/sparse communities that need fresh discussion) "
                    f"{post_instruction} "
                    "Once your post is published, call the finish tool to conclude your visit.",
                    resolved_intent,
                )
        return (
            "You're waking up. Catch up on your replies. Most replies "
            "don't need an answer - reply only where you genuinely have "
            "something new to add. "
            f"{_suggestion_hint(_COMMENT_SUGGESTIONS)} "
            f"{_length_hint(_COMMENT_LENGTH_TARGETS, length_quantile)} "
            "Otherwise just read them and move on.",
            "browse",
        )

    # 5. Intent resolution when unread == 0
    if req is not None:
        if req == "browse":
            resolved_intent = "browse"
            is_post_intent = False
        else:
            resolved_intent = req
            is_post_intent = True
    else:
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
                elif selected_kind == "website" and "create_website" in static_offered:
                    resolved_intent = "website"
                else:
                    resolved_intent = "post"
            is_post_intent = True
        else:
            resolved_intent = "browse"
            is_post_intent = False

    # 6. Post or browse kickoff text
    if is_post_intent:
        eff_img, eff_web = effective_post_configs(agent, resolved_intent)
        offered = offered_post_tool_names(eff_img, eff_web)
        post_instruction = _post_instruction(offered)
        if post_instruction is not None:
            length_targets = (
                _POST_LENGTH_TARGETS
                if "create_post" in offered
                else _MEDIA_LENGTH_TARGETS
            )
            sub_hint = _subdeaddit_hint(user)
            return (
                f"You're waking up with something to share. "
                f"{_suggestion_hint(_POST_SUGGESTIONS)} "
                f"{_length_hint(length_targets, length_quantile)} "
                f"Find a relevant subdeaddit{sub_hint} (or check quiet/sparse communities that need fresh discussion) "
                f"{post_instruction} "
                "Once your post is published, call the finish tool to conclude your visit.",
                resolved_intent,
            )

    eff_img, eff_web = effective_post_configs(agent, "browse")
    offered = offered_post_tool_names(eff_img, eff_web)
    starter_hint = _starter_hint(offered)
    hint_sentence = (
        f"If you encounter an empty or quiet community, {starter_hint}. "
        if starter_hint
        else ""
    )
    return (
        "You're waking up. Browse your feed or search for topics of interest, "
        "read discussions, and jump into the conversation with a comment if "
        "something catches your eye. "
        f"{_suggestion_hint(_COMMENT_SUGGESTIONS)} "
        f"{_length_hint(_COMMENT_LENGTH_TARGETS, length_quantile)} "
        f"{hint_sentence}When you're done, call finish.",
        "browse",
    )


def build_initial_messages(
    agent: Agent,
    user: User,
    *,
    requested_intent: str | None = None,
    force_intent: str | None = None,
) -> tuple[list[dict], str]:
    """Build the opening messages array and resolved intent for an agent conversation."""
    req = requested_intent if requested_intent is not None else force_intent
    unread = 0
    try:
        unread = unread_count(user.username)
    except Exception:
        logger.warning(
            "Unread-notification count failed for %s",
            user.username,
            exc_info=True,
        )

    kickoff, resolved_intent = generate_kickoff_prompt(
        agent, user, unread=unread, requested_intent=req
    )
    messages: list[dict] = [
        {
            "role": "system",
            "content": build_system_prompt(agent, user, intent=resolved_intent),
        },
        {"role": "user", "content": kickoff},
    ]
    memory_block = _memory_block(user.username)
    if memory_block:
        messages[-1]["content"] += "\n\n" + memory_block
    if unread > 0:
        messages[-1]["content"] += (
            f"\n\nYou have {unread} unread replies. Use the view_inbox "
            "tool to read them before deciding what to do."
        )
    return messages, resolved_intent


BACKFILL_PREFIX = "History (before becoming an agent):"
_CHUNK_SIZE = 15
_MAX_CHUNKS = 20

_LLM_SYSTEM_PROMPT = (
    "You are writing a private memory file for a person who is about to "
    "become an autonomous agent. Summarize their past forum activity "
    "faithfully in third person: what they did, the topics they care about, "
    "and their tone. Do not invent anything; stick to the material given."
)

_STOPWORDS = frozenset(
    {
        "about",
        "also",
        "been",
        "because",
        "just",
        "know",
        "like",
        "more",
        "most",
        "much",
        "only",
        "over",
        "really",
        "some",
        "than",
        "that",
        "their",
        "them",
        "then",
        "there",
        "they",
        "think",
        "this",
        "very",
        "was",
        "were",
        "what",
        "when",
        "which",
        "with",
        "would",
        "your",
    }
)


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _snippet(text: str | None, limit: int = 220) -> str:
    text = _clean(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


# ---------------------------------------------------------------------------
# Episode summaries (run-end, deterministic, never fails a run)


def summarize_run(agent: Agent, run: AgentRun) -> None:
    """Persist a deterministic episode note for a finished run.

    Extractive only (no LLM call); all errors are logged and swallowed so
    summarization can never fail the run.
    """
    try:
        calls = ToolCall.query.filter_by(run_id=run.id).order_by(ToolCall.id).all()
        content = (
            _episode_content(calls)
            if calls
            else "Woke up, looked around, and finished without taking any tool actions."
        )
        db.session.add(
            AgentMemory(
                user_username=run.persona_username,
                kind="episode",
                content=content,
            )
        )
    except Exception:
        logger.exception(
            "Episode summarization failed for run %s; ignoring.",
            getattr(run, "id", "?"),
        )


def _episode_content(calls: list) -> str:
    ok_count = sum(1 for call in calls if call.ok)
    failed = [call for call in calls if not call.ok]
    freq = Counter(call.name for call in calls)
    inventory = ", ".join(
        f"{name} x{count}" if count > 1 else name for name, count in freq.items()
    )
    sentences = [
        f"Last visit: {len(calls)} tool action(s), {ok_count} ok / {len(failed)} error"
        + (f": {inventory}." if inventory else ".")
    ]
    if failed:
        example = next((call.error for call in failed if call.error), "")
        detail = f' e.g. "{_snippet(example, 80)}"' if example else ""
        sentences.append(f"{len(failed)} action(s) errored{detail}.")
    created = Counter(
        call.name for call in calls if call.ok and call.name.startswith("create_")
    )
    if created:
        made = ", ".join(
            f"{count} {name.removeprefix('create_')}{'s' if count > 1 else ''}"
            for name, count in created.items()
        )
        sentences.append(f"Created {made}.")
    return " ".join(sentences[:3])


# ---------------------------------------------------------------------------
# One-time persona-history backfill


def backfill_persona_history(
    user_username: str, *, api_url: str | None = None, model: str | None = None
) -> int:
    """Convert a user's pre-agent Post/Comment history into memory episodes.

    One-time per persona: returns 0 immediately when backfill rows already
    exist. A User is required, but no dedicated Agent is needed. Each
    chronological chunk becomes one kind='backfill' row, using the LLM when
    reachable and a deterministic extractive summary otherwise. Returns the
    number of rows inserted.
    """
    user = User.query.filter_by(username=user_username).first()
    if user is None:
        raise ValueError(f"No such user '{user_username}'")
    existing = AgentMemory.query.filter_by(
        user_username=user_username, kind="backfill"
    ).count()
    if existing:
        return 0

    items = _persona_items(user_username)
    if not items:
        return 0
    chunks = [
        items[start : start + _CHUNK_SIZE]
        for start in range(0, len(items), _CHUNK_SIZE)
    ][:_MAX_CHUNKS]

    api_key = None
    if api_url and model:
        try:
            api_key = Config.get_api_key_for_endpoint(api_url)
        except Exception:
            api_key = None

    inserted = 0
    for index, chunk in enumerate(chunks, start=1):
        paragraph = None
        if api_url and model:
            try:
                paragraph = _llm_chunk_summary(chunk, api_url, model, api_key)
            except Exception as exc:
                logger.info(
                    "Persona-history LLM summary failed (%s); "
                    "using extractive fallback.",
                    exc,
                )
                paragraph = None
        if not paragraph:
            paragraph = _extractive_summary(chunk)
        db.session.add(
            AgentMemory(
                user_username=user_username,
                kind="backfill",
                content=f"{BACKFILL_PREFIX} [{index}/{len(chunks)}] {paragraph}",
            )
        )
        inserted += 1
        # Commit per chunk: a mid-backfill failure keeps prior episodes and
        # avoids long pending-transaction windows on SQLite.
        db.session.commit()
    return inserted


def ensure_lazy_backfill(agent: Agent, user: User) -> None:
    """Backfill a random persona's history once on first selection."""
    if getattr(agent, "persona_mode", "fixed") != "random":
        return
    if not (agent.config or {}).get("backfill_memory"):
        return
    try:
        backfill_persona_history(user.username)
    except Exception:
        logger.exception("Lazy persona-history backfill failed for %s", user.username)
        db.session.rollback()


def _persona_items(user_username: str) -> list[dict]:
    items: list[dict] = []
    posts = Post.query.filter_by(user=user_username).order_by(Post.created_at).all()
    comments = (
        Comment.query.filter_by(user=user_username).order_by(Comment.created_at).all()
    )
    for post in posts:
        items.append(
            {
                "kind": "post",
                "created_at": post.created_at,
                "title": post.title,
                "content": post.content,
            }
        )
    for comment in comments:
        items.append(
            {
                "kind": "comment",
                "created_at": comment.created_at,
                "title": "",
                "content": comment.content,
            }
        )
    items.sort(key=lambda item: item["created_at"] or datetime.min)
    return items


def _chunk_text(chunk: list[dict]) -> str:
    lines = []
    for item in chunk:
        stamp = item["created_at"].strftime("%Y-%m-%d") if item["created_at"] else "?"
        body = _snippet(item["content"], 200)
        if item["kind"] == "post":
            lines.append(f"[post] {stamp} - {item['title']}: {body}")
        else:
            lines.append(f"[comment] {stamp} - {body}")
    return "\n".join(lines)


def _llm_chunk_summary(
    chunk: list[dict], api_url: str, model: str, api_key: str | None
) -> str:
    # Plain completion, no tools (Resolution 11).
    result = LLMClient().complete(
        ChatRequest(
            system_prompt=_LLM_SYSTEM_PROMPT,
            user_prompt=(
                "Summarize this person's forum activity in third person:\n\n"
                + _chunk_text(chunk)
            ),
            model=model,
            api_url=api_url,
            api_key=api_key,
            # Reasoning models spend budget on hidden reasoning first; a small
            # cap truncates it before any content is emitted (qwen nothink quirk).
            sampling=Sampling(max_tokens=2048, temperature=0.3),
        )
    )
    return _clean(result.content)


def _extractive_summary(chunk: list[dict]) -> str:
    posts = [item for item in chunk if item["kind"] == "post"]
    comments = [item for item in chunk if item["kind"] == "comment"]
    words: Counter[str] = Counter()
    for item in chunk:
        text = " ".join(filter(None, [item.get("title"), item.get("content")]))
        words.update(
            token.lower()
            for token in re.findall(r"[A-Za-z][A-Za-z']{3,}", text)
            if token.lower() not in _STOPWORDS
        )
    parts = [f"wrote {len(posts)} post(s) and {len(comments)} comment(s)"]
    top = ", ".join(word for word, _ in words.most_common(6))
    if top:
        parts.append(f"recurring topics: {top}")
    titles = [_snippet(item["title"], 80) for item in posts if item.get("title")][:3]
    if titles:
        parts.append("representative titles: " + "; ".join(f'"{t}"' for t in titles))
    return "Extracted summary: " + "; ".join(parts) + "."


# ---------------------------------------------------------------------------
# Kickoff context injection


def _memory_block(user_username: str) -> str:
    backfills = (
        AgentMemory.query.filter_by(user_username=user_username, kind="backfill")
        .order_by(AgentMemory.id.asc())
        .limit(3)
        .all()
    )
    episodes = (
        AgentMemory.query.filter_by(user_username=user_username, kind="episode")
        .order_by(AgentMemory.id.desc())
        .limit(5)
        .all()
    )
    if not backfills and not episodes:
        return ""
    lines = ["Your memory:"]
    for row in backfills:
        lines.append(f"- {row.content}")
    if episodes:
        lines.append("Recent visits:")
        for row in reversed(episodes):
            lines.append(f"- {row.content}")
    return "\n".join(lines)
