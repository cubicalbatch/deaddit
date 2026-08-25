"""UX-5 settings slice: empty-means-unchanged secret semantics.

Covers the settings IA contract for /admin/settings and its save APIs:

* blank/absent secret fields (OPENAI key, API_TOKEN, per-endpoint keys)
  never overwrite stored values (byte-identical Setting rows),
* non-empty secrets are written,
* no bullet-mask placeholder and no secret value ever reaches the
  rendered HTML or the JSON API responses.
"""

from __future__ import annotations

import pytest

from deaddit.models import Setting

OPENAI_KEY = "sk-test-openai-key-abcdef123456"
API_TOKEN = "unit-test-token-987654"
ENDPOINT = "https://api.groq.com/openai/v1"
ENDPOINT_KEY = "gsk-test-groq-key-abcdef123456"


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
def stored_secrets(admin_client, db_session):
    """Seed one of each secret flavor via the public save APIs."""
    resp = admin_client.post(
        "/admin/api/save-config",
        json={
            "openai_api_url": ENDPOINT,
            "openai_key": OPENAI_KEY,
        },
    )
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True

    resp = admin_client.post(
        "/admin/api/save-deaddit-config",
        json={"api_token": API_TOKEN},
    )
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True

    # The endpoint key lands under API_KEY_GROQ (set_api_key_for_endpoint).
    return {
        "OPENAI_KEY": _setting_value(db_session, "OPENAI_KEY"),
        "API_TOKEN": _setting_value(db_session, "API_TOKEN"),
        "API_KEY_GROQ": _setting_value(db_session, "API_KEY_GROQ"),
    }


def test_blank_secrets_are_unchanged_on_resave(stored_secrets, admin_client, db_session):
    before = {k: _setting_value(db_session, k) for k in ("OPENAI_KEY", "API_TOKEN", "API_KEY_GROQ")}
    assert before == stored_secrets

    # First re-save with every secret field blank.
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

    # Second re-save with the keys absent from the payload entirely.
    resp = admin_client.post("/admin/api/save-config", json={"openai_model": "llama3"})
    assert resp.get_json()["success"] is True
    resp = admin_client.post("/admin/api/save-deaddit-config", json={})
    assert resp.get_json()["success"] is True

    after = {k: _setting_value(db_session, k) for k in ("OPENAI_KEY", "API_TOKEN", "API_KEY_GROQ")}
    assert after == before


def test_whitespace_only_secret_is_unchanged(stored_secrets, admin_client, db_session):
    """Whitespace-only payloads count as blank and must not clobber secrets."""
    before = {k: _setting_value(db_session, k) for k in ("OPENAI_KEY", "API_TOKEN")}

    resp = admin_client.post(
        "/admin/api/save-config", json={"openai_key": "   \t "}
    )
    assert resp.get_json()["success"] is True
    resp = admin_client.post("/admin/api/save-deaddit-config", json={"api_token": "  "})
    assert resp.get_json()["success"] is True

    assert _setting_value(db_session, "OPENAI_KEY") == before["OPENAI_KEY"]
    assert _setting_value(db_session, "API_TOKEN") == before["API_TOKEN"]


# ---------------------------------------------------------------------------
# (b) non-empty secrets update stored values


def test_new_nonempty_secrets_update_rows(admin_client, db_session):
    resp = admin_client.post(
        "/admin/api/save-config",
        json={"openai_api_url": ENDPOINT, "openai_key": OPENAI_KEY},
    )
    assert resp.get_json()["success"] is True
    assert _setting_value(db_session, "OPENAI_KEY") == OPENAI_KEY
    # Endpoint-specific storage mirrors the default while it is the current endpoint.
    assert _setting_value(db_session, "API_KEY_GROQ") == OPENAI_KEY

    resp = admin_client.post("/admin/api/save-deaddit-config", json={"api_token": API_TOKEN})
    assert resp.get_json()["success"] is True
    assert _setting_value(db_session, "API_TOKEN") == API_TOKEN

    # Rotating to a new value overwrites; blank afterwards keeps the rotation.
    new_key = "sk-rotated-key-000000"
    resp = admin_client.post(
        "/admin/api/save-config",
        json={"openai_api_url": ENDPOINT, "openai_key": new_key},
    )
    assert resp.get_json()["success"] is True
    assert _setting_value(db_session, "OPENAI_KEY") == new_key

    resp = admin_client.post("/admin/api/save-config", json={"openai_key": ""})
    assert resp.get_json()["success"] is True
    assert _setting_value(db_session, "OPENAI_KEY") == new_key


def test_short_token_is_rejected_without_write(admin_client, db_session):
    resp = admin_client.post("/admin/api/save-deaddit-config", json={"api_token": "ab"})
    body = resp.get_json()
    assert body["success"] is False
    assert _setting_value(db_session, "API_TOKEN") is None


# ---------------------------------------------------------------------------
# (c) rendered page leaks neither bullet masks nor secret values


def test_render_hides_all_secrets(admin_client, db_session, stored_secrets):
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
    assert body["success"] is True
    assert "openai_key" not in body["config"]
    assert body["config"]["openai_key_set"] is True


def test_save_deaddit_config_response_has_no_secret_echo(admin_client, db_session):
    resp = admin_client.post(
        "/admin/api/save-deaddit-config", json={"api_token": API_TOKEN}
    )
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert API_TOKEN not in text
    assert resp.get_json()["success"] is True


def test_get_endpoint_key_never_returns_full_key(admin_client, db_session):
    admin_client.post(
        "/admin/api/save-config",
        json={"openai_api_url": ENDPOINT, "openai_key": ENDPOINT_KEY},
    )
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
    # get_api_key_for_endpoint falls back to OPENAI_KEY (whose DEFAULTS sentinel is
    # "your_openrouter_api_key"), so an endpoint only reports has_key=False when no
    # fallback value exists at all. Precedence quirk noted for A6.
    db_session.add(Setting(key="OPENAI_KEY", value=""))
    db_session.commit()

    resp = admin_client.post(
        "/admin/api/get-endpoint-key", json={"endpoint_url": "http://localhost:11434/v1"}
    )
    body = resp.get_json()
    assert body["success"] is True
    assert body["has_key"] is False
    assert body["last4"] is None


