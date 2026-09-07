"""Admin session hardening: token binding, socket gate, and cookie posture.

Covers the audit fixes: constant-time token comparison on /admin/login, token
rotation invalidating old sessions, the /admin websocket namespace rejecting
unauthenticated connects once API_TOKEN is set, and explicit SameSite=Lax
session-cookie posture.
"""

import pytest

from deaddit.extensions import socketio


def _register_admin_handlers():
    """Bind websocket.py admin handlers onto the CURRENT SocketIO instance.

    Same convention as test_ux6_live._register_live_handlers: flask_socketio
    mints a fresh bare Server per create_app(), so import-time decorators only
    bound to the first one.
    """
    from deaddit import websocket as ws_mod

    socketio.on("connect", namespace="/admin")(ws_mod.admin_connect)


# ---------------------------------------------------------------------------
# /admin/login token comparison
# ---------------------------------------------------------------------------


def test_login_rejects_wrong_token(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "right-token")
    resp = client.post("/admin/login", data={"api_token": "wrong"})
    assert resp.status_code == 200  # re-renders the login form
    with client.session_transaction() as sess:
        assert not sess.get("admin_token_fingerprint")


def test_login_accepts_correct_token(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "right-token")
    resp = client.post("/admin/login", data={"api_token": "right-token"})
    assert resp.status_code == 302
    assert "/admin/dashboard" in resp.headers["Location"]
    with client.session_transaction() as sess:
        assert isinstance(sess["admin_token_fingerprint"], str)
        assert "right-token" not in sess.values()
        assert "admin_authenticated" not in sess

def test_token_rotation_revokes_existing_session(app, client, monkeypatch):
    app.config["SECRET_KEY"] = "strong-test-secret"
    monkeypatch.setenv("API_TOKEN", "token-a")
    assert client.post("/admin/login", data={"api_token": "token-a"}).status_code == 302
    assert client.get("/admin/dashboard").status_code == 200

    monkeypatch.setenv("API_TOKEN", "token-b")
    assert client.get("/admin/dashboard").status_code == 302
    assert (
        client.post("/admin/login", data={"api_token": "token-b"}).status_code == 302
    )
    assert client.get("/admin/dashboard").status_code == 200

    with client.session_transaction() as sess:
        assert "token-a" not in sess.values()
        assert "token-b" not in sess.values()
        assert "admin_authenticated" not in sess


def test_logout_clears_token_binding(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "logout-token")
    assert client.post("/admin/login", data={"api_token": "logout-token"}).status_code == 302
    assert client.get("/admin/dashboard").status_code == 200

    assert client.get("/admin/logout").status_code == 302
    assert client.get("/admin/dashboard").status_code == 302
    with client.session_transaction() as sess:
        assert "admin_token_fingerprint" not in sess


def test_login_with_missing_form_field_rejected(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "right-token")
    resp = client.post("/admin/login", data={})
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        assert not sess.get("admin_token_fingerprint")


def test_legacy_boolean_session_is_denied(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "current-token")
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    assert client.get("/admin/dashboard").status_code == 302


# ---------------------------------------------------------------------------
# /admin websocket namespace gate
# ---------------------------------------------------------------------------


def test_admin_socket_rejects_anonymous_when_token_set(app, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "sekrit")
    _register_admin_handlers()
    ws_client = socketio.test_client(app, namespace="/admin")
    assert not ws_client.is_connected("/admin")


def test_admin_socket_accepts_authenticated_session_when_token_set(app, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "sekrit")
    _register_admin_handlers()
    http_client = app.test_client()
    response = http_client.post("/admin/login", data={"api_token": "sekrit"})
    assert response.status_code == 302
    ws_client = socketio.test_client(
        app, namespace="/admin", flask_test_client=http_client
    )
    assert ws_client.is_connected("/admin")


def test_admin_socket_open_when_no_token(app, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    _register_admin_handlers()
    ws_client = socketio.test_client(app, namespace="/admin")
    assert ws_client.is_connected("/admin")


# ---------------------------------------------------------------------------
# Cookie posture
# ---------------------------------------------------------------------------


def test_session_cookie_samesite_is_lax(app):
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
