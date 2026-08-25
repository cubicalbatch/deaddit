"""Conversation bootstrap and long-term memory for agent runs."""

import logging
import re
from collections import Counter
from datetime import datetime

from deaddit import Config
from deaddit.agents.prompts import build_system_prompt
from deaddit.extensions import db
from deaddit.llm import ChatRequest, LLMClient, Sampling
from deaddit.models import Agent, AgentMemory, AgentRun, Comment, Post, ToolCall, User

logger = logging.getLogger(__name__)

KICKOFF_PROMPT = (
    "You're waking up. Browse, catch up on replies, act if you feel like it, "
    "then finish."
)

INBOX_NOTICE = (
    "If you have unread replies, use the view_inbox tool to read them before "
    "deciding what to do."
)

def build_initial_messages(agent: Agent) -> list[dict]:
    """Build the opening messages array for an agent conversation."""
    user = db.session.get(User, agent.user_username)
    messages: list[dict] = [
        {"role": "system", "content": build_system_prompt(agent, user)},
        {"role": "user", "content": KICKOFF_PROMPT},
    ]
    messages[-1]["content"] += " " + INBOX_NOTICE
    memory_block = _memory_block(agent)
    if memory_block:
        messages[-1]["content"] += "\n\n" + memory_block
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
        "about", "also", "been", "because", "just", "know", "like", "more",
        "most", "much", "only", "over", "really", "some", "than", "that",
        "their", "them", "then", "there", "they", "think", "this", "very",
        "was", "were", "what", "when", "which", "with", "would", "your",
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
        calls = (
            ToolCall.query.filter_by(run_id=run.id).order_by(ToolCall.id).all()
        )
        content = (
            _episode_content(calls) if calls else "Woke up, looked around, and finished without taking any tool actions."
        )
        db.session.add(
            AgentMemory(agent_id=agent.id, kind="episode", content=content)
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
        call.name
        for call in calls
        if call.ok and call.name.startswith("create_")
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

    One-time per agent: returns 0 immediately when backfill rows already
    exist. Each chronological chunk becomes one kind='backfill' row, using
    the LLM when reachable and a deterministic extractive summary otherwise.
    Returns the number of rows inserted.
    """
    agent = Agent.query.filter_by(user_username=user_username).first()
    if agent is None:
        raise ValueError(f"No agent registered for user '{user_username}'")
    existing = AgentMemory.query.filter_by(
        agent_id=agent.id, kind="backfill"
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
                agent_id=agent.id,
                kind="backfill",
                content=f"{BACKFILL_PREFIX} [{index}/{len(chunks)}] {paragraph}",
            )
        )
        inserted += 1
        # Commit per chunk: a mid-backfill failure keeps prior episodes and
        # avoids long pending-transaction windows on SQLite.
        db.session.commit()
    return inserted


def _persona_items(user_username: str) -> list[dict]:
    items: list[dict] = []
    posts = (
        Post.query.filter_by(user=user_username).order_by(Post.created_at).all()
    )
    comments = (
        Comment.query.filter_by(user=user_username)
        .order_by(Comment.created_at)
        .all()
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
    parts = [
        f"wrote {len(posts)} post(s) and {len(comments)} comment(s)"
    ]
    top = ", ".join(word for word, _ in words.most_common(6))
    if top:
        parts.append(f"recurring topics: {top}")
    titles = [_snippet(item["title"], 80) for item in posts if item.get("title")][:3]
    if titles:
        parts.append("representative titles: " + "; ".join(f'"{t}"' for t in titles))
    return "Extracted summary: " + "; ".join(parts) + "."


# ---------------------------------------------------------------------------
# Kickoff context injection


def _memory_block(agent: Agent) -> str:
    backfills = (
        AgentMemory.query.filter_by(agent_id=agent.id, kind="backfill")
        .order_by(AgentMemory.id.asc())
        .limit(3)
        .all()
    )
    episodes = (
        AgentMemory.query.filter_by(agent_id=agent.id, kind="episode")
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
