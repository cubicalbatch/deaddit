"""Tests for deaddit.dynamics.votes.cast_vote (Phase D1, Wave B slice S2)."""

from __future__ import annotations

import pytest

from deaddit.dynamics import votes as votes_module
from deaddit.dynamics.votes import _downvotes_allowed, cast_vote
from deaddit.models import ActivityEvent, Post, Setting, User, Vote


def _refresh(db_session, model, pk):
    db_session.expire_all()
    return db_session.get(model, pk)


# --- Rejection vocabulary (BYTE-FROZEN: asserted exactly verbatim) ---


def test_rejects_invalid_value(seeded_db):
    post = seeded_db["posts"][1]
    result = cast_vote("alice", "post", post.id, 0)
    assert result == {
        "status": "rejected",
        "reason": "value must be 1 or -1",
        "score": 0,
    }


@pytest.mark.parametrize("value", [2, -2])
def test_rejects_non_binary_values(seeded_db, value):
    post = seeded_db["posts"][1]
    result = cast_vote("alice", "post", post.id, value)
    assert result["status"] == "rejected"
    assert result["reason"] == "value must be 1 or -1"


def test_rejects_unknown_voter(seeded_db):
    post = seeded_db["posts"][1]
    result = cast_vote("ghost", "post", post.id, 1)
    assert result == {
        "status": "rejected",
        "reason": "user 'ghost' does not exist",
        "score": 0,
    }


def test_rejects_missing_post_and_comment(seeded_db):
    assert cast_vote("alice", "post", 99999, 1)["reason"] == (
        "post 99999 does not exist"
    )
    assert cast_vote("alice", "comment", 99999, 1)["reason"] == (
        "comment 99999 does not exist"
    )


def test_rejects_unknown_target_type(seeded_db):
    result = cast_vote("alice", "subdeaddit", 1, 1)
    assert result == {
        "status": "rejected",
        "reason": "subdeaddit 1 does not exist",
        "score": 0,
    }


def test_downvote_setting_parser_ignores_outer_whitespace(seeded_db):
    Setting.set_value("allow_downvotes", "  TRUE  ")
    assert _downvotes_allowed() is True
    Setting.set_value("allow_downvotes", "  off  ")
    assert _downvotes_allowed() is False


def test_rejects_self_vote_on_post(seeded_db):
    post = seeded_db["posts"][0]  # authored by alice
    result = cast_vote("alice", "post", post.id, 1)
    assert result == {
        "status": "rejected",
        "reason": "you cannot vote on your own post",
        "score": 0,
    }


def test_rejects_self_vote_on_comment(seeded_db):
    comment = seeded_db["comments"][1]  # authored by alice
    result = cast_vote("alice", "comment", comment.id, -1)
    assert result == {
        "status": "rejected",
        "reason": "you cannot vote on your own comment",
        "score": 0,
    }


def test_rejects_downvote_when_disabled(seeded_db, db_session):
    Setting.set_value("allow_downvotes", "false")
    assert _downvotes_allowed() is False
    post = seeded_db["posts"][1]
    result = cast_vote("alice", "post", post.id, -1)
    assert result == {
        "status": "rejected",
        "reason": "downvotes are disabled",
        "score": 0,
    }
    # Upvotes still work while downvotes are off.
    assert cast_vote("alice", "post", post.id, 1)["status"] == "ok"


@pytest.mark.parametrize(
    ("setting_value", "expected"),
    [
        ("true", True),
        ("1", True),
        ("on", True),
        ("yes", True),
        ("True", True),
        ("false", False),
        ("no", False),
        ("0", False),
        ("off", False),
    ],
)
def test_downvotes_allowed_setting_parsing(seeded_db, setting_value, expected):
    Setting.set_value("allow_downvotes", setting_value)
    assert _downvotes_allowed() is expected


def test_downvote_allowed_by_default(seeded_db):
    assert _downvotes_allowed() is True


# --- Score / vote_count sync and karma accrual ---


def test_new_upvote_updates_all_counters_and_post_karma(seeded_db, db_session):
    post = seeded_db["posts"][1]  # bob's post
    assert cast_vote("alice", "post", post.id, 1) == {"status": "ok", "score": 1}

    post = _refresh(db_session, Post, post.id)
    assert post.score == 1
    assert post.vote_count == 1

    bob = db_session.get(User, "bob")
    assert bob.post_karma == 1


