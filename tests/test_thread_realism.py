"""Thread realism: per-post comment caps and reply-exchange fatigue.

Covers the two guardrails that keep threads human-shaped:

- ``post.comment_cap``: a frozen per-post total-comment ceiling (sampled
  at creation, agent-tool-enforced only, so seeding stays uncapped);
- ``dynamics.threads``: the deterministic pairwise exchange cap that
  ends two-person back-and-forth after 2-3 replies, enforced by the
  create_comment tool (tail > cap rejected) and mirrored by
  notification suppression (tail >= cap skips the reply ping).
"""

from __future__ import annotations

import pytest

from deaddit.agents.executor import execute
from deaddit.agents.prompts import build_system_prompt, prepare_agent_visit
from deaddit.agents.registry import ToolContext
from deaddit.dynamics.threads import (
    _alternating_tail_length,
    exchange_cap,
    exchange_tail_for_reply,
)
from deaddit.models import (
    Agent,
    AgentRun,
    Comment,
    Notification,
    Post,
    Setting,
    ToolCall,
    User,
)
from deaddit.services.content import create_comment, create_post, sample_comment_cap


@pytest.fixture(autouse=True)
def _pin_cap_settings(db_session):
    """Pin both ranges so no test depends on ambient Setting rows."""
    Setting.set_value("thread_comment_cap_min", "20")
    Setting.set_value("thread_comment_cap_max", "39")
    Setting.set_value("reply_exchange_cap_min", "2")
    Setting.set_value("reply_exchange_cap_max", "3")


def _make_ctx(db_session, *, username="alice", tier="regular"):
    agent = Agent(
        user_username=username,
        autonomy_tier=tier,
        is_enabled=True,
        status="idle",
        config={},
        state={},
        consecutive_failures=0,
    )
    db_session.add(agent)
    db_session.flush()
    run = AgentRun(
        agent_id=agent.id,
        persona_username=username,
        trigger="manual",
        status="running",
    )
    db_session.add(run)
    db_session.commit()
    return ToolContext(agent=agent, run=run, user_username=username)


def _make_agent(db_session, username="alice", *, tier="regular"):
    agent = Agent(
        user_username=username,
        autonomy_tier=tier,
        is_enabled=True,
        status="idle",
        config={},
        state={},
        consecutive_failures=0,
    )
    db_session.add(agent)
    db_session.commit()
    return agent


# ---------------------------------------------------------------------------
# Per-post comment cap


def test_sample_comment_cap_varies_within_default_range(db_session):
    caps = {sample_comment_cap() for _ in range(300)}

    assert caps
    assert all(20 <= cap <= 39 for cap in caps)
    # Randomized, not one fixed value every post would share.
    assert len(caps) > 10


def test_sample_comment_cap_honors_settings_and_collapses_inverted_range(
    db_session,
):
    Setting.set_value("thread_comment_cap_min", "5")
    Setting.set_value("thread_comment_cap_max", "6")
    assert {sample_comment_cap() for _ in range(200)} == {5, 6}

    Setting.set_value("thread_comment_cap_max", "2")  # inverted
    assert sample_comment_cap() == 5

    Setting.set_value("thread_comment_cap_min", "banana")  # malformed
    Setting.set_value("thread_comment_cap_max", "banana")
    assert 20 <= sample_comment_cap() <= 39


def test_created_posts_carry_a_frozen_cap(seeded_db, db_session):
    post = create_post(
        title="Capped thread", content="body", user="alice", subdeaddit="testsub"
    )

    assert 20 <= post.comment_cap <= 39
    frozen = post.comment_cap
    db_session.expire(post)
    assert db_session.get(Post, post.id).comment_cap == frozen


def test_create_comment_rejected_when_thread_full(seeded_db, db_session):
    ctx = _make_ctx(db_session, username="alice")
    post = Post.query.filter_by(title="Hello World").one()
    post.comment_cap = 1  # the seeded bob comment already fills it
    db_session.commit()

    result = execute("create_comment", {"post_id": post.id, "content": "one more"}, ctx)

    assert result["ok"] is False
    assert "thread is full" in result["error"]
    assert result["hint"]
    row = ToolCall.query.order_by(ToolCall.id.desc()).first()
    assert row.name == "create_comment"
    assert row.ok is False
    assert Comment.query.filter_by(post_id=post.id).count() == 1


