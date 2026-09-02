"""Phase D4 slice 3: public-surface exclusion + admin reports queue.

Covers:
- Removed posts disappear from feeds (/), subdeaddit pages, search,
  profile tabs and /api/posts; direct links stay reachable with a
  tombstone notice and surviving comments.
- Removed comments render as tombstone nodes (content suppressed,
  children kept) on the web and are marked "removed": true in the API
  tree.
- Admin reports queue lists open reports and remove/dismiss/ban actions
  mutate state through the moderation service.
"""

from datetime import datetime

import pytest

from deaddit.models import Ban, Comment, Post, Report, Setting, User


@pytest.fixture()
def admin_user(app, db_session):
    """The human-moderator identity the admin queue acts as.

    Report.resolved_by / removed_by carry FKs to user.username, so the
    queue's 'admin' identity needs a row to point at.
    """
    user = User(username="admin", model="human")
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture()
def admin_session(client, admin_user):
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


@pytest.fixture()
def removal_scenario(seeded_db, db_session):
    """Seed one removed post, one removed comment (with a surviving reply)."""
    Setting.set_value("SETUP_COMPLETED_AT", "2026-01-01T00:00:00Z")
    post = seeded_db["posts"][0]  # "Hello World" by alice in testsub

    parent = Comment(
        post_id=post.id,
        user="bob",
        content="parent comment body",
        model="test-model",
    )
    db_session.add(parent)
    db_session.flush()
    reply = Comment(
        post_id=post.id,
        parent_id=parent.id,
        user="alice",
        content="reply that must survive",
        model="test-model",
    )
    db_session.add(reply)
    db_session.commit()

    # Soft-remove via ORM (the service path is covered by sibling tests).
    post.removed = True
    post.removal_reason = "spam"
    post.removed_by = "bob"
    post.removed_at = datetime.utcnow()

    parent.removed = True
    parent.removal_reason = "off-topic"
    parent.removed_by = "bob"
    parent.removed_at = datetime.utcnow()

    db_session.commit()
    return {
        "post": post,
        "removed_comment": parent,
        "surviving_reply": reply,
    }


def _assert_absent(html: str, *needles: str):
    for needle in needles:
        assert needle not in html, f"expected absent: {needle!r}"


class TestFeedExclusion:
    def test_removed_post_hidden_from_index(self, client, removal_scenario):
        html = client.get("/").get_data(as_text=True)
        _assert_absent(html, "Hello World", "First post")
        assert "Seeded Post" in html  # control: untouched row still listed

    def test_removed_post_hidden_from_subdeaddit(self, client, removal_scenario):
        html = client.get("/d/testsub").get_data(as_text=True)
        _assert_absent(html, "Hello World")

    def test_removed_post_hidden_from_search(self, client, removal_scenario):
        html = client.get("/search?q=Hello").get_data(as_text=True)
        _assert_absent(html, "Hello World")

    def test_removed_post_hidden_from_profile_posts_tab(self, client, removal_scenario):
        html = client.get("/user/alice?tab=posts").get_data(as_text=True)
        _assert_absent(html, "Hello World")

    def test_removed_comment_hidden_from_profile_comments_tab(
        self, client, removal_scenario
    ):
        html = client.get("/user/bob?tab=comments").get_data(as_text=True)
        _assert_absent(html, "parent comment body")

    def test_removed_post_hidden_from_api_posts(self, client, removal_scenario):
        payload = client.get("/api/posts").get_json()
        ids = [p["id"] for p in payload["posts"]]
        assert removal_scenario["post"].id not in ids


class TestDirectLinkTombstone:
    def test_removed_post_direct_link_renders_tombstone_with_comments(
        self, client, removal_scenario
    ):
        post = removal_scenario["post"]
        resp = client.get(f"/d/testsub/{post.id}")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Post removed" in html  # tombstone notice
        _assert_absent(html, "First post")  # body suppressed
        _assert_absent(html, "Hello World")  # title suppressed (incl. <title>)
        # Non-removed comments stay reachable under the tombstone.
        assert "reply that must survive" in html

    def test_removed_comment_is_tombstone_node_with_children(
        self, client, removal_scenario
    ):
        post = removal_scenario["post"]
        html = client.get(f"/d/testsub/{post.id}").get_data(as_text=True)
        assert "[removed" in html
        assert "off-topic" in html  # removal reason surfaced on the node
        _assert_absent(html, "parent comment body")
        # Thread structure survives: the reply renders beneath its parent.
        assert "reply that must survive" in html
        assert f'id="comment-{removal_scenario["removed_comment"].id}"' in html

    def test_live_post_page_unchanged(self, client, seeded_db):
        post = seeded_db["posts"][0]
        resp = client.get(f"/d/testsub/{post.id}")
        html = resp.get_data(as_text=True)
        assert "Post removed" not in html
        assert "Welcome!" in html


class TestApiExclusion:
    def test_api_post_marks_removed_post(self, client, removal_scenario):
        payload = client.get(f"/api/post/{removal_scenario['post'].id}").get_json()
        assert payload["removed"] is True

    def test_api_post_marks_removed_comment_and_keeps_replies(
        self, client, removal_scenario
    ):
        payload = client.get(f"/api/post/{removal_scenario['post'].id}").get_json()
        tree = payload["comments"]
        # The removed comment keeps its tree position, marked; its replies
        # stay attached beneath it.
        removed_nodes = [n for n in tree if n["removed"]]
        assert len(removed_nodes) == 1
        node = removed_nodes[0]
        assert node["content"] == "[removed]"
        assert node["user"] is None
        replies = node["replies"]
        assert [r["content"] for r in replies] == ["reply that must survive"]
        assert all(r["removed"] is False for r in replies)
        # Live siblings are untouched.
        assert any(n["content"] == "Welcome!" and n["removed"] is False for n in tree)

    def test_api_post_live_comment_not_marked(self, client, seeded_db):
        payload = client.get(f"/api/post/{seeded_db['posts'][0].id}").get_json()
        assert payload["removed"] is False
        assert all(c["removed"] is False for c in payload["comments"])
        assert payload["comments"][0]["content"] == "Welcome!"


