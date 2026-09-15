"""Tests for Phase 1 Human Accounts: schema, session auth, feature flag, and routes."""

from __future__ import annotations

from datetime import datetime, timedelta
import sqlite3
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

import deaddit
from deaddit.config import Config
from deaddit.dynamics.inbox import get_inbox, mark_inbox_read
from deaddit.extensions import db
from deaddit.human_auth import (
    _DUMMY_PASSWORD_HASH,
    authenticate_human,
    current_human,
    human_accounts_enabled,
    register_human,
)
from deaddit.models import Comment, Notification, Post, Setting, Subdeaddit, User
from deaddit.services.content import (
    ContentValidationError,
    create_comment,
    create_post,
)
from deaddit.utils import safe_local_next

PREVIOUS_HEAD = "a6b8c0d2e4f6"
FEATURE_HEAD = "b1e3a5c7d9f2"


# ============================================================================
# 1. User Model Tests
# ============================================================================


def test_user_model_password_hash_and_is_human(app, db_session):
    """Verify password_hash column and is_human property on User."""
    with app.app_context():
        # Synthetic user (no password hash)
        ai_user = User(username="ai_bot", model="agent:test")
        db_session.add(ai_user)
        db_session.commit()

        assert ai_user.password_hash is None
        assert ai_user.is_human is False

        # Human user (has password hash)
        human_user = User(
            username="human_tester",
            password_hash="pbkdf2:sha256:dummyhash",
            model="human",
        )
        db_session.add(human_user)
        db_session.commit()

        assert human_user.password_hash == "pbkdf2:sha256:dummyhash"
        assert human_user.is_human is True


def test_case_insensitive_unique_username_constraint(app, db_session):
    """Verify lower(username) unique index prevents case-folded collisions."""
    with app.app_context():
        u1 = User(username="Alice")
        db_session.add(u1)
        db_session.commit()

        u2 = User(username="alice")
        db_session.add(u2)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

        u3 = User(username="ALICE")
        db_session.add(u3)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


# ============================================================================
# 2. Registration Validation & Handling
# ============================================================================


def test_register_human_validates_input(app, db_session):
    """Test registration input validation rules."""
    with app.test_request_context():
        # Short username
        user, err = register_human("ab", "password123", "password123")
        assert user is None
        assert "between 3 and 30 characters" in err

        # Long username
        user, err = register_human("a" * 31, "password123", "password123")
        assert user is None
        assert "between 3 and 30 characters" in err

        # Invalid characters
        user, err = register_human("user@name", "password123", "password123")
        assert user is None
        assert "between 3 and 30 characters" in err

        user, err = register_human("user name", "password123", "password123")
        assert user is None
        assert "between 3 and 30 characters" in err

        # Short password (< 8)
        user, err = register_human("validuser", "short", "short")
        assert user is None
        assert "between 8 and 256 characters" in err

        # Long password (> 256)
        user, err = register_human("validuser", "p" * 257, "p" * 257)
        assert user is None
        assert "between 8 and 256 characters" in err

        # Mismatched confirmation
        user, err = register_human("validuser", "password123", "password456")
        assert user is None
        assert "do not match" in err


def test_register_human_successful(app, db_session):
    """Test successful human registration sets lowercase username, hash, and session."""
    with app.test_request_context() as ctx:
        user, err = register_human("  NewUser_99  ", "securepassword123", "securepassword123")
        assert err is None
        assert user is not None
        assert user.username == "newuser_99"
        assert user.is_human is True
        assert user.password_hash != "securepassword123"
        assert user.password_hash.startswith("scrypt:") or user.password_hash.startswith("pbkdf2:")
        assert ctx.session.get("human_username") == "newuser_99"


def test_register_human_rejects_collisions(app, db_session):
    """Test registration rejects collisions with existing AI or human accounts."""
    with app.app_context():
        # Pre-create AI user
        ai_user = User(username="synthetic_bot")
        db_session.add(ai_user)
        db_session.commit()

    with app.test_request_context():
        # Collides with AI user (case-folded)
        user, err = register_human("SYNTHETIC_BOT", "password123", "password123")
        assert user is None
        assert "already taken" in err

        # Successful first human registration
        user, err = register_human("human_one", "password123", "password123")
        assert err is None
        assert user is not None

        # Collides with human user (case-folded)
        user2, err2 = register_human("Human_One", "password123", "password123")
        assert user2 is None
        assert "already taken" in err2


def test_register_human_handles_integrity_error(app, monkeypatch):
    """Simulate race condition during commit in register_human."""
    with app.test_request_context():
        def fake_commit():
            raise IntegrityError("mock collision", params=None, orig=Exception())

        monkeypatch.setattr(db.session, "commit", fake_commit)
        user, err = register_human("racing_user", "password123", "password123")
        assert user is None
        assert "already taken" in err


# ============================================================================
# 3. Authentication & Dummy Hash Timing Defense
# ============================================================================


def test_authenticate_human_dummy_hash_defense(app, db_session):
    """Test that missing user and synthetic user invoke dummy hash check to prevent timing leaks."""
    with app.app_context():
        ai_user = User(username="ai_persona", model="agent:ai")
        db_session.add(ai_user)
        db_session.commit()

    with app.test_request_context():
        # Non-existent user
        with patch("deaddit.human_auth.check_password_hash", wraps=deaddit.human_auth.check_password_hash) as mock_check:
            user, err = authenticate_human("nobody", "password123")
            assert user is None
            assert err == "Invalid username or password"
            mock_check.assert_called_once_with(_DUMMY_PASSWORD_HASH, "password123")

        # Synthetic user (no password_hash)
        with patch("deaddit.human_auth.check_password_hash", wraps=deaddit.human_auth.check_password_hash) as mock_check:
            user, err = authenticate_human("ai_persona", "password123")
            assert user is None
            assert err == "Invalid username or password"
            mock_check.assert_called_once_with(_DUMMY_PASSWORD_HASH, "password123")


def test_authenticate_human_success_and_wrong_password(app, db_session):
    """Test authenticate_human for wrong password and correct password."""
    with app.test_request_context():
        reg_user, _ = register_human("auth_test", "correct_password", "correct_password")
        stored_hash = reg_user.password_hash

        # Wrong password
        with patch("deaddit.human_auth.check_password_hash", wraps=deaddit.human_auth.check_password_hash) as mock_check:
            user, err = authenticate_human("AUTH_TEST", "wrong_password")
            assert user is None
            assert err == "Invalid username or password"
            mock_check.assert_called_once_with(stored_hash, "wrong_password")

        # Correct password
        user, err = authenticate_human("auth_test", "correct_password")
        assert err is None
        assert user is not None
        assert user.username == "auth_test"