def test_thread_full_rejection_leaves_other_threads_and_quota_intact(
    seeded_db,
    db_session,
):
    ctx = _make_ctx(db_session, username="alice")
    full = Post.query.filter_by(title="Hello World").one()
    full.comment_cap = 1
    db_session.commit()

    rejected = execute("create_comment", {"post_id": full.id, "content": "nope"}, ctx)
    assert rejected["ok"] is False

    elsewhere = execute(
        "create_comment",
        {
            "post_id": Post.query.filter_by(title="Seeded Post").one().id,
            "content": "fresh thread take",
        },
        ctx,
    )
    assert elsewhere["ok"] is True
    # The rejection was audited but did not consume comment quota.
    ok_calls = ToolCall.query.filter_by(name="create_comment", ok=True).count()
    assert ok_calls == 1


def test_legacy_post_without_cap_still_accepts_agent_comments(seeded_db, db_session):
    ctx = _make_ctx(db_session, username="alice")
    post = Post.query.filter_by(title="Hello World").one()
    assert post.comment_cap is None

    result = execute(
        "create_comment", {"post_id": post.id, "content": "uncapped legacy"}, ctx
    )

    assert result["ok"] is True


def test_service_create_comment_ignores_post_cap(seeded_db, db_session):
    """Seeding (and any non-agent path) controls its own comment volume."""
    post = Post.query.filter_by(title="Hello World").one()
    post.comment_cap = 1
    db_session.commit()

    extra = create_comment(
        post_id=post.id, content="seed threads may exceed the cap", user="alice"
    )

    assert extra.id is not None
    assert Comment.query.filter_by(post_id=post.id).count() == 2


def test_feed_and_read_post_expose_thread_full(seeded_db, db_session):
    ctx = _make_ctx(db_session, username="alice")
    full = Post.query.filter_by(title="Hello World").one()
    full.comment_cap = 1  # seeded comment fills it
    db_session.commit()

    feed = execute("browse_feed", {"subdeaddit": "testsub", "sort": "new"}, ctx)
    by_id = {p["id"]: p for p in feed["posts"]}
    assert by_id[full.id]["thread_full"] is True
    assert by_id[full.id]["comment_count"] == 1
    legacy = Post.query.filter_by(title="Seeded Post").one()
    assert by_id[legacy.id]["thread_full"] is False

    read = execute("read_post", {"post_id": full.id}, ctx)
    assert read["ok"] is True
    assert read["post"]["thread_full"] is True
    assert read["post"]["comment_count"] == 1


# ---------------------------------------------------------------------------
# Reply-exchange fatigue


def test_alternating_tail_length_math():
    assert _alternating_tail_length([]) == 0
    assert _alternating_tail_length(["b"]) == 1
    assert _alternating_tail_length(["b", "a"]) == 2
    assert _alternating_tail_length(["b", "a", "b", "a"]) == 4
    # A third author joining ends the pairwise run at two.
    assert _alternating_tail_length(["c", "a", "b", "a"]) == 2
    # Self-replies never form a two-person exchange.
    assert _alternating_tail_length(["a", "a", "b"]) == 1
    assert _alternating_tail_length(["b", "a", "b", "c"]) == 3


def test_exchange_cap_is_deterministic_bounded_and_symmetric(db_session):
    assert exchange_cap(1, "alice", "bob") == exchange_cap(1, "bob", "alice")
    assert exchange_cap(7, "alice", "bob") == exchange_cap(7, "alice", "bob")
    for post_id in range(1, 25):
        for other in ("bob", "carol"):
            assert 2 <= exchange_cap(post_id, "alice", other) <= 3

    Setting.set_value("reply_exchange_cap_min", "3")
    Setting.set_value("reply_exchange_cap_max", "3")
    assert exchange_cap(99, "alice", "bob") == 3


