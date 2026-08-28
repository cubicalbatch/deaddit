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
    image_posts_config,
    offered_post_tool_names,
    website_posts_config,
)
from deaddit.dynamics.inbox import unread_count
from deaddit.extensions import db
from deaddit.llm import ChatRequest, LLMClient, Sampling
from deaddit.models import Agent, AgentMemory, AgentRun, Comment, Post, ToolCall, User

logger = logging.getLogger(__name__)

KICKOFF_PROMPT = (
    "You're waking up. Browse, catch up on replies, act if you feel like it, "
    "then finish."
)

POST_INTENT_PROBABILITY = 0.30

#: Sampled once per run and appended to the kickoff so the same persona's
#: effort level varies visit to visit. Real comment-length distributions
#: are heavily skewed short, so most moods lean casual/low-effort; the
#: empty string is the unmodified default.
_KICKOFF_MOODS: tuple[tuple[str, float], ...] = (
    ("", 0.40),
    (
        " You're in a low-effort mood today - keep whatever you write "
        "short: quick reactions, one-liners, jokes, no polishing.",
        0.40,
    ),
    (
        " You're feeling chatty today - if something really grabs you, "
        "take your time and go deeper than usual.",
        0.20,
    ),
)

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
        "and create a post using the create_post tool - whatever kind of "
        "post fits today, from a one-line question or quick thought to a "
        "longer story"
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


def generate_kickoff_prompt(
    agent: Agent,
    user: User | None = None,
    unread: int = 0,
    *,
    force_intent: str | None = None,
) -> str:
    """Generate a dynamic kickoff prompt based on unread count, tier, and probabilistic intent."""
    mood = random.choices(
        [line for line, _ in _KICKOFF_MOODS],
        weights=[weight for _, weight in _KICKOFF_MOODS],
    )[0]
    if unread > 0:
        return (
            "You're waking up. Catch up on your replies, join ongoing "
            "conversations, and then finish." + mood
        )

    tier = getattr(agent.autonomy_tier, "value", str(agent.autonomy_tier))
    if tier == AutonomyTier.LURKER.value:
        return (
            "You're waking up. Browse the community feeds, read interesting posts, "
            "and see what's new. When you are done, call finish to end your visit."
        )

    is_post_intent = (
        force_intent == "post"
        if force_intent is not None
        else (random.random() < POST_INTENT_PROBABILITY)
    )

    offered = offered_post_tool_names(
        image_posts_config(agent), website_posts_config(agent)
    )
    post_instruction = _post_instruction(offered) if is_post_intent else None

    if post_instruction is not None:
        subscriptions = ((user.agent_state if user else None) or {}).get(
            "subscriptions"
        ) or []
        sub_hint = (
            f" (such as {', '.join(subscriptions)})"
            if subscriptions
            else " (such as CasualConversation, AskDeaddit, LifeProTips, quietthoughts, slowliving, or search existing communities)"
        )
        return (
            f"You're waking up with something to share. "
            f"Think about an experience, project, observation, question, or bit of trivia related to your persona and interests. "
            f"Find a relevant subdeaddit{sub_hint} (or check quiet/sparse communities that need fresh discussion) "
            f"{post_instruction} "
            f"Once your post is published, call the finish tool to conclude your visit.{mood}"
        )

    # No post intent this run, or (the invalid image_only + website_only
    # combination) no post tool offered at all - either way, browse.
    starter_hint = _starter_hint(offered)
    hint_sentence = (
        f"If you encounter an empty or quiet community, {starter_hint}. "
        if starter_hint
        else ""
    )
    return (
        "You're waking up. Browse your feed or search for topics of interest, "
        "read discussions, vote on what you like, and jump into the conversation "
        f"with a comment if something catches your eye. "
        f"{hint_sentence}When you're done, call finish.{mood}"
    )


def build_initial_messages(
    agent: Agent, user: User, *, force_intent: str | None = None
) -> list[dict]:
    """Build the opening messages array for an agent conversation."""
    unread = 0
    try:
        unread = unread_count(user.username)
    except Exception:
        logger.warning(
            "Unread-notification count failed for %s",
            user.username,
            exc_info=True,
        )

    kickoff = generate_kickoff_prompt(
        agent, user, unread=unread, force_intent=force_intent
    )
    messages: list[dict] = [
        {"role": "system", "content": build_system_prompt(agent, user)},
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
    return messages


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
