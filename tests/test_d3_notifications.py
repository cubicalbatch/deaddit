"""Tests for Phase D3 slice S5: notification emission, inbox contract,
agent wiring and nightly purge."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import event

from deaddit.agents.executor import execute
from deaddit.agents.memory import build_initial_messages
from deaddit.agents.registry import ToolContext
from deaddit.dynamics.inbox import (
    get_inbox,
    mark_inbox_read,
    purge_read_notifications,
    unread_count,
)
from deaddit.models import (
    Agent,
    AgentMemory,
    AgentRun,
    Comment,
    Notification,
    Post,
    User,
)
from deaddit.runtime.nightly import NIGHTLY_JOBS
from deaddit.services import content as content_service
from deaddit.services.content import create_comment, create_post


@pytest.fixture()
def cache_spy(monkeypatch):
    """Replace _clear_read_caches with a recorder; returns the call list."""
    calls = []
    monkeypatch.setattr(
        content_service, "_clear_read_caches", lambda: calls.append("clear")
    )
    return calls


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


def _notif(db_session, recipient, *, kind="reply", actor="bob", hours_old=0, read=None):
    row = Notification(
        recipient=recipient,
        kind=kind,
        actor=actor,
        post_id=Post.query.first().id,
        created_at=datetime.utcnow() - timedelta(hours=hours_old),
    )
    if read is not None:
        row.read_at = read
    db_session.add(row)
    db_session.commit()
    return row


# ---------------------------------------------------------------------------
# 1. Emission matrix


def test_reply_to_comment_notifies_parent_author(seeded_db, db_session, cache_spy):
    parent = seeded_db["comments"][0]  # bob's comment on alice's post

    reply = create_comment(
        post_id=parent.post_id,
        content="thanks bob",
        user="alice",
        parent_id=parent.id,
    )

    rows = Notification.query.all()
    assert len(rows) == 1
    row = rows[0]
    assert row.kind == "reply"
    assert row.recipient == "bob"
    assert row.actor == "alice"
    assert row.comment_id == reply.id
    assert row.post_id == parent.post_id
    assert row.snippet == "thanks bob"


def test_snippet_frozen_at_200_chars(seeded_db, db_session, cache_spy):
    long_content = "x" * 300

    create_comment(
        post_id=seeded_db["posts"][0].id,
        content=long_content,
        user="bob",
    )

    rows = Notification.query.all()
    assert len(rows) == 1
    assert rows[0].snippet == long_content[:200]
    assert len(rows[0].snippet) == 200


def test_top_level_comment_notifies_post_author(seeded_db, db_session, cache_spy):
    post = seeded_db["posts"][1]  # bob's post

    comment = create_comment(post_id=post.id, content="nice post", user="alice")

    rows = Notification.query.all()
    assert len(rows) == 1
    assert rows[0].kind == "reply"
    assert rows[0].recipient == "bob"
    assert rows[0].comment_id == comment.id


def test_self_reply_emits_nothing(seeded_db, db_session, cache_spy):
    own = seeded_db["comments"][0]  # bob's comment

    create_comment(
        post_id=own.post_id, content="self reply", user="bob", parent_id=own.id
    )

    assert Notification.query.count() == 0


def test_self_top_level_comment_on_own_post_emits_nothing(
    seeded_db, db_session, cache_spy
):
    create_comment(
        post_id=seeded_db["posts"][1].id,  # bob's post
        content="talking to myself",
        user="bob",
    )

    assert Notification.query.count() == 0


def test_mention_notifies_existing_user_unknown_token_ignored(
    seeded_db, db_session, cache_spy
):
    # Alice comments on her own post: the reply leg self-suppresses, leaving
    # mentions as the only possible source of rows.
    create_comment(
        post_id=seeded_db["posts"][0].id,  # alice's post
        content="@bob @ghost_404 take a look, cc @bob again",
        user="alice",
    )

    rows = Notification.query.all()
    assert len(rows) == 1
    assert rows[0].kind == "mention"
    assert rows[0].recipient == "bob"
    assert rows[0].actor == "alice"


def test_mention_of_only_unknown_token_emits_nothing(seeded_db, db_session, cache_spy):
    create_comment(
        post_id=seeded_db["posts"][0].id,
        content="@nobody cares",
        user="alice",
    )

    assert Notification.query.count() == 0


def test_hourly_dedupe_two_replies_same_actor_same_parent_one_row(
    seeded_db, db_session, cache_spy
):
    parent = seeded_db["comments"][0]

    create_comment(
        post_id=parent.post_id, content="first!", user="alice", parent_id=parent.id
    )
    create_comment(
        post_id=parent.post_id, content="second!", user="alice", parent_id=parent.id
    )

    rows = Notification.query.all()
    assert len(rows) == 1
    # The surviving row points at the *first* reply; the second was deduped.
    first_reply = Comment.query.filter_by(content="first!").one()
    assert rows[0].comment_id == first_reply.id


def test_post_created_mention_notifies_existing_user(seeded_db, db_session, cache_spy):
    post = create_post(
        title="Ping",
        content="hey @bob look at this",
        user="alice",
        subdeaddit="testsub",
    )

    rows = Notification.query.all()
    assert len(rows) == 1
    assert rows[0].kind == "mention"
    assert rows[0].recipient == "bob"
    assert rows[0].post_id == post.id
    assert rows[0].comment_id is None


# ---------------------------------------------------------------------------
# 2. Failure isolation


def test_notification_insert_failure_does_not_break_create_comment(
    seeded_db, db_session, cache_spy, monkeypatch
):
    """Isolation contract: a failed Notification insert must not break
    content creation.

    Emission failure is contained: ``notify_comment_created`` rolls back the
    session state it dirtied and swallows the error, so ``create_comment``
    returns normally with the comment persisted, no Notification row written,
    and the shared session immediately usable again.
    """

    def boom(mapper, connection, target):
        raise RuntimeError("simulated insert failure")

    event.listen(Notification, "before_insert", boom)
    try:
        comment = create_comment(
            post_id=seeded_db["posts"][1].id, content="hello bob", user="alice"
        )
    finally:
        event.remove(Notification, "before_insert", boom)

    # The comment itself persisted and create_comment returned normally.
    assert db_session.get(Comment, comment.id) is not None
    assert Notification.query.count() == 0

    # The shared session is still usable afterwards.
    user = User(username="carol", bio="c", interests="[]")
    db_session.add(user)
    db_session.commit()
    assert db_session.get(User, "carol") is not None


# ---------------------------------------------------------------------------
# 3. Inbox contract


def _seed_inbox(db_session, recipient, n, *, start_hours_ago=10):
    base = datetime.utcnow() - timedelta(hours=start_hours_ago)
    rows = []
    for i in range(n):
        row = Notification(
            recipient=recipient,
            kind="reply",
            actor="bob",
            created_at=base + timedelta(minutes=i),
        )
        db_session.add(row)
        rows.append(row)
    db_session.commit()
    return rows


def test_get_inbox_newest_first(seeded_db, db_session):
    rows = _seed_inbox(db_session, "alice", 3)

    data = get_inbox("alice")

    assert [item["id"] for item in data["items"]] == [
        rows[2].id,
        rows[1].id,
        rows[0].id,
    ]
    assert data["next_cursor"] is None
    assert data["unread"] == 3


def test_get_inbox_unread_only_excludes_read_rows(seeded_db, db_session):
    rows = _seed_inbox(db_session, "alice", 3)
    rows[0].read_at = datetime.utcnow()
    db_session.commit()

    data = get_inbox("alice")

    assert [item["id"] for item in data["items"]] == [rows[2].id, rows[1].id]
    everything = get_inbox("alice", unread_only=False)
    assert {item["id"] for item in everything["items"]} == {
        rows[0].id,
        rows[1].id,
        rows[2].id,
    }


def test_mark_inbox_read_subset_is_deterministic(seeded_db, db_session):
    rows = _seed_inbox(db_session, "alice", 4)

    assert mark_inbox_read("alice", ids=[rows[0].id, rows[1].id]) == {"count": 2}
    # Repeating the same flip is a no-op.
    assert mark_inbox_read("alice", ids=[rows[0].id, rows[1].id]) == {"count": 0}

    db_session.expire_all()
    assert db_session.get(Notification, rows[0].id).read_at is not None
    assert db_session.get(Notification, rows[1].id).read_at is not None
    assert db_session.get(Notification, rows[2].id).read_at is None
    assert unread_count("alice") == 2


def test_mark_inbox_read_all_flips_everything(seeded_db, db_session):
    _seed_inbox(db_session, "alice", 3)

    assert mark_inbox_read("alice", ids="all") == {"count": 3}
    assert mark_inbox_read("alice", ids="all") == {"count": 0}
    assert unread_count("alice") == 0


def test_mark_inbox_read_ignores_other_users_ids(seeded_db, db_session):
    mine = _seed_inbox(db_session, "alice", 1)
    theirs = _seed_inbox(db_session, "bob", 1)

    result = mark_inbox_read("alice", ids=[theirs[0].id])

    assert result == {"count": 0}
    db_session.expire_all()
    assert db_session.get(Notification, theirs[0].id).read_at is None
    assert unread_count("bob") == 1
    # Own rows remain untouched by someone else's scoping attempt.
    assert unread_count("alice") == 1
    assert mine[0].read_at is None


def test_keyset_pagination_stable_under_midwalk_insert(seeded_db, db_session):
    rows = _seed_inbox(db_session, "alice", 5)
    original_ids = {row.id for row in rows}

    page1 = get_inbox("alice", limit=2)
    walked = [item["id"] for item in page1["items"]]
    assert page1["next_cursor"] is not None

    # A brand-new (newer) notification lands while we are mid-walk.
    newcomer = _notif(db_session, "alice", hours_old=0)

    cursor = page1["next_cursor"]
    while cursor is not None:
        page = get_inbox("alice", limit=2, cursor=cursor)
        walked.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]

    # No duplicates, no gaps, and the mid-walk insert never leaks into the walk.
    assert sorted(walked) == sorted(original_ids)
    assert len(walked) == len(set(walked))
    assert newcomer.id not in walked

    # A fresh reader sees all six, newest first.
    fresh = get_inbox("alice", limit=100)
    assert fresh["items"][0]["id"] == newcomer.id
    assert len(fresh["items"]) == 6


def test_unread_count_consistent_with_get_inbox(seeded_db, db_session):
    rows = _seed_inbox(db_session, "alice", 3)

    assert get_inbox("alice")["unread"] == unread_count("alice") == 3

    mark_inbox_read("alice", ids=[rows[0].id])
    assert get_inbox("alice")["unread"] == unread_count("alice") == 2


def test_get_inbox_scoped_to_recipient(seeded_db, db_session):
    _seed_inbox(db_session, "alice", 2)
    _seed_inbox(db_session, "bob", 1)

    assert len(get_inbox("alice")["items"]) == 2
    assert len(get_inbox("bob")["items"]) == 1


# ---------------------------------------------------------------------------
# 4. Agent wiring


def test_view_inbox_tool_returns_items_and_marks_them_read(
    seeded_db, db_session, cache_spy
):
    ctx = _make_ctx(db_session, username="alice")
    rows = _seed_inbox(db_session, "alice", 2)

    first = execute("view_inbox", {}, ctx)

    assert first["ok"] is True
    assert {item["id"] for item in first["items"]} == {rows[0].id, rows[1].id}
    assert first["unread"] == 2

    db_session.expire_all()
    assert all(db_session.get(Notification, row.id).read_at is not None for row in rows)
    assert unread_count("alice") == 0

    second = execute("view_inbox", {}, ctx)
    assert second["ok"] is True
    assert second["items"] == []


def _agent_with_memory(db_session, username):
    agent = Agent(
        user_username=username,
        autonomy_tier="regular",
        is_enabled=True,
        status="idle",
        config={},
        state={},
        consecutive_failures=0,
    )
    db_session.add(agent)
    db_session.flush()
    db_session.add_all(
        [
            AgentMemory(
                user_username=agent.user_username,
                kind="backfill",
                content="likes testing",
            ),
            AgentMemory(
                user_username=agent.user_username,
                kind="episode",
                content="visited testsub",
            ),
        ]
    )
    db_session.commit()
    return agent


def test_build_initial_messages_includes_memory_then_unread_notice(
    seeded_db, db_session
):
    agent = _agent_with_memory(db_session, "alice")
    _seed_inbox(db_session, "alice", 2)

    messages, _ = build_initial_messages(agent, db_session.get(User, "alice"))
    kickoff = messages[-1]["content"]

    assert "Your memory:" in kickoff
    assert "Recent visits:" in kickoff
    assert "You have 2 unread replies" in kickoff
    assert "view_inbox" in kickoff
    # Memory sections come before the unread notice.
    assert kickoff.index("Your memory:") < kickoff.index("You have 2 unread replies")


def test_build_initial_messages_omits_notice_when_nothing_unread(seeded_db, db_session):
    agent = _agent_with_memory(db_session, "alice")

    messages, _ = build_initial_messages(agent, db_session.get(User, "alice"))
    kickoff = messages[-1]["content"]

    assert "Your memory:" in kickoff
    assert "Recent visits:" in kickoff
    assert "unread" not in kickoff


# ---------------------------------------------------------------------------
# 5. Nightly purge


def test_nightly_jobs_declare_notification_purge():
    jobs = {job.id: job for job in NIGHTLY_JOBS}
    assert "dynamics-notification-purge" in jobs
    assert jobs["dynamics-notification-purge"].func is purge_read_notifications


def test_purge_deletes_only_old_read_rows(seeded_db, db_session):
    utcnow = datetime.utcnow()
    old_read = Notification(
        recipient="alice",
        kind="reply",
        actor="bob",
        created_at=utcnow - timedelta(days=200),
        read_at=utcnow - timedelta(days=100),
    )
    fresh_read = Notification(
        recipient="alice",
        kind="reply",
        actor="bob",
        created_at=utcnow - timedelta(days=1),
        read_at=utcnow - timedelta(days=1),
    )
    old_unread = Notification(
        recipient="bob",
        kind="reply",
        actor="alice",
        created_at=utcnow - timedelta(days=200),
    )
    db_session.add_all([old_read, fresh_read, old_unread])
    db_session.commit()

    assert purge_read_notifications(max_age_days=90) == {"purged": 1}

    remaining = Notification.query.all()
    assert {row.id for row in remaining} == {fresh_read.id, old_unread.id}
