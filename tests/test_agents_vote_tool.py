"""Coverage for the flipped agent vote tool (Dynamics D1, slice S5).

Exercises the registered ``vote`` tool through ``execute`` and asserts that
cast_vote rejection reasons surface byte-identically.
"""

from __future__ import annotations

import pytest

from deaddit.agents.executor import execute
from deaddit.agents.registry import ToolContext
from deaddit.models import Agent, AgentRun, Post, Setting


@pytest.fixture()
def ctx(db_session, seeded_db):
    """A regular-tier agent + open run bound to the seeded 'alice' persona."""
    agent = Agent(
        user_username="alice",
        autonomy_tier="regular",
        is_enabled=True,
        status="idle",
        config={},
        state={},
        consecutive_failures=0,
    )
    db_session.add(agent)
    db_session.flush()
    run = AgentRun(agent_id=agent.id, trigger="manual", status="running")
    db_session.add(run)
    db_session.commit()
    return ToolContext(agent=agent, run=run, user_username="alice")


def _vote(ctx, target_type: str, target_id: int, direction: int) -> dict:
    return execute(
        "vote",
        {
            "target_type": target_type,
            "target_id": target_id,
            "direction": direction,
        },
        ctx,
    )


def test_ok_vote_updates_score(ctx, db_session, seeded_db):
    post = seeded_db["posts"][1]  # bob's post; alice votes

    result = _vote(ctx, "post", post.id, 1)

    assert result == {"ok": True, "status": "ok", "score": 1}
    assert db_session.get(Post, post.id).score == 1


def test_self_post_vote_rejected_verbatim(ctx, db_session, seeded_db):
    post = seeded_db["posts"][0]  # alice's own post

    result = _vote(ctx, "post", post.id, 1)

    assert result["ok"] is False
    assert result["error"] == "you cannot vote on your own post"


def test_self_comment_vote_rejected_verbatim(ctx, db_session, seeded_db):
    comment = seeded_db["comments"][1]  # alice's own comment

    result = _vote(ctx, "comment", comment.id, -1)

    assert result["ok"] is False
    assert result["error"] == "you cannot vote on your own comment"


def test_nonexistent_target_rejected_verbatim(ctx, db_session, seeded_db):
    missing_id = 999_999

    result = _vote(ctx, "post", missing_id, 1)

    assert result["ok"] is False
    assert result["error"] == f"post {missing_id} does not exist"


def test_unknown_voter_rejected_verbatim(ctx, db_session, seeded_db):
    # The Agent/run rows must satisfy FKs; cast_vote sees the context's
    # voter identity, so point it at a username with no User row.
    ghost_ctx = ToolContext(agent=ctx.agent, run=ctx.run, user_username="ghost")

    result = _vote(ghost_ctx, "comment", seeded_db["comments"][0].id, 1)

    assert result["ok"] is False
    assert result["error"] == "user 'ghost' does not exist"


def test_downvote_rejected_when_disabled(ctx, db_session, seeded_db):
    db_session.add(Setting(key="allow_downvotes", value="false"))
    db_session.commit()
    post = seeded_db["posts"][1]

    result = _vote(ctx, "post", post.id, -1)

    assert result["ok"] is False
    assert result["error"] == "downvotes are disabled"


def test_direction_zero_rejected_with_exact_string(ctx, db_session, seeded_db):
    post = seeded_db["posts"][1]

    result = _vote(ctx, "post", post.id, 0)

    assert result == {"ok": False, "error": "value must be 1 or -1"}
