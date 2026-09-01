"""Admin session hardening: login token compare, /admin socket gate, cookie posture.

Covers the audit fixes: constant-time token comparison on /admin/login, the
/admin websocket namespace rejecting unauthenticated connects once API_TOKEN
is set, and the explicit SameSite=Lax session-cookie posture.
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
        assert not sess.get("admin_authenticated")


def test_login_accepts_correct_token(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "right-token")
    resp = client.post("/admin/login", data={"api_token": "right-token"})
    assert resp.status_code == 302
    assert "/admin/dashboard" in resp.headers["Location"]
    with client.session_transaction() as sess:
        assert sess["admin_authenticated"] is True


def test_login_with_missing_form_field_rejected(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "right-token")
    resp = client.post("/admin/login", data={})
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        assert not sess.get("admin_authenticated")


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
    with http_client.session_transaction() as sess:
        sess["admin_authenticated"] = True
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