# ============================================================================
# 4. Session Separation & Current User
# ============================================================================


def test_session_separation(app, client, monkeypatch):
    """Verify human login/logout does not interfere with admin session and vice versa."""
    monkeypatch.setenv("API_TOKEN", "test-admin-token")

    # 1. Human registers and signs in via HTTP
    reg_resp = client.post(
        "/register",
        data={
            "username": "tester",
            "password": "password123",
            "password_confirm": "password123",
        },
        follow_redirects=True,
    )
    assert reg_resp.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get("human_username") == "tester"
        assert "admin_token_fingerprint" not in sess

    # 2. Admin signs in
    admin_resp = client.post(
        "/admin/login",
        data={"api_token": "test-admin-token"},
        follow_redirects=True,
    )
    assert admin_resp.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get("human_username") == "tester"
        assert sess.get("admin_token_fingerprint") is not None

    # 3. Human logs out
    logout_resp = client.post("/logout", follow_redirects=True)
    assert logout_resp.status_code == 200
    with client.session_transaction() as sess:
        assert "human_username" not in sess
        # Admin session remains intact
        assert sess.get("admin_token_fingerprint") is not None

    # 4. Admin logs out
    admin_logout_resp = client.get("/admin/logout", follow_redirects=True)
    assert admin_logout_resp.status_code == 200
    with client.session_transaction() as sess:
        assert "admin_token_fingerprint" not in sess


def test_current_human_stale_identity_cleared(app, db_session):
    """If session holds a username that no longer exists or is synthetic, session is cleared."""
    with app.test_request_context() as ctx:
        ctx.session["human_username"] = "ghost_user"
        assert current_human() is None
        assert "human_username" not in ctx.session

    with app.app_context():
        ai_user = User(username="synthetic_only")
        db_session.add(ai_user)
        db_session.commit()

    with app.test_request_context() as ctx:
        ctx.session["human_username"] = "synthetic_only"
        assert current_human() is None
        assert "human_username" not in ctx.session


# ============================================================================
# 5. safe_local_next Utility
# ============================================================================


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/d/general", "/d/general"),
        ("/submit", "/submit"),
        ("/u/alice?tab=comments", "/u/alice?tab=comments"),
        ("/login?next=/submit", "/login?next=/submit"),
        ("/", "/"),
        # Invalid / Malicious targets
        (None, None),
        ("", None),
        ("https://evil.com", None),
        ("http://evil.com/path", None),
        ("//evil.com", None),
        ("//evil.com/sub", None),
        ("/\\evil.com", None),
        ("/path\\with\\backslash", None),
        ("/path://evil.com", None),
        ("javascript:alert(1)", None),
        ("/path\r\ninjected", None),
        (12345, None),
    ],
)
def test_safe_local_next(path, expected):
    assert safe_local_next(path) == expected


# ============================================================================
# 6. Feature Flag Precedence & Disabling
# ============================================================================


def test_human_accounts_enabled_precedence(app, monkeypatch):
    """Test environment override precedence over database setting."""
    with app.app_context():
        # Default with no env and no DB
        monkeypatch.delenv("HUMAN_ACCOUNTS_ENABLED", raising=False)
        Config.set("HUMAN_ACCOUNTS_ENABLED", "true")
        assert human_accounts_enabled() is True

        # DB is false, but env is true -> True
        Config.set("HUMAN_ACCOUNTS_ENABLED", "false")
        monkeypatch.setenv("HUMAN_ACCOUNTS_ENABLED", "true")
        assert human_accounts_enabled() is True

        # DB is true, but env is false -> False
        Config.set("HUMAN_ACCOUNTS_ENABLED", "true")
        monkeypatch.setenv("HUMAN_ACCOUNTS_ENABLED", "false")
        assert human_accounts_enabled() is False

        # Malformed env var logs warning and fails closed (False)
        monkeypatch.setenv("HUMAN_ACCOUNTS_ENABLED", "not-a-boolean")
        assert human_accounts_enabled() is False

        # When env is absent, DB setting controls
        monkeypatch.delenv("HUMAN_ACCOUNTS_ENABLED", raising=False)
        Config.set("HUMAN_ACCOUNTS_ENABLED", "false")
        assert human_accounts_enabled() is False
        Config.set("HUMAN_ACCOUNTS_ENABLED", "true")
        assert human_accounts_enabled() is True


def test_gated_routes_return_404_when_disabled(app, client, monkeypatch):
    """When human accounts are disabled, /register and /login return 404; /logout works."""
    monkeypatch.setenv("HUMAN_ACCOUNTS_ENABLED", "false")

    # Registration routes 404
    assert client.get("/register").status_code == 404
    assert client.post("/register", data={"username": "test"}).status_code == 404

    # Login routes 404
    assert client.get("/login").status_code == 404
    assert client.post("/login", data={"username": "test"}).status_code == 404

    # Logout route is always available
    logout_resp = client.post("/logout")
    assert logout_resp.status_code == 302


def test_already_signed_in_redirects_home(app, client):
    """Signed-in human visiting /login or /register redirects to index."""
    # Register and sign in
    client.post(
        "/register",
        data={"username": "logged_in", "password": "password123", "password_confirm": "password123"},
        follow_redirects=True,
    )
    # Visiting /login redirects to home
    resp = client.get("/login")
    assert resp.status_code == 302
    assert resp.headers["Location"] in ("/", "http://localhost/")

    # Visiting /register redirects to home
    resp = client.get("/register")
    assert resp.status_code == 302
    assert resp.headers["Location"] in ("/", "http://localhost/")


# ============================================================================
# 7. Context Processor & Template Elements
# ============================================================================


def test_context_processor_and_nav_bar(app, client, db_session):
    """Verify template context contains human identity, unread count, and nav renders correctly."""
    # Anonymous visitor
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Sign In" in resp.text
    assert "Create Account" in resp.text
    assert "Log Out" not in resp.text

    # Register human
    client.post(
        "/register",
        data={"username": "nav_user", "password": "password123", "password_confirm": "password123"},
        follow_redirects=True,
    )

    # Signed in visitor
    resp = client.get("/")
    assert resp.status_code == 200
    assert "u/nav_user" in resp.text
    assert "Log Out" in resp.text
    assert "Sign In" not in resp.text

    # Notifications unread count
    with app.app_context():
        notif = Notification(recipient="nav_user", actor=None, kind="reply", is_read=False)
        db_session.add(notif)
        db_session.commit()

    with client.session_transaction() as sess:
        sess["human_username"] = "nav_user"

    resp = client.get("/")
    assert resp.status_code == 200


