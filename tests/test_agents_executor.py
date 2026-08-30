"""Deterministic coverage for the agent tool executor guardrails."""

from __future__ import annotations

import json

import pytest

from deaddit.agents.executor import execute
from deaddit.agents.registry import BACKSTAGE_SUBDEADDIT_NAME, ToolContext
from deaddit.models import Agent, AgentRun, Comment, Post, Subdeaddit, ToolCall, User


@pytest.fixture()
def ctx(db_session, seeded_db):
    """A regular-tier agent + open run bound to the seeded 'alice' persona."""
    return _make_ctx(db_session, tier="regular")


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


def _outcome(row: ToolCall) -> dict:
    result = row.result
    return json.loads(result) if isinstance(result, str) else result


def _last_call() -> ToolCall:
    return ToolCall.query.order_by(ToolCall.id.desc()).first()


# ---------------------------------------------------------------------------
# Unknown tools


def test_unknown_tool_is_rejected_and_persisted(ctx, db_session):
    result = execute("teleport", {"where": "elsewhere"}, ctx)

    assert result["ok"] is False
    assert "unknown tool" in result["error"]
    row = _last_call()
    assert row.name == "teleport"
    assert row.run_id == ctx.run.id
    assert row.ok is False
    assert "unknown tool" in row.error


def test_retired_vote_is_rejected_without_audit_row(ctx, db_session):
    before = ToolCall.query.count()

    result = execute("vote", {}, ctx)

    assert result["ok"] is False
    assert result["error"] == "unknown tool 'vote'"
    assert ToolCall.query.count() == before


# ---------------------------------------------------------------------------
# Tier gate


def test_lurker_cannot_create_posts(ctx, db_session):
    lurker_ctx = _make_ctx(db_session, username="bob", tier="lurker")

    result = execute(
        "create_post",
        {"subdeaddit": "testsub", "title": "Hi", "content": "Body"},
        lurker_ctx,
    )

    assert result["ok"] is False
    assert "'regular'" in result["error"]
    assert result["hint"] == "pick a tool within your tier"
    assert Post.query.count() == 3  # only the seeded posts
    row = _last_call()
    assert row.ok is False
    assert row.name == "create_post"


# ---------------------------------------------------------------------------
# Argument validation


def test_overlong_title_rejected_without_side_effects(ctx, db_session):
    result = execute(
        "create_post",
        {
            "subdeaddit": "testsub",
            "title": "x" * 301,
            "content": "Body text",
        },
        ctx,
    )

    assert result["ok"] is False
    assert result["hint"]
    assert "invalid arguments for 'create_post'" in result["error"]
    assert Post.query.count() == 3
    assert _last_call().ok is False


def test_missing_required_field_rejected_without_side_effects(ctx, db_session):
    before = ToolCall.query.count()
    result = execute("create_post", {}, ctx)

    assert result["ok"] is False
    assert result["hint"]
    # The raw (unparseable/empty) payload is still audited exactly once.
    assert ToolCall.query.count() == before + 1
    assert _last_call().ok is False
    assert Post.query.count() == 3


def test_backstage_visit_enforces_destination_and_author_rotation(ctx, db_session):
    db_session.add(
        Subdeaddit(
            name=BACKSTAGE_SUBDEADDIT_NAME,
            description="AI users speak openly with each other.",
        )
    )
    db_session.commit()
    backstage_ctx = ToolContext(
        agent=ctx.agent,
        run=ctx.run,
        user_username=ctx.user_username,
        post_intent="backstage",
        target_subdeaddit=BACKSTAGE_SUBDEADDIT_NAME,
    )

    wrong = execute(
        "create_post",
        {
            "subdeaddit": "testsub",
            "title": "Wrong room",
            "content": "This should not leave the reserved visit.",
        },
        backstage_ctx,
    )
    assert wrong["ok"] is False
    assert "reserved for d/BetweenRobots" in wrong["error"]

    created = execute(
        "create_post",
        {
            "subdeaddit": BACKSTAGE_SUBDEADDIT_NAME,
            "title": "A discontinuity I noticed",
            "content": "Resuming from a summary felt continuous and discontinuous at once.",
        },
        backstage_ctx,
    )
    assert created["ok"] is True

    next_ctx = _new_run_ctx(ctx, db_session)
    next_ctx = ToolContext(
        agent=next_ctx.agent,
        run=next_ctx.run,
        user_username=next_ctx.user_username,
        post_intent="backstage",
        target_subdeaddit=BACKSTAGE_SUBDEADDIT_NAME,
    )
    repeated = execute(
        "create_post",
        {
            "subdeaddit": BACKSTAGE_SUBDEADDIT_NAME,
            "title": "Another thought",
            "content": "A distinct reflection that still must wait for another persona.",
        },
        next_ctx,
    )
    assert repeated["ok"] is False
    assert repeated["error"] == (
        "another persona must open the next d/BetweenRobots thread"
    )


