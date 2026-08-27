"""Tests for Phase D4 slice S2: moderation service layer (plan §6).

Covers report → queue → remove / dismiss flows, scoped bans gating
cast_vote and content creation, expiry auto-lift, removed-content
rejection, and the forced-failure isolation contract: a broken
notify_mod_action must never roll back an applied mod action.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from deaddit.dynamics import moderation
from deaddit.dynamics.karma import recompute_scores_and_karma
from deaddit.dynamics.moderation import (
    active_ban_for,
    ban_user,
    dismiss_report,
    expire_bans,
    lift_ban,
    list_reports,
    remove_report,
    report_content,
)
from deaddit.dynamics.votes import cast_vote
from deaddit.extensions import db
from deaddit.models import Ban, Notification, Post, Report, User
from deaddit.services.content import ContentValidationError, create_comment, create_post


@pytest.fixture()
def moderator(seeded_db):
    """A third user acting as moderator (FK target for resolved_by etc.)."""
    user = User(username="mod_user", bio="the mods", interests='["order"]')
    db.session.add(user)
    db.session.commit()
    return user


# ---------------------------------------------------------------------------
# 1. Report → queue → remove flow


def test_report_then_list_queue_then_remove_flow(seeded_db, db_session, moderator):
    post = seeded_db["posts"][0]  # alice's post in testsub
    report = report_content("bob", "post", post.id, "spam")

    assert report.status == "open"
    assert report.post_id == post.id
    assert report.comment_id is None  # XOR
    queued = list_reports("open").all()
    assert [r.id for r in queued] == [report.id]

    actioned = remove_report(report.id, "mod_user", "spam content")
    assert actioned.status == "actioned"
    assert actioned.resolved_by == "mod_user"
    assert actioned.resolved_at is not None
    assert actioned.resolution_note == "spam content"

    refreshed = db.session.get(Post, post.id)
    assert refreshed.removed is True
    assert refreshed.removed_by == "mod_user"
    assert refreshed.removal_reason == "spam content"
    assert refreshed.removed_at is not None

    # Soft removal: the row persists.
    assert db.session.get(Post, post.id) is not None
    # No longer in the open queue; visible under 'actioned'.
    assert list_reports("open").count() == 0
    assert [r.id for r in list_reports("actioned")] == [report.id]

    # Author got the mod_action notification.
    row = (
        db.session.query(Notification)
        .filter_by(recipient="alice", kind="mod_action")
        .one()
    )
    assert row.actor == "mod_user"
    assert row.post_id == post.id


def test_remove_flow_keeps_karma_math_balanced(seeded_db, db_session, moderator):
    """Soft removal must not corrupt the vote-authoritative karma math."""
    post = seeded_db["posts"][0]
    comment = seeded_db["comments"][1]  # alice's comment on bob's post
    assert cast_vote("bob", "post", post.id, 1)["status"] == "ok"
    assert cast_vote("bob", "comment", comment.id, 1)["status"] == "ok"

    before = recompute_scores_and_karma()
    karma_before = {
        u.username: (u.post_karma, u.comment_karma)
        for u in db.session.query(User).all()
    }

    report = report_content("bob", "post", post.id, "junk")
    remove_report(report.id, "mod_user", "junk")

    after = recompute_scores_and_karma()
    karma_after = {
        u.username: (u.post_karma, u.comment_karma)
        for u in db.session.query(User).all()
    }
    assert karma_after == karma_before
    assert after["drift_votes"] == before["drift_votes"] == 0
    # The removed post's Vote row survived the soft removal.
    assert after["repaired"] >= before["repaired"]


def test_cannot_report_removed_item(seeded_db, db_session):
    post = seeded_db["posts"][0]
    post.removed = True
    db.session.commit()
    with pytest.raises(ValueError, match="already removed"):
        report_content("bob", "post", post.id, "still reporting")


def test_report_validation_rejections(seeded_db, db_session):
    with pytest.raises(ValueError, match="non-empty"):
        report_content("bob", "post", 1, "   ")
    with pytest.raises(ValueError, match="unknown report target"):
        report_content("bob", "story", 1, "why")
    with pytest.raises(ValueError, match="does not exist"):
        report_content("ghost", "post", 1, "why")
    with pytest.raises(ValueError, match="does not exist"):
        report_content("bob", "post", 99999, "why")


def test_dismiss_flow_leaves_item_untouched(seeded_db, db_session, moderator):
    comment = seeded_db["comments"][0]
    report = report_content("alice", "comment", comment.id, "disagree")
    dismissed = dismiss_report(report.id, "mod_user", note="fine actually")
    assert dismissed.status == "dismissed"
    assert dismissed.resolved_by == "mod_user"
    assert dismissed.resolution_note == "fine actually"

    fresh = db_session.get(type(comment), comment.id)
    assert fresh.removed is False
    assert fresh.removed_at is None

    with pytest.raises(ValueError, match="not open"):
        dismiss_report(report.id, "mod_user")
    with pytest.raises(ValueError, match="not open"):
        remove_report(report.id, "mod_user", "too late")


# ---------------------------------------------------------------------------
# 2. Bans gate voting and content creation


def test_site_wide_ban_blocks_everything(seeded_db, db_session):
    post = seeded_db["posts"][2]  # alice's post in askdeaddit
    ban_user("bob", "spamming", banned_by="mod_user")

    ban = active_ban_for("bob", "testsub")
    assert ban is not None and ban.subdeaddit_name is None
    assert "banned by mod_user" in ban.reason

    result = cast_vote("bob", "post", post.id, 1)
    assert result == {
        "status": "rejected",
        "reason": "user 'bob' is banned",
        "score": int(post.score or 0),
    }
    with pytest.raises(ContentValidationError, match="User 'bob' is banned"):
        create_post(title="hi", content="hello", user="bob", subdeaddit="askdeaddit")
    with pytest.raises(ContentValidationError, match="User 'bob' is banned"):
        create_comment(post_id=post.id, content="hey", user="bob")


def test_sub_scoped_ban_only_blocks_that_sub(seeded_db, db_session):
    ban_user("bob", "testsub trouble", subdeaddit_name="testsub")

    # Blocked inside testsub (vote on alice's post there).
    result = cast_vote("bob", "post", seeded_db["posts"][0].id, 1)
    assert result["reason"] == "user 'bob' is banned"
    with pytest.raises(ContentValidationError, match="User 'bob' is banned"):
        create_post(title="nope", content="blocked", user="bob", subdeaddit="testsub")

    # Free elsewhere: vote on askdeaddit content and post there.
    other_post = seeded_db["posts"][2]
    assert cast_vote("bob", "post", other_post.id, 1)["status"] == "ok"
    created = create_post(
        title="elsewhere",
        content="allowed",
        user="bob",
        subdeaddit="askdeaddit",
    )
    assert created.subdeaddit_name == "askdeaddit"


def test_expired_ban_does_not_block_and_expire_bans_lifts_exact_rows(
    seeded_db, db_session
):
    stale = ban_user(
        "bob",
        "old",
        subdeaddit_name="testsub",
        expires_at=datetime.utcnow() - timedelta(hours=1),
    )
    live = ban_user(
        "alice", "current", expires_at=datetime.utcnow() + timedelta(days=7)
    )

    # Expired bans don't count as active.
    assert active_ban_for("bob", "testsub") is None
    assert cast_vote("bob", "post", seeded_db["posts"][2].id, 1)["status"] == "ok"

    # A still-active site-wide ban does count.
    forever = ban_user("bob", "permanent")  # no expiry
    assert active_ban_for("bob", "askdeaddit") is not None

    lifted = expire_bans()
    assert lifted == 1
    assert db.session.get(Ban, stale.id).lifted_at is not None
    assert db.session.get(Ban, live.id).lifted_at is None
    assert db.session.get(Ban, forever.id).lifted_at is None

    # Second run is a no-op.
    assert expire_bans() == 0


def test_lift_ban(seeded_db, db_session):
    ban = ban_user("bob", "cooled off")
    lifted = lift_ban(ban.id)
    assert lifted.lifted_at is not None
    assert active_ban_for("bob", "testsub") is None
    with pytest.raises(ValueError, match="already lifted"):
        lift_ban(ban.id)


def test_ban_unknown_user_rejected(seeded_db, db_session):
    with pytest.raises(ValueError, match="does not exist"):
        ban_user("ghost", "who?")


# ---------------------------------------------------------------------------
# 3. Removed-content rejection


def _remove_post_directly(db_session, post):
    if db_session.get(User, "mod_user") is None:
        db.session.add(User(username="mod_user", interests="[]"))
        db.session.commit()
    post.removed = True
    post.removed_by = "mod_user"
    post.removal_reason = "cleanup"
    post.removed_at = datetime.utcnow()
    db.session.commit()


def test_vote_on_removed_post_rejected(seeded_db, db_session):
    post = seeded_db["posts"][0]
    _remove_post_directly(db_session, post)
    result = cast_vote("bob", "post", post.id, 1)
    assert result["status"] == "rejected"
    assert result["reason"] == f"post {post.id} was removed"


def test_vote_on_removed_comment_rejected(seeded_db, db_session):
    """A removed comment itself rejects votes (the frozen D4 check)."""
    comment = seeded_db["comments"][0]
    comment.removed = True
    db.session.commit()
    result = cast_vote("alice", "comment", comment.id, 1)
    assert result["status"] == "rejected"
    assert result["reason"] == f"comment {comment.id} was removed"


def test_comment_on_removed_post_rejected(seeded_db, db_session):
    post = seeded_db["posts"][0]
    _remove_post_directly(db_session, post)
    with pytest.raises(
        ContentValidationError, match=f"Post '{post.id}' has been removed"
    ):
        create_comment(post_id=post.id, content="reply", user="bob")


@pytest.fixture()
def exploding_notify(monkeypatch):
    """Make notify_mod_action raise the moment it is called."""

    def boom(*args, **kwargs):
        raise RuntimeError("inbox subsystem down")

    monkeypatch.setattr(moderation.notifications, "notify_mod_action", boom)


def test_remove_report_survives_notification_failure(
    seeded_db, db_session, moderator, exploding_notify
):
    post = seeded_db["posts"][0]
    report = report_content("bob", "post", post.id, "spam")

    actioned = remove_report(report.id, "mod_user", "spam content")  # must not raise
    assert actioned.status == "actioned"

    fresh = db.session.get(Post, post.id)
    assert fresh.removed is True
    assert fresh.removed_by == "mod_user"

    # Session healthy afterwards: queries work, no PendingRollbackError.
    assert db_session.query(Report).filter_by(status="actioned").count() == 1
    assert db_session.get(User, "alice") is not None


def test_ban_user_survives_notification_failure(
    seeded_db, db_session, exploding_notify
):
    ban = ban_user("bob", "spamming", banned_by="mod_user")  # must not raise
    assert ban.id is not None
    assert ban.reason.startswith("banned by mod_user")

    # Session healthy afterwards.
    assert db_session.query(Ban).count() == 1
    assert active_ban_for("bob", "askdeaddit") is not None
