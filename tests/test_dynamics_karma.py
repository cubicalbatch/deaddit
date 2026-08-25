"""Tests for deaddit.dynamics.karma.recompute_scores_and_karma (D1 Wave B S2)."""

from __future__ import annotations

from sqlalchemy import text

from deaddit.dynamics.karma import recompute_scores_and_karma
from deaddit.dynamics.votes import cast_vote
from deaddit.extensions import db
from deaddit.models import Comment, Post, User, Vote


def _refresh(db_session, model, pk):
    db_session.expire_all()
    return db_session.get(model, pk)


def _make_user(db_session, username):
    user = User(username=username)
    db_session.add(user)
    db_session.commit()
    return user


def test_repair_drifted_score_vote_count_and_upvote_alias(seeded_db, db_session):
    _make_user(db_session, "charlie")
    post = seeded_db["posts"][1]  # bob's post
    assert cast_vote("alice", "post", post.id, 1)["status"] == "ok"
    assert cast_vote("charlie", "post", post.id, 1)["status"] == "ok"

    # Corrupt the aggregates away from Vote truth (score 9/count 5 vs real 2/2).
    post.score = 9
    post.vote_count = 5
    post.upvote_count = 9
    db_session.commit()

    summary = recompute_scores_and_karma()

    assert summary["repaired"] >= 1
    assert summary["drift_votes"] >= 2

    fresh = _refresh(db_session, Post, post.id)
    assert fresh.score == 2
    assert fresh.vote_count == 2
    assert fresh.upvote_count == 2  # alias resynced to score


def test_legacy_vote_less_items_untouched(seeded_db, db_session):
    post = seeded_db["posts"][0]
    post.upvote_count = 7
    post.score = 7  # fabricated legacy display numbers
    comment = seeded_db["comments"][0]
    comment.upvote_count = 4
    comment.score = 4
    legacy_null = Post(
        title="Null legacy",
        content="no fabricated count",
        user="alice",
        subdeaddit_name="testsub",
        model="test-model",
    )
    db_session.add(legacy_null)
    db_session.commit()
    # The ORM client-side default fills upvote_count with 0; force a genuine
    # NULL so the COALESCE(upvote_count, 0) branch is exercised for real.
    db.session.execute(
        text("UPDATE post SET upvote_count = NULL WHERE id = :pid"),
        {"pid": legacy_null.id},
    )
    db_session.commit()

    summary = recompute_scores_and_karma()

    assert summary["legacy_items"] >= 3  # these three plus other seeded content

    fresh_post = _refresh(db_session, Post, post.id)
    assert fresh_post.upvote_count == 7
    assert fresh_post.score == 7
    fresh_comment = _refresh(db_session, Comment, comment.id)
    assert fresh_comment.upvote_count == 4
    assert fresh_comment.score == 4
    fresh_null = _refresh(db_session, Post, legacy_null.id)
    assert fresh_null.upvote_count is None
    assert db_session.query(Vote).count() == 0


def test_karma_sums_effective_scores(seeded_db, db_session):
    """Post karma from votes; comment karma from legacy upvote_count."""
    _make_user(db_session, "charlie")
    bob_post = seeded_db["posts"][1]
    cast_vote("alice", "post", bob_post.id, 1)
    cast_vote("charlie", "post", bob_post.id, 1)  # net vote-authoritative score: 2

    # Legacy comment authored by bob: no votes, fabricated upvote_count.
    legacy = Comment(
        post_id=bob_post.id,
        user="bob",
        content="legacy chatter",
        model="test-model",
        upvote_count=4,
        score=4,
    )
    db_session.add(legacy)
    db_session.commit()

    summary = recompute_scores_and_karma()

    assert summary["karma_updates"] >= 1
    bob = _refresh(db_session, User, "bob")
    assert bob.post_karma == 2  # vote-authoritative
    assert bob.comment_karma == 4  # effective-score rule for vote-less items


def test_effective_score_rule_ignores_score_when_no_votes(seeded_db, db_session):
    """A vote-less item with drifted score must contribute COALESCE(upvote_count)."""
    post = seeded_db["posts"][0]  # alice's post, zero votes
    post.upvote_count = 3
    post.score = 99  # stale/fabricated score with vote_count == 0
    db_session.commit()

    recompute_scores_and_karma()

    alice = _refresh(db_session, User, "alice")
    # Legacy item untouched: effective score is upvote_count (3), not score (99).
    assert alice.post_karma == 3


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

    first = recompute_scores_and_karma()
    second = recompute_scores_and_karma()
    assert second == {
        "repaired": 0,
        "drift_votes": 0,
        "karma_updates": 0,
        "legacy_items": first["legacy_items"],
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
    assert set(summary) == {"repaired", "drift_votes", "karma_updates", "legacy_items"}