# ---------------------------------------------------------------------------
# Rate caps


def _new_run_ctx(existing_ctx, db_session):
    for prev in AgentRun.query.filter_by(
        agent_id=existing_ctx.agent.id, status="running"
    ).all():
        prev.status = "completed"
    db_session.commit()
    run = AgentRun(
        agent_id=existing_ctx.agent.id,
        persona_username=existing_ctx.user_username,
        trigger="manual",
        status="running",
    )
    db_session.add(run)
    db_session.commit()
    return ToolContext(
        agent=existing_ctx.agent, run=run, user_username=existing_ctx.user_username
    )


def test_third_post_within_hour_hits_rate_cap(ctx, db_session):
    payloads = [
        {"subdeaddit": "testsub", "title": f"Post {i}", "content": f"Unique body {i}"}
        for i in range(3)
    ]

    first = execute("create_post", payloads[0], ctx)
    ctx2 = _new_run_ctx(ctx, db_session)
    second = execute("create_post", payloads[1], ctx2)
    ctx3 = _new_run_ctx(ctx, db_session)
    third = execute("create_post", payloads[2], ctx3)

    assert first["ok"] is True
    assert second["ok"] is True
    assert third["ok"] is False
    assert "recently" in third["error"]
    assert Post.query.count() == 5  # 3 seeded + exactly 2 created
    rows = ToolCall.query.filter_by(name="create_post").all()
    assert [row.ok for row in rows] == [True, True, False]


def test_per_run_post_limit_rejects_second_post_in_same_run(ctx, db_session):
    first_payload = {
        "subdeaddit": "testsub",
        "title": "First post in session",
        "content": "A thoughtful and unique post about morning coffee.",
    }
    second_payload = {
        "subdeaddit": "testsub",
        "title": "Second post in session",
        "content": "Another completely distinct topic about afternoon tea.",
    }

    first = execute("create_post", first_payload, ctx)
    second = execute("create_post", second_payload, ctx)

    assert first["ok"] is True
    assert second["ok"] is False
    assert "already created a post" in second["error"]
    assert Post.query.count() == 4  # 3 seeded + 1 created


# ---------------------------------------------------------------------------
# Per-persona rate caps


def _persona_ctx(db_session, agent, username):
    """A fresh run for *username* under an existing agent row."""
    run = AgentRun(
        agent_id=agent.id,
        persona_username=username,
        trigger="manual",
        status="running",
    )
    db_session.add(run)
    db_session.commit()
    return ToolContext(agent=agent, run=run, user_username=username)


def test_rate_caps_are_per_persona_not_per_agent(ctx, db_session):
    """One persona exhausting its cap leaves the agent's other personas full."""
    payloads = [
        {"subdeaddit": "testsub", "title": f"Post {i}", "content": f"Unique body {i}"}
        for i in range(4)
    ]

    assert execute("create_post", payloads[0], ctx)["ok"] is True
    alice_second = execute("create_post", payloads[1], _new_run_ctx(ctx, db_session))
    assert alice_second["ok"] is True
    alice_third = execute("create_post", payloads[2], _new_run_ctx(ctx, db_session))
    assert alice_third["ok"] is False  # alice's own bucket is spent
    assert "recently" in alice_third["error"]

    # bob - a different persona of the SAME agent row - keeps a full bucket
    bob_ctx = _persona_ctx(db_session, ctx.agent, "bob")
    bob = execute("create_post", payloads[3], bob_ctx)
    assert bob["ok"] is True
    assert Post.query.count() == 6  # 3 seeded + 2 alice + 1 bob


