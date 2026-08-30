"""All-pages bulk delete: ``all: true`` applies the action to the full
filtered set server-side instead of a posted id list.

The admin content page's "select all pages" banner sends ``{all: true,
search, subdeaddit}`` to the four bulk-delete endpoints; these tests pin
that contract: everything matching the filter is deleted, everything
else is spared, and the plain id-list path still behaves as before.
"""

from __future__ import annotations

import pytest

from deaddit.models import Comment, Post, Subdeaddit, User

pytestmark = pytest.mark.usefixtures("seeded_db")


@pytest.fixture()
def admin_client(client):
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


def test_bulk_all_users_deletes_every_user(admin_client):
    resp = admin_client.post("/admin/api/users/bulk-delete", json={"all": True})
    assert resp.status_code == 200
    assert resp.get_json()["deleted"]["users"] == 2
    assert User.query.count() == 0


def test_bulk_all_users_respects_search_filter(admin_client):
    resp = admin_client.post(
        "/admin/api/users/bulk-delete", json={"all": True, "search": "alice"}
    )
    assert resp.status_code == 200
    assert resp.get_json()["deleted"]["users"] == 1
    assert User.query.filter_by(username="alice").first() is None
    assert User.query.filter_by(username="bob").first() is not None


def test_bulk_all_posts_respects_subdeaddit_filter(admin_client):
    resp = admin_client.post(
        "/admin/api/posts/bulk-delete",
        json={"all": True, "subdeaddit": "testsub"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["deleted"]["posts"] == 2
    # Only the askdeaddit post survives; testsub posts are gone.
    assert [p.title for p in Post.query.all()] == ["What is TDD?"]


def test_bulk_all_posts_without_filter_deletes_everything(admin_client):
    resp = admin_client.post("/admin/api/posts/bulk-delete", json={"all": True})
    assert resp.status_code == 200
    assert resp.get_json()["deleted"]["posts"] == 3
    assert Post.query.count() == 0


def test_bulk_all_comments_deletes_replies_too(admin_client, db_session):
    parent = Comment(post_id=Post.query.first().id, user="bob", content="parent")
    db_session.add(parent)
    db_session.flush()
    db_session.add(
        Comment(
            post_id=parent.post_id, parent_id=parent.id, user="alice", content="child"
        )
    )
    db_session.commit()

    resp = admin_client.post("/admin/api/comments/bulk-delete", json={"all": True})
    assert resp.status_code == 200
    body = resp.get_json()
    # 2 seeded + parent + child; the child counts as a reply of the set.
    assert body["deleted"]["comments"] == 4
    assert Comment.query.count() == 0


def test_bulk_all_comments_respects_search_filter(admin_client):
    resp = admin_client.post(
        "/admin/api/comments/bulk-delete",
        json={"all": True, "search": "Nice seed"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["deleted"]["comments"] == 1
    remaining = {c.content for c in Comment.query.all()}
    assert remaining == {"Welcome!"}


def test_bulk_all_subdeaddits_deletes_posts_and_comments(admin_client):
    resp = admin_client.post("/admin/api/subdeaddits/bulk-delete", json={"all": True})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["deleted"]["subdeaddits"] == 2
    assert body["deleted"]["posts"] == 3
    assert body["deleted"]["comments"] == 2
    assert Subdeaddit.query.count() == 0
    assert Post.query.count() == 0
    assert Comment.query.count() == 0


def test_bulk_all_matching_zero_rows_is_a_no_op_success(admin_client):
    resp = admin_client.post(
        "/admin/api/posts/bulk-delete",
        json={"all": True, "search": "no such title anywhere"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["deleted"]["posts"] == 0
    assert Post.query.count() == 3


def test_explicit_id_list_path_still_deletes_only_those(admin_client):
    post_id = Post.query.filter_by(subdeaddit_name="testsub").first().id
    resp = admin_client.post(
        "/admin/api/posts/bulk-delete", json={"post_ids": [post_id]}
    )
    assert resp.status_code == 200
    assert resp.get_json()["deleted"]["posts"] == 1
    assert Post.query.count() == 2