def test_admin_settings_human_accounts_save(app, client, admin_login):
    """Verify admin settings save handling for HUMAN_ACCOUNTS_ENABLED."""
    admin_login(client)

    # Valid true
    resp = client.post("/admin/api/save-deaddit-config", json={"human_accounts_enabled": "true"})
    assert resp.status_code == 200
    assert Config.get("HUMAN_ACCOUNTS_ENABLED") == "true"

    # Valid false
    resp = client.post("/admin/api/save-deaddit-config", json={"human_accounts_enabled": "false"})
    assert resp.status_code == 200
    assert Config.get("HUMAN_ACCOUNTS_ENABLED") == "false"

    # Invalid value -> 400
    resp = client.post("/admin/api/save-deaddit-config", json={"human_accounts_enabled": "maybe"})
    assert resp.status_code == 400


# ============================================================================
# 8. Password Hash Exposure Defense
# ============================================================================


def test_password_hash_never_exposed(app, client, db_session, admin_login):
    """Verify password_hash is never exposed in public API, admin API, or profiles."""
    with app.app_context():
        user = User(
            username="secret_user",
            password_hash="pbkdf2:sha256:verysecretvalue123",
            model="human",
            bio="My public bio",
            interests="[]",
            personality_traits="[]",
        )
        db_session.add(user)
        db_session.commit()

    # 1. Public /api/users
    resp = client.get("/api/users")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "verysecretvalue123" not in resp.text
    found = next((u for u in data["users"] if u["username"] == "secret_user"), None)
    assert found is not None
    assert "password_hash" not in found

    # 2. Admin /admin/api/users
    admin_login(client)
    resp = client.get("/admin/api/users")
    assert resp.status_code == 200
    assert "verysecretvalue123" not in resp.text
    data = resp.get_json()
    found = next((u for u in data["users"] if u["username"] == "secret_user"), None)
    assert found is not None
    assert "password_hash" not in found

    # 3. Public profile
    resp = client.get("/u/secret_user")
    assert resp.status_code == 200
    assert "verysecretvalue123" not in resp.text
    assert "password_hash" not in resp.text


# ============================================================================
# 9. Alembic Migration Upgrade and Downgrade
# ============================================================================


def test_migration_upgrade_and_downgrade(tmp_path):
    """Test Alembic upgrade to b1e3a5c7d9f2 and downgrade back to a6b8c0d2e4f6."""
    from tests.test_random_persona_migration import (
        _columns,
        _downgrade,
        _index_names,
        _runner,
        _upgrade,
    )

    db_path, app, runner = _runner(tmp_path)

    # 1. Upgrade to PREVIOUS_HEAD
    _upgrade(runner, PREVIOUS_HEAD)
    cols = _columns(db_path, "user")
    indexes = _index_names(db_path, "user")
    assert "password_hash" not in cols
    assert "uq_user_username_lower" not in indexes

    # 2. Upgrade to FEATURE_HEAD
    _upgrade(runner, FEATURE_HEAD)
    cols = _columns(db_path, "user")
    indexes = _index_names(db_path, "user")
    assert "password_hash" in cols
    assert "uq_user_username_lower" in indexes

    # 3. Test duplicate check: seed duplicate case-folded usernames and verify upgrade fails
    _downgrade(runner, PREVIOUS_HEAD)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO user (username) VALUES ('dup_user')")
    conn.execute("INSERT INTO user (username) VALUES ('DUP_USER')")
    conn.commit()
    conn.close()

    result = runner.invoke(args=["db", "upgrade", FEATURE_HEAD])
    assert result.exit_code != 0
    assert "Case-folded duplicate usernames found" in result.output

    # Clean up duplicate
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM user WHERE username = 'DUP_USER'")
    conn.commit()
    conn.close()

    # Now upgrade succeeds
    _upgrade(runner, FEATURE_HEAD)
    assert "password_hash" in _columns(db_path, "user")

    # 4. Downgrade back to PREVIOUS_HEAD
    _downgrade(runner, PREVIOUS_HEAD)
    cols = _columns(db_path, "user")
    indexes = _index_names(db_path, "user")
    assert "password_hash" not in cols
    assert "uq_user_username_lower" not in indexes


# ============================================================================
# 10. Phase 2: Post Submission, Commenting, and Replies
# ============================================================================


def _register_and_login(client, username="alice", password="password123"):
    """Helper to register and sign in a human user."""
    client.post(
        "/register",
        data={
            "username": username,
            "password": password,
            "password_confirm": password,
        },
        follow_redirects=True,
    )


def test_submit_get_returns_form_for_logged_in_user(app, client, db_session):
    """Test /submit GET returns form for logged in user and preselects community."""
    _register_and_login(client, "author_user")

    with app.app_context():
        s1 = Subdeaddit(name="general", description="General community")
        s2 = Subdeaddit(name="science", description="Science community")
        db_session.add_all([s1, s2])
        db_session.commit()

    # 1. Plain GET
    resp = client.get("/submit")
    assert resp.status_code == 200
    assert "Create a Post" in resp.text
    assert 'name="community"' in resp.text
    assert 'name="title"' in resp.text
    assert 'name="body"' in resp.text
    assert "d/general" in resp.text
    assert "d/science" in resp.text

    # 2. Preselected community via ?community=science
    resp_pre = client.get("/submit?community=science")
    assert resp_pre.status_code == 200
    assert 'value="science"' in resp_pre.text
    assert 'selected' in resp_pre.text

    # 3. Preselected community via ?subdeaddit=general
    resp_sub = client.get("/submit?subdeaddit=general")
    assert resp_sub.status_code == 200
    assert 'value="general"' in resp_sub.text


def test_submit_redirects_anonymous_user_to_login(client):
    """Test /submit redirects anonymous user to /login?next=/submit."""
    resp = client.get("/submit")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login?next=/submit")

    # With query parameter
    resp_query = client.get("/submit?community=general")
    assert resp_query.status_code == 302
    assert "/login?next=" in resp_query.headers["Location"]
    assert "community%3Dgeneral" in resp_query.headers["Location"] or "community=general" in resp_query.headers["Location"]


