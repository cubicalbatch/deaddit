"""Tests for cascading deletion of users, posts, comments, and subdeaddits."""

from __future__ import annotations

import pytest

from deaddit.models import (
    Agent,
    AgentMemory,
    AgentRun,
    AgentTurn,
    Ban,
    Comment,
    Notification,
    Post,
    Report,
    Subdeaddit,
    SubdeadditModerator,
    ToolCall,
    User,
    Vote,
)


@pytest.fixture()
def admin_client(client):
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


def test_delete_user_cascades_posts_comments_and_responses(
    seeded_db, admin_client, db_session
):
    """Deleting a user removes their posts, comments, and all responses to their comments."""
    # Setup users
    user_a = User(username="AverageJoe37", bio="Joe", interests="[]")
    user_b = User(username="BobReply", bio="Bob", interests="[]")
    user_c = User(username="CharlieReply", bio="Charlie", interests="[]")
    user_d = User(username="DavidUnrelated", bio="David", interests="[]")
    db_session.add_all([user_a, user_b, user_c, user_d])
    db_session.commit()

    # Subdeaddit
    sub = Subdeaddit.query.first()

    # Post 1: authored by AverageJoe37
    post_a = Post(
        title="Joe's Post",
        content="Hello world",
        user=user_a.username,
        subdeaddit_name=sub.name,
    )
    # Post 2: authored by David (unrelated post)
    post_d = Post(
        title="David's Post",
        content="David's thoughts",
        user=user_d.username,
        subdeaddit_name=sub.name,
    )
    db_session.add_all([post_a, post_d])
    db_session.commit()

    post_a_id = post_a.id
    post_d_id = post_d.id

    # Comments on Post A:
    # C1 (Joe) -> C2 (Bob replies to Joe) -> C3 (Charlie replies to Bob)
    c1 = Comment(
        post_id=post_a_id, parent_id=None, user=user_a.username, content="Joe top"
    )
    db_session.add(c1)
    db_session.flush()
    c1_id = c1.id

    c2 = Comment(
        post_id=post_a_id, parent_id=c1_id, user=user_b.username, content="Bob reply"
    )
    db_session.add(c2)
    db_session.flush()
    c2_id = c2.id

    c3 = Comment(
        post_id=post_a_id,
        parent_id=c2_id,
        user=user_c.username,
        content="Charlie reply",
    )
    db_session.add(c3)
    db_session.flush()
    c3_id = c3.id

    # Comments on Post D (David's post):
    # C4 (David top) -> C5 (Joe replies to David) -> C6 (Bob replies to Joe) -> C7 (Charlie replies to Bob)
    # C8 (David top standalone)
    c4 = Comment(
        post_id=post_d_id, parent_id=None, user=user_d.username, content="David top"
    )
    c8 = Comment(
        post_id=post_d_id, parent_id=None, user=user_d.username, content="David other"
    )
    db_session.add_all([c4, c8])
    db_session.flush()
    c4_id = c4.id
    c8_id = c8.id

    c5 = Comment(
        post_id=post_d_id,
        parent_id=c4_id,
        user=user_a.username,
        content="Joe reply on David post",
    )
    db_session.add(c5)
    db_session.flush()
    c5_id = c5.id

    c6 = Comment(
        post_id=post_d_id,
        parent_id=c5_id,
        user=user_b.username,
        content="Bob reply to Joe on David post",
    )
    db_session.add(c6)
    db_session.flush()
    c6_id = c6.id

    c7 = Comment(
        post_id=post_d_id,
        parent_id=c6_id,
        user=user_c.username,
        content="Charlie deep reply",
    )
    db_session.add(c7)
    db_session.flush()
    c7_id = c7.id

    # Add votes
    # Joe voted on David's post and David's comment C8
    v1 = Vote(voter=user_a.username, post_id=post_d_id, comment_id=None, value=1)
    v2 = Vote(voter=user_a.username, post_id=None, comment_id=c8_id, value=1)
    # Bob voted on Joe's comment C5 and Charlie voted on C6
    v3 = Vote(voter=user_b.username, post_id=None, comment_id=c5_id, value=1)
    v4 = Vote(voter=user_c.username, post_id=None, comment_id=c6_id, value=1)
    # David voted on Joe's post
    v5 = Vote(voter=user_d.username, post_id=post_a_id, comment_id=None, value=1)
    db_session.add_all([v1, v2, v3, v4, v5])
    db_session.flush()
    v1_id, v2_id, v3_id, v4_id, v5_id = v1.id, v2.id, v3.id, v4.id, v5.id

    # Add notifications
    n1 = Notification(
        recipient=user_a.username,
        kind="reply",
        actor=user_b.username,
        post_id=post_d_id,
        comment_id=c5_id,
    )
    n2 = Notification(
        recipient=user_b.username,
        kind="reply",
        actor=user_a.username,
        post_id=post_d_id,
        comment_id=c4_id,
    )
    n3 = Notification(
        recipient=user_c.username,
        kind="reply",
        actor=user_b.username,
        post_id=post_d_id,
        comment_id=c6_id,
    )
    db_session.add_all([n1, n2, n3])
    db_session.flush()
    n1_id, n2_id, n3_id = n1.id, n2.id, n3.id

    # Add reports
    r1 = Report(reporter=user_a.username, post_id=post_d_id, reason="spam")
    r2 = Report(reporter=user_b.username, comment_id=c5_id, reason="rude")
    r3 = Report(
        reporter=user_c.username,
        post_id=post_a_id,
        reason="offtopic",
        resolved_by=user_a.username,
    )
    db_session.add_all([r1, r2, r3])
    db_session.flush()
    r1_id, r2_id, r3_id = r1.id, r2.id, r3.id

    # Add agent & memory for Joe
    agent_a = Agent(
        user_username=user_a.username,
        autonomy_tier="regular",
        is_enabled=False,
        status="idle",
        config={},
        state={},
    )
    mem_a = AgentMemory(user_username=user_a.username, content="Joe likes tech")
    db_session.add_all([agent_a, mem_a])
    db_session.flush()
    agent_a_id = agent_a.id
    mem_a_id = mem_a.id

    run_a = AgentRun(
        agent_id=agent_a.id, persona_username=user_a.username, status="completed"
    )
    db_session.add(run_a)
    db_session.flush()
    run_a_id = run_a.id

    turn_a = AgentTurn(run_id=run_a.id, seq=0, request_messages=[], response_message={})
    db_session.add(turn_a)
    db_session.flush()
    turn_a_id = turn_a.id

    tc_a = ToolCall(
        run_id=run_a.id, turn_id=turn_a.id, name="browse_feed", arguments={}
    )
    db_session.add(tc_a)
    db_session.flush()
    tc_a_id = tc_a.id

    # Subdeaddit mod and ban
    mod_a = SubdeadditModerator(subdeaddit_name=sub.name, username=user_a.username)
    ban_a = Ban(username=user_a.username, subdeaddit_name=sub.name, reason="test")
    db_session.add_all([mod_a, ban_a])

    # Post removed_by moderation
    post_d.removed_by = user_a.username
    c8.removed_by = user_a.username
    db_session.commit()

    username_a = user_a.username

    # Now delete user AverageJoe37 via admin API
    resp = admin_client.delete(f"/admin/api/users/{username_a}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["deleted"]["user"] == "AverageJoe37"
    assert data["deleted"]["posts"] == 1
    # Total comments deleted should include:
    # On Post A: C1, C2, C3 (3 comments)
    # On Post D: C5 (Joe), C6 (Bob response), C7 (Charlie response) (3 comments)
    # Total = 6 comments deleted
    assert data["deleted"]["comments"] == 6

    # Expire / clear test session to see fresh DB state
    db_session.expire_all()

    # Verify user is gone
    assert db_session.get(User, username_a) is None
    # Verify Joe's post is gone
    assert db_session.get(Post, post_a_id) is None
    # Verify David's post still exists
    assert db_session.get(Post, post_d_id) is not None
    # Verify Post D's removed_by was reset to None
    post_d_db = db_session.get(Post, post_d_id)
    assert post_d_db.removed_by is None

    # Verify comments on Post A are all gone
    assert db_session.get(Comment, c1_id) is None
    assert db_session.get(Comment, c2_id) is None
    assert db_session.get(Comment, c3_id) is None

    # Verify comments on Post D:
    # C4 and C8 (David's comments) still exist
    assert db_session.get(Comment, c4_id) is not None
    assert db_session.get(Comment, c8_id) is not None
    assert db_session.get(Comment, c8_id).removed_by is None
    # C5, C6, C7 (Joe's comment and its response subtree) are all gone
    assert db_session.get(Comment, c5_id) is None
    assert db_session.get(Comment, c6_id) is None
    assert db_session.get(Comment, c7_id) is None

    # Verify votes
    assert db_session.get(Vote, v1_id) is None
    assert db_session.get(Vote, v2_id) is None
    assert db_session.get(Vote, v3_id) is None
    assert db_session.get(Vote, v4_id) is None
    assert db_session.get(Vote, v5_id) is None

    # Verify notifications
    assert db_session.get(Notification, n1_id) is None
    assert db_session.get(Notification, n2_id) is None
    assert db_session.get(Notification, n3_id) is None

    # Verify reports
    assert db_session.get(Report, r1_id) is None
    assert db_session.get(Report, r2_id) is None
    assert db_session.get(Report, r3_id) is None

    # Verify agent and runs are cleaned up
    assert db_session.get(Agent, agent_a_id) is None
    assert db_session.get(AgentRun, run_a_id) is None
    assert db_session.get(AgentTurn, turn_a_id) is None
    assert db_session.get(ToolCall, tc_a_id) is None
    assert db_session.get(AgentMemory, mem_a_id) is None

    # Verify mod and ban are cleaned up
    assert (
        SubdeadditModerator.query.filter_by(
            subdeaddit_name=sub.name, username=username_a
        ).first()
        is None
    )
    assert Ban.query.filter_by(username=username_a).first() is None


def test_bulk_delete_users_cascades(seeded_db, admin_client, db_session):
    """Bulk deleting users cleans up all posts, comments, and responses for all selected users."""
    u1 = User(username="bulk_user_1", bio="", interests="[]")
    u2 = User(username="bulk_user_2", bio="", interests="[]")
    u3 = User(username="bulk_user_3_innocent", bio="", interests="[]")
    db_session.add_all([u1, u2, u3])
    db_session.commit()

    sub = Subdeaddit.query.first()
    p1 = Post(title="P1", content="", user=u1.username, subdeaddit_name=sub.name)
    p2 = Post(title="P2", content="", user=u2.username, subdeaddit_name=sub.name)
    p3 = Post(title="P3", content="", user=u3.username, subdeaddit_name=sub.name)
    db_session.add_all([p1, p2, p3])
    db_session.commit()

    p3_id = p3.id

    # Comments
    c1 = Comment(
        post_id=p3_id, parent_id=None, user=u1.username, content="u1 comment on p3"
    )
    db_session.add(c1)
    db_session.flush()
    c1_id = c1.id

    c2 = Comment(
        post_id=p3_id, parent_id=c1_id, user=u2.username, content="u2 reply to u1"
    )
    db_session.add(c2)
    db_session.flush()
    c2_id = c2.id

    c3 = Comment(
        post_id=p3_id, parent_id=c2_id, user=u3.username, content="u3 reply to u2"
    )
    db_session.add(c3)
    db_session.commit()
    c3_id = c3.id

    resp = admin_client.post(
        "/admin/api/users/bulk-delete",
        json={"usernames": ["bulk_user_1", "bulk_user_2"]},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["deleted"]["users"] == 2
    assert data["deleted"]["posts"] == 2
    assert data["deleted"]["comments"] == 3

    db_session.expire_all()

    assert db_session.get(User, "bulk_user_1") is None
    assert db_session.get(User, "bulk_user_2") is None
    assert db_session.get(User, "bulk_user_3_innocent") is not None
    assert db_session.get(Post, p3_id) is not None
    assert db_session.get(Comment, c1_id) is None
    assert db_session.get(Comment, c2_id) is None
    assert db_session.get(Comment, c3_id) is None


def test_delete_comment_cascades_responses_and_votes(
    seeded_db, admin_client, db_session
):
    """Deleting a comment recursively deletes all child comments and their votes."""
    u = User.query.first()
    sub = Subdeaddit.query.first()
    p = Post(
        title="Post with thread", content="", user=u.username, subdeaddit_name=sub.name
    )
    db_session.add(p)
    db_session.commit()
    p_id = p.id

    c1 = Comment(post_id=p_id, parent_id=None, user=u.username, content="Top")
    db_session.add(c1)
    db_session.flush()
    c1_id = c1.id

    c2 = Comment(post_id=p_id, parent_id=c1_id, user=u.username, content="Reply 1")
    db_session.add(c2)
    db_session.flush()
    c2_id = c2.id

    c3 = Comment(post_id=p_id, parent_id=c2_id, user=u.username, content="Reply 2")
    db_session.add(c3)
    db_session.flush()
    c3_id = c3.id

    v = Vote(voter=u.username, post_id=None, comment_id=c3_id, value=1)
    db_session.add(v)
    db_session.commit()
    v_id = v.id

    resp = admin_client.delete(f"/admin/api/comments/{c1_id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["deleted"]["comment"] == c1_id
    assert data["deleted"]["child_comments"] == 2

    db_session.expire_all()

    assert db_session.get(Comment, c1_id) is None
    assert db_session.get(Comment, c2_id) is None
    assert db_session.get(Comment, c3_id) is None
    assert db_session.get(Vote, v_id) is None
