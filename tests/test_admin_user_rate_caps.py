"""Admin API round-trip for per-persona rate-cap overrides."""

import pytest

from deaddit.extensions import db
from deaddit.models import User


@pytest.fixture()
def admin_client(client):
    """Client authenticated as admin."""
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


def test_rate_caps_round_trip_and_clear(admin_client, app, seeded_db):
    with app.app_context():
        resp = admin_client.put(
            "/admin/api/users/alice",
            json={"rate_caps": {"post": 5, "vote": 0}},
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

        user = db.session.get(User, "alice")
        assert user.agent_state["rate_caps"] == {"post": 5, "vote": 0}

        resp = admin_client.get("/admin/api/users/alice")
        assert resp.status_code == 200
        assert resp.get_json()["rate_caps"] == {"post": 5, "vote": 0}

        # All-null rate_caps means "no override anywhere": the namespace is
        # removed so the executor falls back to the default caps.
        resp = admin_client.put(
            "/admin/api/users/alice",
            json={"rate_caps": {"post": None, "comment": None, "vote": None}},
        )
        assert resp.status_code == 200
        assert "rate_caps" not in (user.agent_state or {})
        assert admin_client.get("/admin/api/users/alice").get_json()["rate_caps"] == {}


def test_rate_caps_reject_invalid_values(admin_client, app, seeded_db):
    with app.app_context():
        for bad in (
            {"post": -1},
            {"post": "many"},
            {"post": True},
            {"spam": 3},
            "unlimited",
        ):
            resp = admin_client.put("/admin/api/users/alice", json={"rate_caps": bad})
            assert resp.status_code == 400, bad
            assert resp.get_json()["success"] is False

        user = db.session.get(User, "alice")
        assert "rate_caps" not in (user.agent_state or {})


def test_rate_caps_preserve_other_agent_state(admin_client, app, seeded_db):
    with app.app_context():
        user = db.session.get(User, "alice")
        user.agent_state = {"subscriptions": ["testsub"]}
        db.session.commit()

        resp = admin_client.put(
            "/admin/api/users/alice", json={"rate_caps": {"comment": 30}}
        )
        assert resp.status_code == 200

        user = db.session.get(User, "alice")
        assert user.agent_state == {
            "subscriptions": ["testsub"],
            "rate_caps": {"comment": 30},
        }