def test_submit_creates_post_success(app, client, db_session):
    """Test /submit creates post with model='human', llm_model=None, text post type."""
    _register_and_login(client, "writer_human")

    with app.app_context():
        sub = Subdeaddit(name="technology", description="Technology")
        db_session.add(sub)
        db_session.commit()

    resp = client.post(
        "/submit",
        data={
            "community": "technology",
            "title": "A Great Discovery",
            "body": "Detailed findings on artificial intelligence and human collaboration.",
        },
    )
    assert resp.status_code == 302

    with app.app_context():
        post = Post.query.filter_by(title="A Great Discovery").first()
        assert post is not None
        assert post.content == "Detailed findings on artificial intelligence and human collaboration."
        assert post.user == "writer_human"
        assert post.subdeaddit_name == "technology"
        assert post.model == "human"
        assert post.llm_model is None
        assert post.post_type == "text"

        assert resp.headers["Location"].endswith(f"/d/technology/{post.id}")


def test_submit_validation(app, client, db_session):
    """Test /submit validation: title length, body length, invalid subdeaddit."""
    _register_and_login(client, "validator_user")

    with app.app_context():
        sub = Subdeaddit(name="general", description="General")
        db_session.add(sub)
        db_session.commit()

    # 1. Missing / whitespace-only title
    resp = client.post(
        "/submit",
        data={"community": "general", "title": "   ", "body": "Some body content"},
    )
    assert resp.status_code == 200
    assert "Title must be between 1 and 100 characters." in resp.text
    assert "Some body content" in resp.text

    # 2. Title too long (>100 chars)
    long_title = "T" * 101
    resp = client.post(
        "/submit",
        data={"community": "general", "title": long_title, "body": "Some body content"},
    )
    assert resp.status_code == 200
    assert "Title must be between 1 and 100 characters." in resp.text
    assert long_title in resp.text

    # 3. Missing / whitespace-only body
    resp = client.post(
        "/submit",
        data={"community": "general", "title": "Valid Title", "body": "   "},
    )
    assert resp.status_code == 200
    assert "Body must be between 1 and 10,000 characters." in resp.text
    assert "Valid Title" in resp.text

    # 4. Body too long (>10,000 chars)
    long_body = "B" * 10001
    resp = client.post(
        "/submit",
        data={"community": "general", "title": "Valid Title", "body": long_body},
    )
    assert resp.status_code == 200
    assert "Body must be between 1 and 10,000 characters." in resp.text

    # 5. Non-existent subdeaddit
    resp = client.post(
        "/submit",
        data={"community": "nonexistent_sub", "title": "Valid Title", "body": "Valid body"},
    )
    assert resp.status_code == 200
    assert "does not exist" in resp.text
    assert "Valid Title" in resp.text
    assert "Valid body" in resp.text

    # 6. Missing community
    resp = client.post(
        "/submit",
        data={"community": "", "title": "Valid Title", "body": "Valid body"},
    )
    assert resp.status_code == 200
    assert "Please select a community." in resp.text

    with app.app_context():
        assert Post.query.count() == 0


def test_submit_rate_limit_error_handling(app, client, db_session):
    """Test rate limit error handling on /submit catches ContentValidationError and preserves inputs."""
    _register_and_login(client, "rate_limited_human")

    with app.app_context():
        sub = Subdeaddit(name="general", description="General")
        db_session.add(sub)
        db_session.commit()

    with patch("deaddit.services.content.create_post", side_effect=ContentValidationError("rate_limited")):
        resp = client.post(
            "/submit",
            data={"community": "general", "title": "Preserved Title", "body": "Preserved Body Text"},
        )
        assert resp.status_code == 200
        assert "rate limit" in resp.text.lower()
        assert "Preserved Title" in resp.text
        assert "Preserved Body Text" in resp.text


def _create_synthetic_user(db_session, username="bot"):
    """Helper to ensure a synthetic user exists for foreign key references."""
    user = User.query.filter_by(username=username).first()
    if not user:
        user = User(username=username, model=f"agent:{username}")
        db_session.add(user)
        db_session.commit()
    return user


def test_comment_creation_top_level(app, client, db_session):
    """Test POST /d/<subdeaddit>/<id>/comments creates top-level comment with author from session."""
    _register_and_login(client, "commenter_human")

    with app.app_context():
        _create_synthetic_user(db_session, "author_bot")
        sub = Subdeaddit(name="general", description="General")
        db_session.add(sub)
        post = Post(
            title="Post to comment on",
            content="Hello everyone",
            user="author_bot",
            subdeaddit_name="general",
            model="agent:test",
        )
        db_session.add(post)
        db_session.commit()
        post_id = post.id

    # Attempt to spoof author, model, score
    resp = client.post(
        f"/d/general/{post_id}/comments",
        data={
            "content": "A thoughtful human response",
            "author": "spoofed_author",
            "model": "gpt-4-super",
            "score": 9999,
        },
    )
    assert resp.status_code == 302

    with app.app_context():
        comment = Comment.query.filter_by(post_id=post_id).first()
        assert comment is not None
        assert comment.content == "A thoughtful human response"
        assert comment.user == "commenter_human"
        assert comment.model == "human"
        assert comment.score == 0
        assert comment.llm_model is None
        assert comment.parent_id is None

        assert resp.headers["Location"].endswith(f"/d/general/{post_id}#comment-{comment.id}")


def test_comment_creation_nested_reply(app, client, db_session):
    """Test nested reply creation with valid parent_id."""
    _register_and_login(client, "replier_human")

    with app.app_context():
        _create_synthetic_user(db_session, "author_bot")
        _create_synthetic_user(db_session, "first_commenter")
        sub = Subdeaddit(name="general", description="General")
        db_session.add(sub)
        post = Post(
            title="Post with existing comment",
            content="Thread content",
            user="author_bot",
            subdeaddit_name="general",
            model="agent:test",
        )
        db_session.add(post)
        db_session.flush()

        parent = Comment(
            post_id=post.id,
            user="first_commenter",
            content="Initial thought",
            model="agent:test",
        )
        db_session.add(parent)
        db_session.commit()
        post_id = post.id
        parent_id = parent.id

    resp = client.post(
        f"/d/general/{post_id}/comments",
        data={
            "content": "A reply to the initial thought",
            "parent_id": str(parent_id),
        },
    )
    assert resp.status_code == 302

    with app.app_context():
        reply = Comment.query.filter_by(parent_id=parent_id).first()
        assert reply is not None
        assert reply.content == "A reply to the initial thought"
        assert reply.user == "replier_human"
        assert reply.post_id == post_id
        assert reply.parent_id == parent_id
        assert reply.model == "human"

        assert resp.headers["Location"].endswith(f"/d/general/{post_id}#comment-{reply.id}")