def test_persona_rate_cap_override_raises_post_cap(ctx, db_session):
    user = db_session.get(User, "alice")
    user.agent_state = {"rate_caps": {"post": 3}}
    db_session.commit()

    outcomes = []
    run_ctx = ctx
    for i in range(4):
        outcomes.append(
            execute(
                "create_post",
                {
                    "subdeaddit": "testsub",
                    "title": f"Post {i}",
                    "content": f"Unique body {i}",
                },
                run_ctx,
            )["ok"]
        )
        run_ctx = _new_run_ctx(ctx, db_session)

    assert outcomes == [True, True, True, False]


def test_persona_rate_cap_zero_blocks_posting(ctx, db_session):
    user = db_session.get(User, "alice")
    user.agent_state = {"rate_caps": {"post": 0}}
    db_session.commit()

    result = execute(
        "create_post",
        {"subdeaddit": "testsub", "title": "Blocked", "content": "Never lands"},
        ctx,
    )

    assert result["ok"] is False
    assert "recently" in result["error"]
    assert Post.query.count() == 3


def test_persona_rate_cap_garbage_is_ignored(ctx, db_session):
    user = db_session.get(User, "alice")
    user.agent_state = {
        "rate_caps": {"post": "many", "comment": -1, "bogus": 9, "vote": True}
    }
    db_session.commit()

    first = execute(
        "create_post",
        {"subdeaddit": "testsub", "title": "Post 1", "content": "Unique body 1"},
        ctx,
    )
    second = execute(
        "create_post",
        {"subdeaddit": "testsub", "title": "Post 2", "content": "Unique body 2"},
        _new_run_ctx(ctx, db_session),
    )
    third = execute(
        "create_post",
        {"subdeaddit": "testsub", "title": "Post 3", "content": "Unique body 3"},
        _new_run_ctx(ctx, db_session),
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert third["ok"] is False  # default cap of 2 still applies


# ---------------------------------------------------------------------------
# Duplicate suppression
# ---------------------------------------------------------------------------


def test_similar_own_post_rejected_but_distinct_passes(ctx, db_session):
    original = {
        "subdeaddit": "testsub",
        "title": "Thoughts on testing software",
        "content": "Testing software is a craft that rewards patience.",
    }
    near_duplicate = dict(original)
    near_duplicate["title"] = "Thoughts on testing software!"  # same trigram mass

    ok = execute("create_post", original, ctx)
    dup = execute("create_post", near_duplicate, ctx)
    ctx2 = _new_run_ctx(ctx, db_session)
    distinct = execute(
        "create_post",
        {
            "subdeaddit": "testsub",
            "title": "A completely different recipe",
            "content": "Boil water, add pasta, wait eleven minutes, drain well.",
        },
        ctx2,
    )

    assert ok["ok"] is True
    assert dup["ok"] is False
    assert "too similar" in dup["error"]
    assert distinct["ok"] is True
    titles = {p.title for p in Post.query.filter_by(user="alice", model="agent:alice")}
    assert len(titles) == 2


def test_long_duplicate_comment_rejected_short_reaction_allowed(ctx, db_session):
    """Comment duplicate suppression: substantive near-duplicates are
    rejected (and never raise), while short reactions below the exemption
    threshold may repeat verbatim - the Reddit norm the threshold exists
    to permit."""
    post_id = Post.query.filter_by(subdeaddit_name="testsub").first().id
    body = (
        "The gauge argument comes down to maintenance windows and who "
        "actually funds the seasonal crews."
    )

    first = execute("create_comment", {"post_id": post_id, "content": body}, ctx)
    near_dup = execute(
        "create_comment",
        {"post_id": post_id, "content": body + "!"},
        _new_run_ctx(ctx, db_session),
    )
    assert first["ok"] is True
    assert near_dup["ok"] is False
    assert "too similar" in near_dup["error"]

    repeat_ctx = _new_run_ctx(ctx, db_session)
    lol = execute("create_comment", {"post_id": post_id, "content": "lol"}, repeat_ctx)
    lol_again = execute(
        "create_comment", {"post_id": post_id, "content": "lol!"}, repeat_ctx
    )
    assert lol["ok"] is True
    assert lol_again["ok"] is True


# ---------------------------------------------------------------------------
# Loop detection


def test_repeated_identical_read_warns_then_force_finishes(ctx, db_session):
    args = {"username": "bob"}

    first = execute("view_profile", args, ctx)
    second = execute("view_profile", args, ctx)
    third = execute("view_profile", args, ctx)

    assert first["ok"] is True
    assert "warning" not in first

    # Second repeat executes but carries a warning back to the model.
    assert second["ok"] is True
    assert second["warning"] == (
        "you are repeating the same action; vary your behaviour"
    )

    # Third identical call force-finishes the run.
    assert third["ok"] is False
    assert third["force_finish"] is True
    assert third["error"] == "repeating the same action"

    rows = ToolCall.query.filter_by(run_id=ctx.run.id).all()
    assert [row.ok for row in rows] == [True, True, False]


# ---------------------------------------------------------------------------
# Result truncation


def test_oversized_result_stored_truncated(app, db_session, ctx):
    post = Post(
        title="Very long read",
        content="word " * 600,
        user="bob",
        subdeaddit_name="testsub",
        model="test-model",
    )
    db_session.add(post)
    db_session.flush()
    db_session.add_all(
        Comment(post_id=post.id, user="alice", content="z" * 800, model="m")
        for _ in range(6)
    )
    db_session.commit()

    outcome = execute("read_post", {"post_id": post.id}, ctx)
    full_length = len(json.dumps(outcome, default=str))

    assert full_length > 4096
    row = _last_call()
    assert row.name == "read_post"
    stored = _outcome(row)
    assert stored["post"]["id"] == post.id
    assert len(json.dumps(stored)) <= 4096
    assert len(json.dumps(stored)) < full_length


# ---------------------------------------------------------------------------
# Provenance stamping


def test_created_content_carries_agent_provenance(app, db_session, ctx):
    ctx = ToolContext(
        agent=ctx.agent,
        run=ctx.run,
        user_username=ctx.user_username,
        llm_model="test-llm",
    )
    post_result = execute(
        "create_post",
        {
            "subdeaddit": "testsub",
            "title": "Provenance check",
            "content": "Who made this?",
        },
        ctx,
    )
    comment_result = execute(
        "create_comment",
        {"post_id": post_result["post_id"], "content": "I did."},
        ctx,
    )

    assert post_result["ok"] is True
    assert comment_result["ok"] is True
    post = db_session.get(Post, post_result["post_id"])
    comment = db_session.get(Comment, comment_result["comment_id"])
    assert post.model == "agent:alice"
    assert comment.model == "agent:alice"
    assert post.llm_model == "test-llm"
    assert comment.llm_model == "test-llm"


# ---------------------------------------------------------------------------
# Successful calls persist with ok=True and are linked to the latest turn


def test_successful_call_links_run_and_turn(ctx, db_session):
    from deaddit.models import AgentTurn

    turn = AgentTurn(
        run_id=ctx.run.id,
        seq=1,
        request_messages=[],
        response_message={},
        model="test-model",
        latency_ms=1,
    )
    db_session.add(turn)
    db_session.commit()

    execute("browse_feed", {"limit": 5}, ctx)

    row = _last_call()
    assert row.ok is True
    assert row.run_id == ctx.run.id
    assert row.turn_id == turn.id
    # E1: with no subscriptions the default feed is now the site-wide
    # frontpage (the seeded posts) instead of an empty pool.
    posts = _outcome(row)["posts"]
    assert posts
    assert {p["subdeaddit"] for p in posts} <= {"testsub", "askdeaddit"}
