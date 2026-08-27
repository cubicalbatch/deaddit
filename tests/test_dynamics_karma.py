"""Tests for deaddit.dynamics.karma.recompute_scores_and_karma (D1 Wave B S2)."""

from __future__ import annotations

from deaddit.dynamics.karma import recompute_scores_and_karma
from deaddit.dynamics.votes import cast_vote
from deaddit.models import Comment, Post, User, Vote


def _refresh(db_session, model, pk):
    db_session.expire_all()
    return db_session.get(model, pk)


def _make_user(db_session, username):
    user = User(username=username)
    db_session.add(user)
    db_session.commit()
    return user


def test_repair_drifted_score_and_vote_count(seeded_db, db_session):
    _make_user(db_session, "charlie")
    post = seeded_db["posts"][1]  # bob's post
    assert cast_vote("alice", "post", post.id, 1)["status"] == "ok"
    assert cast_vote("charlie", "post", post.id, 1)["status"] == "ok"

    # Corrupt the aggregates away from Vote truth (score 9/count 5 vs real 2/2).
    post.score = 9
    post.vote_count = 5
    db_session.commit()

    summary = recompute_scores_and_karma()

    assert summary["repaired"] >= 1
    assert summary["drift_votes"] >= 2

    fresh = _refresh(db_session, Post, post.id)
    assert fresh.score == 2
    assert fresh.vote_count == 2


def test_zero_vote_items_repaired_to_zero(seeded_db, db_session):
    post = seeded_db["posts"][0]
    post.score = 7  # corrupted/drifted display number
    comment = seeded_db["comments"][0]
    comment.score = 4
    zero_vote = Post(
        title="Zero-vote item",
        content="no votes at all",
        user="alice",
        subdeaddit_name="testsub",
        model="test-model",
        score=0,
        vote_count=0,
    )
    db_session.add(zero_vote)
    db_session.commit()

    summary = recompute_scores_and_karma()

    assert summary["repaired"] >= 2  # post and comment repaired to 0

    fresh_post = _refresh(db_session, Post, post.id)
    assert fresh_post.score == 0
    assert fresh_post.vote_count == 0
    fresh_comment = _refresh(db_session, Comment, comment.id)
    assert fresh_comment.score == 0
    assert fresh_comment.vote_count == 0
    fresh_zero = _refresh(db_session, Post, zero_vote.id)
    assert (fresh_zero.score, fresh_zero.vote_count) == (0, 0)
    assert db_session.query(Vote).count() == 0


def test_karma_sums_effective_scores(seeded_db, db_session):
    """Post karma and comment karma from vote-authoritative scores."""
    _make_user(db_session, "charlie")
    bob_post = seeded_db["posts"][1]
    cast_vote("alice", "post", bob_post.id, 1)
    cast_vote("charlie", "post", bob_post.id, 1)  # net vote-authoritative score: 2

    # Comment authored by bob with one upvote
    comment = Comment(
        post_id=bob_post.id,
        user="bob",
        content="bob chatter",
        model="test-model",
        score=0,
    )
    db_session.add(comment)
    db_session.commit()
    cast_vote("alice", "comment", comment.id, 1)

    # Corrupt bob's karma away from the vote totals to test rebuild
    bob = _refresh(db_session, User, "bob")
    bob.post_karma = 0
    bob.comment_karma = 0
    db_session.commit()

    summary = recompute_scores_and_karma()

    assert summary["karma_updates"] >= 1
    bob = _refresh(db_session, User, "bob")
    assert bob.post_karma == 2  # vote-authoritative
    assert bob.comment_karma == 1  # vote-authoritative


def test_self_votes_never_exist_so_karma_is_clean(seeded_db, db_session):
    """cast_vote rejects self-votes, so a full rebuild never counts them."""
    post = seeded_db["posts"][1]
    assert cast_vote("bob", "post", post.id, 1)["status"] == "rejected"

    summary = recompute_scores_and_karma()

    assert summary["repaired"] == 0
    assert summary["drift_votes"] == 0
    bob = _refresh(db_session, User, "bob")
    assert bob.post_karma == 0


def test_recompute_is_idempotent(seeded_db, db_session):
    _make_user(db_session, "charlie")
    post = seeded_db["posts"][1]
    cast_vote("alice", "post", post.id, 1)
    cast_vote("charlie", "comment", seeded_db["comments"][0].id, -1)

    recompute_scores_and_karma()
    second = recompute_scores_and_karma()
    assert second == {
        "repaired": 0,
        "drift_votes": 0,
        "karma_updates": 0,
    }


def test_users_without_content_are_not_reset(seeded_db, db_session):
    """Only users owning posts/comments are touched by the karma pass."""
    outsider = _make_user(db_session, "outsider")
    outsider.post_karma = 42
    outsider.comment_karma = 43
    db_session.commit()

    recompute_scores_and_karma()

    outsider = _refresh(db_session, User, "outsider")
    assert (outsider.post_karma, outsider.comment_karma) == (42, 43)


def test_summary_keys_exact(seeded_db):
    summary = recompute_scores_and_karma()
    assert set(summary) == {"repaired", "drift_votes", "karma_updates"}
