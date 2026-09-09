"""Admin session hardening: token binding, socket gate, and cookie posture.

Covers the audit fixes: constant-time token comparison on /admin/login, token
rotation invalidating old sessions, the /admin websocket namespace rejecting
unauthenticated connects once API_TOKEN is set, and explicit SameSite=Lax
session-cookie posture.
"""
import hashlib
import hmac

import pytest
from flask.sessions import SecureCookieSessionInterface

from deaddit import create_app, db
from deaddit.config import Config
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



def _session_app(tmp_path, monkeypatch, *, secret=None):
    monkeypatch.setenv("API_TOKEN", "api-token-for-session-tests")
    if secret is None:
        monkeypatch.delenv("SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("SECRET_KEY", secret)
    app = create_app(
        {
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'session.db'}",
            "TESTING": True,
        }
    )
    with app.app_context():
        db.create_all()
    return app


def test_api_token_derives_session_secret_and_rejects_legacy_cookie(
    tmp_path, monkeypatch
):
    app = _session_app(tmp_path, monkeypatch)
    assert app.config["SECRET_KEY"] != Config.DEFAULTS["SECRET_KEY"]

    with app.app_context():
        app.config["SECRET_KEY"] = Config.DEFAULTS["SECRET_KEY"]
        legacy_cookie = SecureCookieSessionInterface().get_signing_serializer(
            app
        ).dumps({"admin_authenticated": True})
        app.config["SECRET_KEY"] = hmac.new(
            b"api-token-for-session-tests",
            b"deaddit:flask-session-signing:v1",
            hashlib.sha256,
        ).hexdigest()

    response = app.test_client().get(
        "/admin/api/setup/status",
        headers={"Cookie": f"session={legacy_cookie}"},
    )
    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"]


def test_api_token_session_works_across_app_instances_without_leaking_token(
    tmp_path, monkeypatch
):
    app_one = _session_app(tmp_path, monkeypatch)
    login_client = app_one.test_client()
    token = "api-token-for-session-tests"
    response = login_client.post("/admin/login", data={"api_token": token})
    assert response.status_code == 302
    assert token not in response.get_data(as_text=True)
    assert token not in response.headers["Set-Cookie"]

    cookie = login_client.get_cookie("session")
    assert cookie is not None
    assert token not in cookie.value
    with login_client.session_transaction() as session:
        assert isinstance(session["admin_token_fingerprint"], str)
        assert token not in repr(dict(session))
        assert "api_token" not in session

    app_two = _session_app(tmp_path, monkeypatch)
    second_client = app_two.test_client()
    second_client.set_cookie("session", cookie.value)
    response = second_client.get("/admin/api/setup/status")
    assert response.status_code == 200


def test_explicit_session_secret_is_honored(tmp_path, monkeypatch):
    explicit_secret = "explicit-session-secret-for-tests"
    app = _session_app(tmp_path, monkeypatch, secret=explicit_secret)
    assert app.config["SECRET_KEY"] == explicit_secret

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