# --- Admin reports queue ---


@pytest.fixture()
def report_rows(seeded_db, db_session, admin_user):
    """Two open reports (one per target kind) plus one dismissed one."""
    post = seeded_db["posts"][0]
    comment = seeded_db["comments"][0]
    rows = [
        Report(reporter="bob", post_id=post.id, reason="spam"),
        Report(reporter="alice", comment_id=comment.id, reason="abuse"),
        Report(
            reporter="bob",
            post_id=seeded_db["posts"][1].id,
            reason="stale-dismissed-marker",
            status="dismissed",
        ),
    ]
    db_session.add_all(rows)
    db_session.commit()
    return {"rows": rows, "post": post, "comment": comment}


class TestReportsQueue:
    def test_queue_lists_open_reports(self, client, report_rows):
        resp = client.get("/admin/reports")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Moderation Reports" in html
        assert "spam" in html and "abuse" in html

    def test_remove_action_soft_removes_target(self, admin_session, report_rows):
        report = report_rows["rows"][0]
        resp = admin_session.post(
            f"/admin/reports/{report.id}/remove",
            data={"removal_reason": "confirmed-spam"},
        )
        assert resp.status_code == 302
        report = Report.query.get(report.id)
        assert report.status == "actioned"
        assert report.resolved_by == "admin"
        post = Post.query.get(report.post_id)
        assert post.removed is True
        assert post.removed_by == "admin"
        assert post.removal_reason == "confirmed-spam"

    def test_remove_without_admin_principal_redirects_without_mutation(
        self, client, db_session, report_rows, admin_user
    ):
        db_session.delete(admin_user)
        db_session.commit()
        report = report_rows["rows"][0]

        resp = client.post(f"/admin/reports/{report.id}/remove", data={})

        assert resp.status_code == 302
        assert Report.query.get(report.id).status == "open"
        assert Post.query.get(report.post_id).removed is False

    def test_dismiss_action_closes_report(self, client, report_rows):
        report = report_rows["rows"][1]
        resp = client.post(
            f"/admin/reports/{report.id}/dismiss", data={"note": "not abuse"}
        )
        assert resp.status_code == 302
        report = Report.query.get(report.id)
        assert report.status == "dismissed"
        assert report.resolution_note == "not abuse"

    def test_ban_action_site_wide(self, client, report_rows):
        report = report_rows["rows"][0]  # targets alice's post
        resp = client.post(
            f"/admin/reports/{report.id}/ban",
            data={"scope": "site", "reason": "repeat offender"},
        )
        assert resp.status_code == 302
        ban = Ban.query.one()
        assert ban.username == "alice"
        assert ban.subdeaddit_name is None
        assert ban.expires_at is None
        # No actor column: the service prefixes the moderator into reason.
        assert ban.reason.startswith("banned by admin:")

    def test_ban_action_subdeaddit_scoped_and_timed(self, client, report_rows):
        report = report_rows["rows"][1]  # targets bob's comment in testsub
        resp = client.post(
            f"/admin/reports/{report.id}/ban",
            data={
                "scope": "subdeaddit",
                "reason": "trolling",
                "duration_days": "7",
            },
        )
        assert resp.status_code == 302
        ban = Ban.query.one()
        assert ban.username == "bob"
        assert ban.subdeaddit_name == "testsub"
        assert ban.expires_at is not None

    def test_ban_unknown_user_handled_gracefully(
        self, client, db_session, seeded_db, admin_user
    ):
        """FK-less ghost author: service ValueError becomes flash+redirect."""
        post = Post(
            title="Ghost post title",
            content="ghost authored",
            user="alice",
            subdeaddit_name="testsub",
            model="m",
        )
        db_session.add(post)
        report = Report(reporter="alice", post_id=post.id, reason="ghost-report")
        db_session.add(report)
        db_session.commit()
        # Repoint the author at a nonexistent user via raw SQL (FKs relaxed
        # on this connection only) to exercise the service-side ValueError.
        from sqlalchemy import text

        db_session.execute(text("PRAGMA foreign_keys = OFF"))
        db_session.execute(
            text("UPDATE post SET user = 'ghost' WHERE id = :pid"),
            {"pid": post.id},
        )
        db_session.commit()

        resp = client.post(
            f"/admin/reports/{report.id}/ban",
            data={"scope": "site", "reason": "ban the ghost"},
        )
        assert resp.status_code == 302
        assert Ban.query.count() == 0
        assert Report.query.get(report.id).status == "open"

    def test_remove_nonexistent_report_flash_redirect(self, client, admin_user):
        resp = client.post("/admin/reports/9999/remove", data={})
        assert resp.status_code == 302

    def test_actioned_report_leaves_open_queue(self, client, report_rows):
        report = report_rows["rows"][0]
        client.post(
            f"/admin/reports/{report.id}/remove",
            data={"removal_reason": "confirmed-gone"},
        )
        html = client.get("/admin/reports").get_data(as_text=True)
        assert "confirmed-gone" not in html


def test_moderation_service_queue_contract():
    """Guard the service surface the queue relies on."""
    from deaddit.dynamics import moderation

    assert callable(moderation.remove_report)
    assert callable(moderation.dismiss_report)
    assert callable(moderation.ban_user)
    assert callable(moderation.list_reports)