def test_new_comment_upvote_accrues_comment_karma(seeded_db, db_session):
    comment = seeded_db["comments"][1]  # alice's comment on bob's post
    assert cast_vote("bob", "comment", comment.id, 1) == {"status": "ok", "score": 1}

    comment = _refresh(db_session, type(comment), comment.id)
    assert comment.score == 1
    assert comment.vote_count == 1

    alice = db_session.get(User, "alice")
    assert alice.comment_karma == 1
    # Comment votes never touch post karma.
    assert alice.post_karma == 0


def test_new_downvote_negative_score_and_karma(seeded_db, db_session):
    post = seeded_db["posts"][1]
    assert cast_vote("alice", "post", post.id, -1) == {"status": "ok", "score": -1}

    post = _refresh(db_session, Post, post.id)
    assert post.score == -1
    assert post.vote_count == 1
    assert db_session.get(User, "bob").post_karma == -1


def test_same_value_revote_is_idempotent_noop(seeded_db, db_session):
    post = seeded_db["posts"][1]
    first = cast_vote("alice", "post", post.id, 1)
    second = cast_vote("alice", "post", post.id, 1)
    assert first == {"status": "ok", "score": 1}
    assert second == {"status": "ok", "score": 1}

    db_session.expire_all()
    rows = db_session.query(Vote).filter_by(voter="alice", post_id=post.id).all()
    assert len(rows) == 1
    post = db_session.get(Post, post.id)
    assert (post.score, post.vote_count) == (1, 1)
    assert db_session.get(User, "bob").post_karma == 1


def test_switch_up_to_down_delta_is_two(seeded_db, db_session):
    post = seeded_db["posts"][1]
    assert cast_vote("alice", "post", post.id, 1) == {"status": "ok", "score": 1}
    switched = cast_vote("alice", "post", post.id, -1)
    assert switched == {"status": "ok", "score": -1}

    post = _refresh(db_session, Post, post.id)
    assert post.score == -1
    assert post.vote_count == 1  # switch never changes vote_count
    assert db_session.get(User, "bob").post_karma == -1


def test_switch_down_to_up_delta_is_two(seeded_db, db_session):
    post = seeded_db["posts"][1]
    assert cast_vote("alice", "post", post.id, -1) == {"status": "ok", "score": -1}
    assert cast_vote("alice", "post", post.id, 1) == {"status": "ok", "score": 1}

    post = _refresh(db_session, Post, post.id)
    assert post.score == 1
    assert post.vote_count == 1
    assert db_session.get(User, "bob").post_karma == 1


def test_downvote_toggle_both_directions_full_cycle(seeded_db, db_session):
    """down -> up -> down exercises the ±2 delta in both states."""
    post = seeded_db["posts"][1]
    scores = [
        cast_vote("alice", "post", post.id, direction)["score"]
        for direction in (-1, 1, -1)
    ]
    assert scores == [-1, 1, -1]

    post = _refresh(db_session, Post, post.id)
    assert (post.score, post.vote_count) == (-1, 1)
    assert db_session.get(User, "bob").post_karma == -1


def test_switch_on_comment_keeps_vote_count(seeded_db, db_session):
    comment = seeded_db["comments"][0]  # bob's comment
    assert cast_vote("alice", "comment", comment.id, 1)["status"] == "ok"
    assert cast_vote("alice", "comment", comment.id, -1) == {
        "status": "ok",
        "score": -1,
    }

    comment = _refresh(db_session, type(comment), comment.id)
    assert (comment.score, comment.vote_count) == (-1, 1)
    assert db_session.get(User, "bob").comment_karma == -1


def test_rejected_votes_change_nothing(seeded_db, db_session):
    post = seeded_db["posts"][1]
    before = db_session.query(Vote).count()
    assert cast_vote("ghost", "post", post.id, 1)["status"] == "rejected"
    assert cast_vote("alice", "post", 99999, 1)["status"] == "rejected"
    assert cast_vote("alice", "post", post.id, 7)["status"] == "rejected"
    assert cast_vote("bob", "post", post.id, 1)["status"] == "rejected"  # self-vote

    db_session.expire_all()
    assert db_session.query(Vote).count() == before
    fresh = db_session.get(Post, post.id)
    assert (fresh.score, fresh.vote_count) == (0, 0)


