from __future__ import annotations

from datetime import datetime, timedelta

from deaddit.models import Comment, Post


def test_max_comments_is_applied_before_limit_and_keeps_zero_comment_posts(
    client, db_session, seeded_db
):
    now = datetime.utcnow() + timedelta(days=1)
    newer = Post(
        title="newer",
        content="",
        user="alice",
        subdeaddit_name="testsub",
        created_at=now,
    )
    older = Post(
        title="older",
        content="",
        user="alice",
        subdeaddit_name="testsub",
        created_at=now - timedelta(seconds=1),
    )
    db_session.add_all([newer, older])
    db_session.flush()
    db_session.add_all(
        [
            Comment(post_id=newer.id, user="alice", content="first"),
            Comment(post_id=newer.id, user="alice", content="second"),
        ]
    )
    db_session.commit()

    response = client.get("/api/posts?limit=1&max_comments=0")

    assert response.status_code == 200
    entries = response.get_json()["posts"]
    assert len(entries) == 1
    assert entries[0]["id"] == older.id
    assert entries[0]["title"] == "older"
    assert entries[0]["comment_count"] == 0


def test_api_posts_reports_grouped_comment_counts(client, db_session, seeded_db):
    post = Post(
        title="counted",
        content="",
        user="alice",
        subdeaddit_name="testsub",
    )
    db_session.add(post)
    db_session.flush()
    db_session.add_all(
        [
            Comment(post_id=post.id, user="alice", content="first"),
            Comment(post_id=post.id, user="alice", content="second"),
            Comment(post_id=post.id, user="alice", content="third"),
        ]
    )
    db_session.commit()

    entries = client.get("/api/posts?title=counted").get_json()["posts"]

    assert len(entries) == 1
    assert entries[0]["id"] == post.id
    assert entries[0]["comment_count"] == 3


def test_api_posts_limit_is_clamped(client, db_session, seeded_db):
    db_session.add_all(
        [
            Post(
                title=f"post-{index}",
                content="",
                user="alice",
                subdeaddit_name="testsub",
            )
            for index in range(101)
        ]
    )
    db_session.commit()

    assert len(client.get("/api/posts?limit=-1").get_json()["posts"]) == 1
    assert len(client.get("/api/posts?limit=not-a-number").get_json()["posts"]) == 50
    assert len(client.get("/api/posts?limit=999999").get_json()["posts"]) == 100