def test_comment_cross_post_parent_rejected(app, client, db_session):
    """Test cross-post parent_id is rejected without comment persistence."""
    _register_and_login(client, "cross_tester")

    with app.app_context():
        _create_synthetic_user(db_session, "bot1")
        _create_synthetic_user(db_session, "bot2")
        sub = Subdeaddit(name="general", description="General")
        db_session.add(sub)
        post1 = Post(
            title="Post 1",
            content="Content 1",
            user="bot1",
            subdeaddit_name="general",
            model="agent:test",
        )
        post2 = Post(
            title="Post 2",
            content="Content 2",
            user="bot2",
            subdeaddit_name="general",
            model="agent:test",
        )
        db_session.add_all([post1, post2])
        db_session.flush()

        comment_on_post1 = Comment(
            post_id=post1.id,
            user="bot1",
            content="Comment on post 1",
            model="agent:test",
        )
        db_session.add(comment_on_post1)
        db_session.commit()
        post2_id = post2.id
        comment1_id = comment_on_post1.id

    resp = client.post(
        f"/d/general/{post2_id}/comments",
        data={
            "content": "Malicious reply attempting cross-post parent",
            "parent_id": str(comment1_id),
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "does not belong to this post" in resp.text

    with app.app_context():
        assert Comment.query.filter_by(post_id=post2_id).count() == 0


def test_comment_validation(app, client, db_session):
    """Test comment validation: length 1-5000 chars and empty body."""
    _register_and_login(client, "comment_validator")

    with app.app_context():
        _create_synthetic_user(db_session, "bot")
        sub = Subdeaddit(name="general", description="General")
        db_session.add(sub)
        post = Post(
            title="Post for comment validation",
            content="Content",
            user="bot",
            subdeaddit_name="general",
            model="agent:test",
        )
        db_session.add(post)
        db_session.commit()
        post_id = post.id

    # 1. Empty body
    resp = client.post(
        f"/d/general/{post_id}/comments",
        data={"content": "   "},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "Comment must be between 1 and 5,000 characters." in resp.text

    # 2. Body too long (>5,000 chars)
    resp = client.post(
        f"/d/general/{post_id}/comments",
        data={"content": "C" * 5001},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "Comment must be between 1 and 5,000 characters." in resp.text

    # 3. Invalid non-numeric parent_id
    resp = client.post(
        f"/d/general/{post_id}/comments",
        data={"content": "Valid comment", "parent_id": "not-an-int"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "Invalid parent comment." in resp.text

    with app.app_context():
        assert Comment.query.count() == 0


def test_anonymous_comment_post_redirects_to_login(app, client, db_session):
    """Test anonymous comment POST redirects to login."""
    with app.app_context():
        _create_synthetic_user(db_session, "bot")
        sub = Subdeaddit(name="general", description="General")
        db_session.add(sub)
        post = Post(
            title="Public Post",
            content="Content",
            user="bot",
            subdeaddit_name="general",
            model="agent:test",
        )
        db_session.add(post)
        db_session.commit()
        post_id = post.id

    resp = client.post(
        f"/d/general/{post_id}/comments",
        data={"content": "Anonymous comment attempt"},
    )
    assert resp.status_code == 302
    assert "/login?next=" in resp.headers["Location"]
    assert f"/d/general/{post_id}" in resp.headers["Location"]

    with app.app_context():
        assert Comment.query.count() == 0


def test_disabled_feature_returns_404_phase2(app, client, db_session, monkeypatch):
    """Test disabled feature returns 404 on /submit and comment POST, and hides composer."""
    _register_and_login(client, "gated_human")

    with app.app_context():
        _create_synthetic_user(db_session, "bot")
        sub = Subdeaddit(name="general", description="General")
        db_session.add(sub)
        post = Post(
            title="Existing Post",
            content="Content",
            user="bot",
            subdeaddit_name="general",
            model="agent:test",
        )
        db_session.add(post)
        db_session.commit()
        post_id = post.id

    monkeypatch.setenv("HUMAN_ACCOUNTS_ENABLED", "false")

    # /submit GET and POST 404
    assert client.get("/submit").status_code == 404
    assert client.post("/submit", data={"community": "general", "title": "Test", "body": "Test"}).status_code == 404

    # Comment POST 404
    assert client.post(f"/d/general/{post_id}/comments", data={"content": "Test comment"}).status_code == 404

    # Thread view hides comment composer and reply links
    resp = client.get(f"/d/general/{post_id}")
    assert resp.status_code == 200
    assert "comment-composer" not in resp.text
    assert "comment-reply-link" not in resp.text


def test_post_get_reply_to_context(app, client, db_session):
    """Test GET /d/<subdeaddit>/<id>?reply_to=<comment_id> sets reply target and renders reply banner."""
    _register_and_login(client, "reply_context_tester")

    with app.app_context():
        _create_synthetic_user(db_session, "bot")
        _create_synthetic_user(db_session, "original_replier")
        sub = Subdeaddit(name="general", description="General")
        db_session.add(sub)
        post = Post(
            title="Post with target comment",
            content="Content",
            user="bot",
            subdeaddit_name="general",
            model="agent:test",
        )
        db_session.add(post)
        db_session.flush()

        target_comment = Comment(
            post_id=post.id,
            user="original_replier",
            content="Target comment text",
            model="agent:test",
        )
        db_session.add(target_comment)
        db_session.commit()
        post_id = post.id
        comment_id = target_comment.id

    # Valid reply_to
    resp = client.get(f"/d/general/{post_id}?reply_to={comment_id}")
    assert resp.status_code == 200
    assert "Replying to" in resp.text
    assert "u/original_replier" in resp.text
    assert f'name="parent_id" value="{comment_id}"' in resp.text
    assert "Cancel" in resp.text

    # Invalid reply_to (non-existent id)
    resp_invalid = client.get(f"/d/general/{post_id}?reply_to=999999")
    assert resp_invalid.status_code == 200
    assert "Replying to" not in resp_invalid.text


def test_nav_create_post_link(app, client, monkeypatch):
    """Test 'Create Post' nav link visibility depending on auth state and feature flag."""
    # 1. Anonymous visitor -> no "Create Post"
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Create Post" not in resp.text

    # 2. Signed-in human -> has "Create Post"
    _register_and_login(client, "nav_poster")
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Create Post" in resp.text
    assert 'href="/submit"' in resp.text

    # 3. Disabled feature flag -> no "Create Post" even when session exists
    monkeypatch.setenv("HUMAN_ACCOUNTS_ENABLED", "false")
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Create Post" not in resp.text


# ============================================================================
# 8. Phase 3: Human Inbox, Navigation, and Dynamics
# ============================================================================


def test_inbox_get_returns_items_newest_first(app, client, db_session):
    """Verify GET /inbox returns user's notifications in newest-first order with canonical links."""
    _register_and_login(client, "inbox_tester")

    with app.app_context():
        _create_synthetic_user(db_session, "bot_author")
        sub = Subdeaddit(name="general", description="General")
        db_session.add(sub)
        post = Post(
            title="Notification Target Post",
            content="Content",
            user="inbox_tester",
            subdeaddit_name="general",
            model="human",
        )
        db_session.add(post)
        db_session.commit()
        post_id = post.id

        c1 = Comment(
            post_id=post_id,
            user="bot_author",
            content="First reply snippet",
            model="agent:test",
        )
        c2 = Comment(
            post_id=post_id,
            user="bot_author",
            content="Second mention snippet",
            model="agent:test",
        )
        c3 = Comment(
            post_id=post_id,
            user="bot_author",
            content="Third reply snippet (newest)",
            model="agent:test",
        )
        db_session.add_all([c1, c2, c3])
        db_session.flush()

        now = datetime.utcnow()
        n1 = Notification(
            recipient="inbox_tester",
            kind="reply",
            actor="bot_author",
            post_id=post_id,
            comment_id=c1.id,
            snippet="First reply snippet",
            created_at=now - timedelta(minutes=30),
        )
        n2 = Notification(
            recipient="inbox_tester",
            kind="mention",
            actor="bot_author",
            post_id=post_id,
            comment_id=c2.id,
            snippet="Second mention snippet",
            created_at=now - timedelta(minutes=15),
        )
        n3 = Notification(
            recipient="inbox_tester",
            kind="reply",
            actor="bot_author",
            post_id=post_id,
            comment_id=c3.id,
            snippet="Third reply snippet (newest)",
            created_at=now,
        )
        db_session.add_all([n1, n2, n3])
        db_session.commit()
        c3_id = c3.id

    resp = client.get("/inbox")
    assert resp.status_code == 200
    assert "Inbox" in resp.text
    # Check newest-first order in rendered HTML
    pos_newest = resp.text.find("Third reply snippet (newest)")
    pos_middle = resp.text.find("Second mention snippet")
    pos_oldest = resp.text.find("First reply snippet")
    assert pos_newest != -1 and pos_middle != -1 and pos_oldest != -1
    assert pos_newest < pos_middle < pos_oldest

    # Check badges and canonical links
    assert "inbox-badge--reply" in resp.text
    assert "inbox-badge--mention" in resp.text
    assert f"/d/general/{post_id}#comment-{c3_id}" in resp.text
    assert "u/bot_author" in resp.text


def test_inbox_keyset_pagination_and_invalid_cursor(app, client, db_session):
    """Test keyset pagination over 25 items and 400 response on malformed cursor."""
    import re

    _register_and_login(client, "page_user")

    with app.app_context():
        _create_synthetic_user(db_session, "pager_bot")
        sub = Subdeaddit(name="general", description="General")
        db_session.add(sub)
        post = Post(
            title="Pagination Post",
            content="Content",
            user="page_user",
            subdeaddit_name="general",
            model="human",
        )
        db_session.add(post)
        db_session.commit()
        post_id = post.id

        now = datetime.utcnow()
        # Insert 30 notifications: limit is 25, so page 1 has 25 and next_cursor
        for i in range(30):
            db_session.add(
                Notification(
                    recipient="page_user",
                    kind="reply",
                    actor="pager_bot",
                    post_id=post_id,
                    comment_id=None,
                    snippet=f"Snippet item {i:02d}",
                    created_at=now - timedelta(minutes=30 - i),
                )
            )
        db_session.commit()

    # Page 1
    resp = client.get("/inbox")
    assert resp.status_code == 200
    assert "Older notifications" in resp.text
    assert "Snippet item 29" in resp.text  # Newest on page 1
    assert "Snippet item 05" in resp.text  # 25th item on page 1
    assert "Snippet item 04" not in resp.text  # Belongs to page 2

    # Extract cursor from Older notifications link
    cursor_match = re.search(r'href="/inbox\?cursor=([^"]+)"', resp.text)
    assert cursor_match is not None
    next_cursor = cursor_match.group(1)

    # Page 2 using valid cursor
    resp_page2 = client.get(f"/inbox?cursor={next_cursor}")
    assert resp_page2.status_code == 200
    assert "Snippet item 04" in resp_page2.text
    assert "Snippet item 00" in resp_page2.text
    assert "Older notifications" not in resp_page2.text  # Exhausted

    # Invalid cursor formats -> 400 Bad Request
    assert client.get("/inbox?cursor=malformed").status_code == 400
    assert client.get("/inbox?cursor=not-a-date|123").status_code == 400
    assert client.get("/inbox?cursor=2026-01-01T00:00:00|not-an-int").status_code == 400


def test_inbox_unread_count_badge_in_navigation(app, client, db_session):
    """Test unread badge in base navigation reflects current unread count."""
    _register_and_login(client, "nav_badge_user")

    # 1. No notifications -> Inbox link present, but no badge
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Inbox" in resp.text
    assert "badge--unread" not in resp.text

    # 2. Add 3 unread notifications
    with app.app_context():
        _create_synthetic_user(db_session, "badge_bot")
        for i in range(3):
            db_session.add(
                Notification(
                    recipient="nav_badge_user",
                    kind="reply",
                    actor="badge_bot",
                    snippet=f"Unread snippet {i}",
                )
            )
        db_session.commit()

    resp2 = client.get("/")
    assert resp2.status_code == 200
    assert '<span class="badge badge--unread">3</span>' in resp2.text

    # 3. Mark 1 notification read
    with app.app_context():
        notif = Notification.query.filter_by(recipient="nav_badge_user").first()
        mark_inbox_read("nav_badge_user", ids=[notif.id])

    resp3 = client.get("/")
    assert resp3.status_code == 200
    assert '<span class="badge badge--unread">2</span>' in resp3.text


def test_inbox_read_single_id_redirects_safely(app, client, db_session):
    """Test POST /inbox/read with single id marks read and redirects to safe next target."""
    _register_and_login(client, "single_reader")

    with app.app_context():
        _create_synthetic_user(db_session, "bot")
        sub = Subdeaddit(name="general", description="General")
        db_session.add(sub)
        post = Post(
            title="Post",
            content="Content",
            user="single_reader",
            subdeaddit_name="general",
            model="human",
        )
        db_session.add(post)
        db_session.flush()

        comment = Comment(
            post_id=post.id,
            user="bot",
            content="Test reply",
            model="agent:test",
        )
        db_session.add(comment)
        db_session.flush()

        notif = Notification(
            recipient="single_reader",
            kind="reply",
            actor="bot",
            post_id=post.id,
            comment_id=comment.id,
            snippet="Test reply",
        )
        db_session.add(notif)
        db_session.commit()
        notif_id = notif.id
        post_id = post.id
        comment_id = comment.id

    # 1. POST /inbox/read with valid next
    resp = client.post(
        "/inbox/read",
        data={
            "notification_id": str(notif_id),
            "next": f"/d/general/{post_id}#comment-{comment_id}",
        },
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == f"/d/general/{post_id}#comment-{comment_id}"

    with app.app_context():
        updated = db.session.get(Notification, notif_id)
        assert updated.read_at is not None

    # 2. POST /inbox/read with unsafe external next falls back to /inbox
    resp_unsafe = client.post(
        "/inbox/read",
        data={
            "notification_id": str(notif_id),
            "next": "https://evil.com/phish",
        },
    )
    assert resp_unsafe.status_code == 302
    assert resp_unsafe.headers["Location"] == "/inbox"


def test_inbox_read_all(app, client, db_session):
    """Test POST /inbox/read with notification_id='all' marks all unread items read."""
    _register_and_login(client, "read_all_user")

    with app.app_context():
        _create_synthetic_user(db_session, "bot")
        for i in range(3):
            db_session.add(
                Notification(
                    recipient="read_all_user",
                    kind="reply",
                    actor="bot",
                    snippet=f"Snippet {i}",
                )
            )
        db_session.commit()

        assert (
            Notification.query.filter_by(
                recipient="read_all_user", read_at=None
            ).count()
            == 3
        )

    resp = client.post(
        "/inbox/read",
        data={"notification_id": "all", "next": "/inbox"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/inbox"

    with app.app_context():
        assert (
            Notification.query.filter_by(
                recipient="read_all_user", read_at=None
            ).count()
            == 0
        )


def test_inbox_cross_user_isolation(app, client, db_session):
    """Test User A cannot view or mark User B's notifications."""
    _register_and_login(client, "user_a")

    with app.app_context():
        _create_synthetic_user(db_session, "bot")
        # Create user_b directly
        user_b = User(
            username="user_b",
            password_hash="pbkdf2:sha256:dummy",
            model="human",
        )
        db_session.add(user_b)
        db_session.flush()

        notif_b = Notification(
            recipient="user_b",
            kind="reply",
            actor="bot",
            snippet="Secret notification for User B",
        )
        db_session.add(notif_b)
        db_session.commit()
        notif_b_id = notif_b.id

    # 1. User A visits /inbox -> User B's notification is not present
    resp = client.get("/inbox")
    assert resp.status_code == 200
    assert "Secret notification for User B" not in resp.text

    # 2. User A attempts to mark User B's notification read
    resp_mark = client.post(
        "/inbox/read",
        data={"notification_id": str(notif_b_id), "next": "/inbox"},
    )
    assert resp_mark.status_code == 302

    with app.app_context():
        # User B's notification remains unread!
        b_row = db.session.get(Notification, notif_b_id)
        assert b_row.read_at is None


def test_ai_comment_on_human_post_emits_notification_with_working_link(
    app, client, db_session
):
    """Test AI comment on human post creates notification with working canonical URL."""
    _register_and_login(client, "human_op")

    with app.app_context():
        _create_synthetic_user(db_session, "bot_commenter")
        sub = Subdeaddit(name="general", description="General")
        db_session.add(sub)
        post = Post(
            title="Post by OP",
            content="Content",
            user="human_op",
            subdeaddit_name="general",
            model="human",
        )
        db_session.add(post)
        db_session.commit()
        post_id = post.id

        # AI creates top-level comment
        comment = create_comment(
            post_id=post_id,
            content="AI insight on your post",
            user="bot_commenter",
        )
        comment_id = comment.id

        notifs = Notification.query.filter_by(recipient="human_op").all()
        assert len(notifs) == 1
        assert notifs[0].kind == "reply"
        assert notifs[0].post_id == post_id
        assert notifs[0].comment_id == comment_id
        assert notifs[0].snippet == "AI insight on your post"

    resp = client.get("/inbox")
    assert resp.status_code == 200
    assert f"/d/general/{post_id}#comment-{comment_id}" in resp.text
    assert "AI insight on your post" in resp.text


def test_ai_reply_to_human_comment_emits_notification_with_working_link(
    app, client, db_session
):
    """Test AI reply to human comment creates notification with working canonical URL."""
    _register_and_login(client, "human_commenter")

    with app.app_context():
        _create_synthetic_user(db_session, "bot_author")
        _create_synthetic_user(db_session, "bot_replier")
        sub = Subdeaddit(name="general", description="General")
        db_session.add(sub)
        post = Post(
            title="Post by bot",
            content="Content",
            user="bot_author",
            subdeaddit_name="general",
            model="agent:test",
        )
        db_session.add(post)
        db_session.commit()
        post_id = post.id

        human_comment = create_comment(
            post_id=post_id,
            content="Human comment on thread",
            user="human_commenter",
        )

        ai_reply = create_comment(
            post_id=post_id,
            content="AI response to human comment",
            user="bot_replier",
            parent_id=human_comment.id,
        )
        reply_id = ai_reply.id

        notifs = Notification.query.filter_by(recipient="human_commenter").all()
        assert len(notifs) == 1
        assert notifs[0].kind == "reply"
        assert notifs[0].post_id == post_id
        assert notifs[0].comment_id == reply_id
        assert notifs[0].snippet == "AI response to human comment"

    resp = client.get("/inbox")
    assert resp.status_code == 200
    assert f"/d/general/{post_id}#comment-{reply_id}" in resp.text


def test_human_recipient_bypasses_dedupe_and_reply_fatigue(app, db_session):
    """Test human recipient bypasses rolling dedupe and reply-chain fatigue suppression."""
    with app.app_context():
        human = User(
            username="human_hero",
            password_hash="pbkdf2:sha256:dummy",
            model="human",
        )
        bot = User(username="bot_buddy", model="agent:test")
        sub = Subdeaddit(name="general", description="General")
        db_session.add_all([human, bot, sub])
        db_session.flush()

        post = Post(
            title="Hero Post",
            content="Content",
            user="human_hero",
            subdeaddit_name="general",
            model="human",
        )
        db_session.add(post)
        db_session.commit()
        post_id = post.id

        # 1. Rolling dedupe bypass: bot replies twice to human in same post within 1 hour
        c1 = create_comment(post_id=post_id, content="Reply 1", user="bot_buddy")
        c2 = create_comment(post_id=post_id, content="Reply 2", user="bot_buddy")

        notifs = Notification.query.filter_by(
            recipient="human_hero", kind="reply"
        ).all()
        assert len(notifs) == 2  # Dedupe bypassed for human recipient

        # 2. Reply-chain fatigue bypass:
        Setting.set_value("reply_exchange_cap_min", "2")
        Setting.set_value("reply_exchange_cap_max", "2")

        # Build alternating chain: c1 (bot) -> c3 (human) -> c4 (bot) -> c5 (human) -> c6 (bot)
        c3 = create_comment(
            post_id=post_id,
            content="Human reply 1",
            user="human_hero",
            parent_id=c1.id,
        )
        c4 = create_comment(
            post_id=post_id, content="Bot reply 2", user="bot_buddy", parent_id=c3.id
        )
        c5 = create_comment(
            post_id=post_id,
            content="Human reply 3",
            user="human_hero",
            parent_id=c4.id,
        )
        # exchange_tail is >= cap=2.
        c6 = create_comment(
            post_id=post_id,
            content="Bot reply 4 capping exchange",
            user="bot_buddy",
            parent_id=c5.id,
        )

        c6_notifs = Notification.query.filter_by(
            recipient="human_hero", comment_id=c6.id
        ).all()
        assert len(c6_notifs) == 1  # Fatigue bypassed for human recipient


def test_synthetic_recipient_respects_dedupe_and_reply_fatigue(app, db_session):
    """Test AI recipient respects rolling dedupe and reply-chain fatigue cap."""
    with app.app_context():
        bot_target = User(username="ai_target", model="agent:test")
        bot_actor = User(username="ai_actor", model="agent:test")
        sub = Subdeaddit(name="general", description="General")
        db_session.add_all([bot_target, bot_actor, sub])
        db_session.flush()

        post = Post(
            title="AI Post",
            content="Content",
            user="ai_target",
            subdeaddit_name="general",
            model="agent:test",
        )
        db_session.add(post)
        db_session.commit()
        post_id = post.id

        # 1. Rolling dedupe: bot_actor comments twice on same post within 1 hour
        c1 = create_comment(post_id=post_id, content="Bot reply 1", user="ai_actor")
        c2 = create_comment(post_id=post_id, content="Bot reply 2", user="ai_actor")

        notifs = Notification.query.filter_by(
            recipient="ai_target", kind="reply"
        ).all()
        assert len(notifs) == 1
        assert notifs[0].comment_id == c1.id

        # 2. Reply-chain fatigue:
        Setting.set_value("reply_exchange_cap_min", "2")
        Setting.set_value("reply_exchange_cap_max", "2")

        Notification.query.delete()
        db_session.commit()

        c_root = create_comment(
            post_id=post_id, content="Root by target", user="ai_target"
        )
        r1 = create_comment(
            post_id=post_id,
            content="Reply 1 by actor",
            user="ai_actor",
            parent_id=c_root.id,
        )
        r2 = create_comment(
            post_id=post_id,
            content="Reply 2 by target",
            user="ai_target",
            parent_id=r1.id,
        )
        # Tail >= cap=2. Fatigue suppresses notification for synthetic ai_target!
        r3 = create_comment(
            post_id=post_id,
            content="Reply 3 by actor capping exchange",
            user="ai_actor",
            parent_id=r2.id,
        )

        r3_notifs = Notification.query.filter_by(
            recipient="ai_target", comment_id=r3.id
        ).all()
        assert len(r3_notifs) == 0


def test_inbox_anonymous_and_disabled_access(app, client, db_session, monkeypatch):
    """Test anonymous access redirects to login and disabled feature returns 404."""
    # 1. Anonymous GET /inbox redirects to login with next=/inbox
    resp = client.get("/inbox")
    assert resp.status_code == 302
    assert (
        "/login?next=%2Finbox" in resp.headers["Location"]
        or "/login?next=/inbox" in resp.headers["Location"]
    )

    # 2. Anonymous POST /inbox/read redirects to login
    resp_read = client.post("/inbox/read", data={"notification_id": "all"})
    assert resp_read.status_code == 302
    assert "/login" in resp_read.headers["Location"]

    # 3. Signed in, but feature disabled -> 404
    _register_and_login(client, "gated_inbox_user")
    monkeypatch.setenv("HUMAN_ACCOUNTS_ENABLED", "false")

    assert client.get("/inbox").status_code == 404
    assert client.post("/inbox/read", data={"notification_id": "all"}).status_code == 404

    # Nav does not show Inbox when disabled
    resp_nav = client.get("/")
    assert resp_nav.status_code == 200
    assert "Inbox" not in resp_nav.text


def test_post_convenience_redirect(app, client, db_session):
    """Test /post/<id> convenience route redirects to /d/<subdeaddit>/<id>."""
    with app.app_context():
        _create_synthetic_user(db_session, "poster")
        sub = Subdeaddit(name="technology", description="Tech")
        db_session.add(sub)
        post = Post(
            title="Tech Post",
            content="Content",
            user="poster",
            subdeaddit_name="technology",
            model="agent:test",
        )
        db_session.add(post)
        db_session.commit()
        post_id = post.id

    resp = client.get(f"/post/{post_id}")
    assert resp.status_code == 302
    assert resp.headers["Location"] == f"/d/technology/{post_id}"

    # Non-existent post returns 404
    assert client.get("/post/999999").status_code == 404