def test_concurrent_duplicate_insert_rolls_back_and_resolves(
    seeded_db, db_session, monkeypatch
):
    """Simulate the lost race: the lookup misses a just-committed duplicate.

    The INSERT then violates uq_vote_post, the transaction rolls back, and
    the single retry re-reads the row and resolves idempotently.
    """
    post = seeded_db["posts"][1]
    assert cast_vote("alice", "post", post.id, 1)["status"] == "ok"

    real_find = votes_module._find_vote
    calls: list[int] = []

    def flaky_find(voter, target, target_id):
        calls.append(1)
        if len(calls) > 1:
            return real_find(voter, target, target_id)
        return None  # stale snapshot during the race

    monkeypatch.setattr(votes_module, "_find_vote", flaky_find)

    result = cast_vote("alice", "post", post.id, 1)

    assert result == {"status": "ok", "score": 1}
    assert len(calls) == 2  # rollback + retry happened exactly once

    db_session.expire_all()
    assert db_session.query(Vote).filter_by(voter="alice", post_id=post.id).count() == 1
    post = db_session.get(Post, post.id)
    assert (post.score, post.vote_count) == (1, 1)
    assert db_session.get(User, "bob").post_karma == 1
 
 
def test_simulated_insert_returns_metadata_and_emits_activity(
    seeded_db, db_session
):
    post = seeded_db["posts"][1]
    result = cast_vote(
        "alice", "post", post.id, 1, source="simulated", allow_recast=False
    )

    assert result == {
        "status": "ok",
        "score": 1,
        "changed": True,
        "change_kind": "insert",
    }
    vote = db_session.query(Vote).one()
    assert vote.source == "simulated"
    assert db_session.get(Post, post.id).vote_count == 1
    assert db_session.get(User, "bob").post_karma == 1
    assert db_session.query(ActivityEvent).count() == 1


def test_simulated_same_value_is_noop_without_activity(seeded_db, db_session):
    post = seeded_db["posts"][1]
    cast_vote("alice", "post", post.id, 1, source="simulated", allow_recast=False)
    before = {
        "score": post.score,
        "vote_count": post.vote_count,
        "karma": db_session.get(User, "bob").post_karma,
        "events": db_session.query(ActivityEvent).count(),
    }

    result = cast_vote("alice", "post", post.id, 1, source="agent")

    assert result["changed"] is False
    assert result["change_kind"] == "same_value_noop"
    db_session.expire_all()
    assert {
        "score": db_session.get(Post, post.id).score,
        "vote_count": db_session.get(Post, post.id).vote_count,
        "karma": db_session.get(User, "bob").post_karma,
        "events": db_session.query(ActivityEvent).count(),
    } == before


def test_direction_switch_metadata_and_activity(seeded_db, db_session):
    post = seeded_db["posts"][1]
    cast_vote("alice", "post", post.id, 1, source="human")
    result = cast_vote("alice", "post", post.id, -1, source="human")

    assert result["changed"] is True
    assert result["change_kind"] == "direction_switch"
    assert db_session.get(Post, post.id).score == -1
    assert db_session.get(Post, post.id).vote_count == 1
    assert db_session.get(User, "bob").post_karma == -1
    assert db_session.query(ActivityEvent).count() == 2


def test_insert_only_collision_preserves_vote_and_source(seeded_db, db_session):
    post = seeded_db["posts"][1]
    cast_vote("alice", "post", post.id, 1, source="human")

    result = cast_vote(
        "alice", "post", post.id, -1, source="simulated", allow_recast=False
    )

    assert result["changed"] is False
    assert result["change_kind"] == "insert_only_collision"
    vote = db_session.query(Vote).one()
    assert (vote.value, vote.source) == (1, "human")
    assert (post.score, post.vote_count) == (1, 1)
    assert db_session.get(User, "bob").post_karma == 1
    assert db_session.query(ActivityEvent).count() == 1
