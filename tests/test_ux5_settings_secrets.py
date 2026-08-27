"""Settings secrets contract (UX-5 slice, updated by A6).

Since refactor A6, secrets are ENVIRONMENT-ONLY: the app resolves
API_TOKEN / SECRET_KEY / OPENAI_KEY / API_KEY_* from the environment and
refuses to persist them (``SecretNotPersistable``). These tests pin the
post-A6 behaviour of /admin/settings and its save APIs:

* blank/absent secret fields never touch anything,
* non-empty secrets are REFUSED with an explicit environment-only
  message while the non-secret rest of the request still commits,
* short tokens keep their dedicated validation error,
* no bullet-mask placeholder and no secret value ever reaches the
  rendered HTML or the JSON API responses,
* the endpoint-key status endpoint reports has_key/last4 only.
"""

from __future__ import annotations

import pytest

from deaddit.models import Setting

OPENAI_KEY = "sk-test-openai-key-abcdef123456"
API_TOKEN = "unit-test-token-987654"
ENDPOINT = "https://api.groq.com/openai/v1"
ENDPOINT_KEY = "gsk-test-groq-key-abcdef123456"

SECRET_KEYS = ("OPENAI_KEY", "API_TOKEN", "API_KEY_GROQ")


def _setting_value(db_session, key: str) -> str | None:
    row = db_session.get(Setting, key)
    return None if row is None else row.value


@pytest.fixture()
def admin_client(client):
    """Client that passes admin_required even after API_TOKEN is stored."""
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


@pytest.fixture()
def env_secrets(monkeypatch):
    """Seed secrets in the environment."""
    monkeypatch.setenv("OPENAI_KEY", OPENAI_KEY)
    monkeypatch.setenv("API_TOKEN", API_TOKEN)
    monkeypatch.setenv("API_KEY_GROQ", ENDPOINT_KEY)
    return {
        "OPENAI_KEY": OPENAI_KEY,
        "API_TOKEN": API_TOKEN,
        "API_KEY_GROQ": ENDPOINT_KEY,
    }


def test_blank_secrets_on_save_do_not_persist(admin_client, db_session):
    # First save with every secret field blank.
    resp = admin_client.post(
        "/admin/api/save-config",
        json={"openai_api_url": ENDPOINT, "openai_key": "", "openai_model": "llama3"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True

    resp = admin_client.post(
        "/admin/api/save-deaddit-config",
        json={"api_base_url": "http://localhost:5000", "api_token": ""},
    )
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True

    # Second save with the keys absent from the payload entirely.
    resp = admin_client.post("/admin/api/save-config", json={"openai_model": "llama3"})
    assert resp.get_json()["success"] is True
    resp = admin_client.post("/admin/api/save-deaddit-config", json={})
    assert resp.get_json()["success"] is True

    for k in SECRET_KEYS:
        assert _setting_value(db_session, k) is None


def test_whitespace_only_secret_is_ignored(admin_client, db_session):
    """Whitespace-only payloads count as blank and do not persist."""
    resp = admin_client.post("/admin/api/save-config", json={"openai_key": "   \t "})
    assert resp.get_json()["success"] is True
    resp = admin_client.post("/admin/api/save-deaddit-config", json={"api_token": "  "})
    assert resp.get_json()["success"] is True

    assert _setting_value(db_session, "OPENAI_KEY") is None
    assert _setting_value(db_session, "API_TOKEN") is None


# ---------------------------------------------------------------------------
# (b) non-empty secrets are refused: the database is no longer their home


def test_nonempty_secrets_are_refused_env_only(admin_client, db_session):
    """A6: save APIs reject secret values and write NO secret rows.

    Regression guard for the UX-5 double-write defect too: even when the
    posted endpoint equals the current OPENAI_API_URL, neither OPENAI_KEY
    nor the mirrored API_KEY_* row may appear.
    """
    resp = admin_client.post(
        "/admin/api/save-config",
        json={"openai_api_url": ENDPOINT, "openai_key": OPENAI_KEY},
    )
    body = resp.get_json()
    assert body["success"] is False
    assert "environment-only" in body["message"]

    resp = admin_client.post(
        "/admin/api/save-deaddit-config", json={"api_token": API_TOKEN}
    )
    body = resp.get_json()
    assert body["success"] is False
    assert "environment-only" in body["message"]

    for key in SECRET_KEYS:
        assert _setting_value(db_session, key) is None


def test_refused_secret_still_commits_other_fields(admin_client, db_session):
    """The non-secret rest of a request survives the secret refusal."""
    resp = admin_client.post(
        "/admin/api/save-config",
        json={
            "openai_api_url": ENDPOINT,
            "openai_key": OPENAI_KEY,
            "openai_model": "llama3",
        },
    )
    assert resp.get_json()["success"] is False
    assert _setting_value(db_session, "OPENAI_MODEL") == "llama3"
    assert _setting_value(db_session, "OPENAI_API_URL") == ENDPOINT


def test_short_token_is_rejected_without_write(admin_client, db_session):
    resp = admin_client.post("/admin/api/save-deaddit-config", json={"api_token": "ab"})
    body = resp.get_json()
    assert body["success"] is False
    assert _setting_value(db_session, "API_TOKEN") is None


# ---------------------------------------------------------------------------
# (c) rendered page leaks neither bullet masks nor secret values


def test_render_hides_all_secrets(admin_client, db_session, env_secrets):
    resp = admin_client.get("/admin/settings")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "•••" not in html
    assert OPENAI_KEY not in html
    assert API_TOKEN not in html
    assert ENDPOINT_KEY not in html
    # Status booleans are rendered instead.
    assert ">set</span>" in html


# ---------------------------------------------------------------------------
# (d) JSON APIs succeed without echoing secret values


def test_save_config_response_has_no_secret_echo(admin_client, db_session):
    resp = admin_client.post(
        "/admin/api/save-config",
        json={"openai_api_url": ENDPOINT, "openai_key": OPENAI_KEY},
    )
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert OPENAI_KEY not in text
    body = resp.get_json()
    # Refused under A6 (env-only), and the refusal never echoes the value.
    assert body["success"] is False
    assert "openai_key" not in body["config"]


def test_save_deaddit_config_response_has_no_secret_echo(admin_client, db_session):
    resp = admin_client.post(
        "/admin/api/save-deaddit-config", json={"api_token": API_TOKEN}
    )
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert API_TOKEN not in text


def test_get_endpoint_key_never_returns_full_key(admin_client, db_session, monkeypatch):
    """Env-provided keys surface only as has_key/last4, never plaintext."""
    monkeypatch.setenv("OPENAI_KEY", ENDPOINT_KEY)

    resp = admin_client.post(
        "/admin/api/get-endpoint-key", json={"endpoint_url": ENDPOINT}
    )
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert ENDPOINT_KEY not in text
    assert "•••" not in text

    body = resp.get_json()
    assert body["success"] is True
    assert body["has_key"] is True
    assert body["last4"] == ENDPOINT_KEY[-4:]
    assert "api_key" not in body
    assert "masked_key" not in body


def test_get_endpoint_key_reports_unset(admin_client, db_session):
    """Virgin deployments report has_key=False (no sentinel fallback — A6 fix)."""
    resp = admin_client.post(
        "/admin/api/get-endpoint-key",
        json={"endpoint_url": "http://localhost:11434/v1"},
    )
    body = resp.get_json()
    assert body["success"] is True
    assert body["has_key"] is False
    assert body["last4"] is None
