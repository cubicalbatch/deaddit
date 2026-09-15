"""Admin human-user management: filtered list, password change, filtered bulk delete."""

from __future__ import annotations

import pytest

from deaddit.extensions import db
from deaddit.models import User


@pytest.fixture()
def admin_client(client, admin_login):
    return admin_login(client)


@pytest.fixture()
def human_user(app, db_session):
    """A registered human account (password hash set) among the seeded personas."""
    with app.app_context():
        user = User(
            username="charlie",
            password_hash="stored-hash",
            model="human",
            bio="",
            occupation="",
            education="",
            writing_style="",
            interests="[]",
            personality_traits="[]",
        )
        db_session.add(user)
        db_session.commit()
    return "charlie"


def test_human_filter_lists_only_human_accounts(admin_client, seeded_db, human_user):
    body = admin_client.get("/admin/api/users?human=1").get_json()
    assert [u["username"] for u in body["users"]] == ["charlie"]
    assert body["total"] == 1

    body = admin_client.get("/admin/api/users").get_json()
    assert {u["username"] for u in body["users"]} == {"alice", "bob", "charlie"}


def test_human_filter_combines_with_search(admin_client, seeded_db, human_user):
    body = admin_client.get("/admin/api/users?human=1&search=nobody").get_json()
    assert body["users"] == []
    assert body["total"] == 0


def test_password_change_enables_real_login(
    admin_client, app, db_session, monkeypatch, seeded_db, human_user
):
    monkeypatch.delenv("HUMAN_ACCOUNTS_ENABLED", raising=False)
    resp = admin_client.put(
        "/admin/api/users/charlie", json={"password": "new-secret-1"}
    )
    assert resp.get_json()["success"] is True

    # The changed password works through the real human login flow.
    resp = admin_client.post(
        "/login", data={"username": "charlie", "password": "new-secret-1"}
    )
    assert resp.status_code == 302

    admin_client.post("/logout")
    resp = admin_client.post(
        "/login", data={"username": "charlie", "password": "wrong-password"}
    )
    assert resp.status_code == 200  # form re-rendered with an error


def test_password_change_requires_admin(
    client, app, db_session, monkeypatch, seeded_db, human_user
):
    monkeypatch.setenv("API_TOKEN", "test-admin-token")
    resp = client.put("/admin/api/users/charlie", json={"password": "new-secret-1"})
    assert resp.status_code in (302, 401, 403)

    with app.app_context():
        user = db.session.get(User, "charlie")
        assert user.password_hash == "stored-hash"


def test_password_change_rejects_bad_lengths(
    admin_client, app, db_session, seeded_db, human_user
):
    for bad in ("short", "x" * 257, 12345):
        resp = admin_client.put("/admin/api/users/charlie", json={"password": bad})
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False

    with app.app_context():
        user = db.session.get(User, "charlie")
        assert user.password_hash == "stored-hash"


def test_bulk_delete_all_respects_human_filter(
    admin_client, app, db_session, seeded_db, human_user
):
    resp = admin_client.post(
        "/admin/api/users/bulk-delete", json={"all": True, "human": True}
    )
    assert resp.status_code == 200
    assert resp.get_json()["deleted"]["users"] == 1

    with app.app_context():
        remaining = {u.username for u in db.session.query(User).all()}
    assert remaining == {"alice", "bob"}


def test_human_password_survives_persona_edit(
    admin_client, app, db_session, seeded_db, human_user
):
    """A persona edit PUT must not clobber the account's password."""
    resp = admin_client.put("/admin/api/users/charlie", json={"bio": "updated"})
    assert resp.status_code == 200

    with app.app_context():
        user = db.session.get(User, "charlie")
        assert user.password_hash == "stored-hash"