def test_exchange_tail_counts_the_live_chain(seeded_db, db_session):
    post = Post.query.filter_by(title="Hello World").one()
    bob_root = create_comment(post_id=post.id, content="bob root", user="bob")
    alice_reply = create_comment(
        post_id=post.id, content="alice counter", user="alice", parent_id=bob_root.id
    )
    bob_reply = create_comment(
        post_id=post.id, content="bob rebuttal", user="bob", parent_id=alice_reply.id
    )

    # Chain is bob->alice->bob: a would-be alice reply lands in a tail of 4.
    assert exchange_tail_for_reply(bob_reply.id, "alice") == 4
    assert exchange_tail_for_reply(bob_reply.id, "carol") == 2
    assert exchange_tail_for_reply(alice_reply.id, "bob") == 3
    # Top-level reply starts its own count.
    assert exchange_tail_for_reply(None, "alice") == 1


def test_create_comment_rejected_past_pair_exchange_cap(seeded_db, db_session):
    Setting.set_value("reply_exchange_cap_min", "2")
    Setting.set_value("reply_exchange_cap_max", "2")
    db_session.add(User(username="carol", bio="third party"))
    db_session.commit()
    alice_ctx = _make_ctx(db_session, username="alice")
    bob_ctx = _make_ctx(db_session, username="bob")
    carol_ctx = _make_ctx(db_session, username="carol")
    post = Post.query.filter_by(title="Seeded Post").one()

    # alice opens (tail 1), bob counters (tail 2 == cap, still allowed)...
    opened = execute(
        "create_comment",
        {"post_id": post.id, "content": "alice opening take"},
        alice_ctx,
    )
    assert opened["ok"] is True
    countered = execute(
        "create_comment",
        {
            "post_id": post.id,
            "parent_id": opened["comment_id"],
            "content": "bob disagrees",
        },
        bob_ctx,
    )
    assert countered["ok"] is True

    # ...and alice cannot push the exchange to a third round.
    rejected = execute(
        "create_comment",
        {
            "post_id": post.id,
            "parent_id": countered["comment_id"],
            "content": "alice has the last word",
        },
        alice_ctx,
    )
    assert rejected["ok"] is False
    assert "back and forth" in rejected["error"]
    assert "top-level" in rejected["hint"]

    # A third party joining the chain starts a fresh pair count...
    joined = execute(
        "create_comment",
        {
            "post_id": post.id,
            "parent_id": countered["comment_id"],
            "content": "carol pile-on",
        },
        carol_ctx,
    )
    assert joined["ok"] is True

    # ...and replying to yourself is not a two-person exchange.
    self_reply = execute(
        "create_comment",
        {
            "post_id": post.id,
            "parent_id": countered["comment_id"],
            "content": "bob adds a ps",
        },
        bob_ctx,
    )
    assert self_reply["ok"] is True


def test_reply_notification_suppressed_once_exchange_completes(seeded_db, db_session):
    Setting.set_value("reply_exchange_cap_min", "2")
    Setting.set_value("reply_exchange_cap_max", "2")
    post = Post.query.filter_by(title="Seeded Post").one()  # bob's post

    first = create_comment(
        post_id=post.id, content="alice top-level take", user="alice"
    )
    # tail 1 < cap: the post author still gets his reply ping.
    assert (
        Notification.query.filter_by(
            recipient="bob", kind="reply", comment_id=first.id
        ).count()
        == 1
    )

    second = create_comment(
        post_id=post.id, content="bob counter", user="bob", parent_id=first.id
    )
    # tail 2 >= cap: the exchange is complete, alice is not invited back.
    assert (
        Notification.query.filter_by(
            recipient="alice", kind="reply", comment_id=second.id
        ).count()
        == 0
    )

    # Mentions stay live even on a capped exchange.
    third = create_comment(
        post_id=post.id,
        content="hey @bob unrelated ping",
        user="alice",
        parent_id=second.id,
    )
    assert (
        Notification.query.filter_by(
            recipient="bob", kind="mention", comment_id=third.id
        ).count()
        == 1
    )


# ---------------------------------------------------------------------------
# Prompt softening


def test_unread_kickoff_encourages_moving_on(seeded_db, db_session):
    agent = _make_agent(db_session, "alice")

    visit = prepare_agent_visit(agent, db_session.get(User, "alice"), unread=3)
    prompt = visit.messages[1]["content"]

    assert "answer only where you have something genuinely new to add" in prompt
    assert "join ongoing conversations" not in prompt


def test_system_prompt_tells_agents_to_let_conversations_end(seeded_db, db_session):
    agent = _make_agent(db_session, "alice")
    user = db_session.get(User, "alice")

    assert "Let conversations end" in build_system_prompt(agent, user)
