"""Admin API round-trip and validation for user subscriptions."""

import pytest

from deaddit.extensions import db
from deaddit.models import User


@pytest.fixture()
def admin_client(client):
    """Client authenticated as admin."""
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


def test_user_subscriptions_round_trip_and_clear(admin_client, app, seeded_db):
    with app.app_context():
        # Update with list of subdeaddits
        resp = admin_client.put(
            "/admin/api/users/alice",
            json={"subscriptions": ["testsub", "askdeaddit"]},
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

        user = db.session.get(User, "alice")
        assert user.agent_state["subscriptions"] == ["askdeaddit", "testsub"]

        # GET single user
        resp = admin_client.get("/admin/api/users/alice")
        assert resp.status_code == 200
        assert resp.get_json()["subscriptions"] == ["askdeaddit", "testsub"]

        # GET users list
        resp = admin_client.get("/admin/api/users")
        assert resp.status_code == 200
        users = resp.get_json()["users"]
        alice_entry = next(u for u in users if u["username"] == "alice")
        assert alice_entry["subscriptions"] == ["askdeaddit", "testsub"]

        # Update with comma-separated string with prefixes
        resp = admin_client.put(
            "/admin/api/users/alice",
            json={"subscriptions": "d/testsub, r/askdeaddit"},
        )
        assert resp.status_code == 200
        assert user.agent_state["subscriptions"] == ["askdeaddit", "testsub"]

        # Update with JSON array string
        resp = admin_client.put(
            "/admin/api/users/alice",
            json={"subscriptions": '["testsub"]'},
        )
        assert resp.status_code == 200
        assert user.agent_state["subscriptions"] == ["testsub"]

        # Clear subscriptions with empty string / empty list
        resp = admin_client.put(
            "/admin/api/users/alice",
            json={"subscriptions": ""},
        )
        assert resp.status_code == 200
        assert "subscriptions" not in (user.agent_state or {})
        assert (
            admin_client.get("/admin/api/users/alice").get_json()["subscriptions"] == []
        )


def test_user_subscriptions_reject_invalid_subdeaddit(admin_client, app, seeded_db):
    with app.app_context():
        resp = admin_client.put(
            "/admin/api/users/alice",
            json={"subscriptions": ["nonexistent_sub"]},
        )
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False
        assert "does not exist" in resp.get_json()["error"]
