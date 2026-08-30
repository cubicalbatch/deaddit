"""A6: config/secrets split and settings TTL cache."""

from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import text

import deaddit.settings.service as settings_service
from deaddit import create_app, db
from deaddit.config import SECRET_KEYS, Config, is_secret_key
from deaddit.models import Setting
from deaddit.settings.service import SecretNotPersistable


@pytest.fixture(autouse=True)
def _fresh_settings_cache(monkeypatch):
    """Isolate the process-global cache and TTL env between tests."""
    settings_service.clear()
    monkeypatch.delenv("DEADDIT_SETTINGS_TTL_SECONDS", raising=False)
    monkeypatch.delenv("DEADDIT_DB_PATH", raising=False)
    yield
    settings_service.clear()


# ---------------------------------------------------------------------------
# Non-secret precedence (unchanged: DB > env > DEFAULTS > default param)
# ---------------------------------------------------------------------------


def test_nonsecret_db_beats_env(app, db_session, monkeypatch):
    monkeypatch.setenv("SEED_VOTE_MAX", "100")
    Setting.set_value("SEED_VOTE_MAX", "200", None)
    assert Config.get("SEED_VOTE_MAX") == "200"


def test_nonsecret_env_beats_default_when_no_row(app, db_session, monkeypatch):
    db_session.query(Setting).filter(Setting.key == "SEED_VOTE_MAX").delete()
    db_session.commit()
    monkeypatch.setenv("SEED_VOTE_MAX", "100")
    assert Config.get("SEED_VOTE_MAX") == "100"


def test_nonsecret_default_used_when_db_and_env_unset(app, db_session, monkeypatch):
    db_session.query(Setting).filter(Setting.key == "SEED_VOTE_MAX").delete()
    db_session.commit()
    monkeypatch.delenv("SEED_VOTE_MAX", raising=False)
    assert Config.get("SEED_VOTE_MAX") == Config.DEFAULTS["SEED_VOTE_MAX"]


def test_nonsecret_param_default_wins_over_nothing(app, db_session, monkeypatch):
    db_session.query(Setting).filter(Setting.key == "SEED_ANCHOR_AT").delete()
    db_session.commit()
    monkeypatch.delenv("SEED_ANCHOR_AT", raising=False)
    assert Config.get("SEED_ANCHOR_AT") is None
    assert Config.get("SEED_ANCHOR_AT", "fallback") == "fallback"


# ---------------------------------------------------------------------------
# TTL cache correctness
# ---------------------------------------------------------------------------


def test_cached_get_hits_database_once(app, db_session, monkeypatch):
    Setting.set_value("SEED_VOTE_MAX", "50", None)
    calls = []
    original = Setting.get_value

    def counting_get_value(key, default=None):
        calls.append(key)
        return original(key, default)

    monkeypatch.setattr(Setting, "get_value", counting_get_value)
    assert Config.get("SEED_VOTE_MAX") == "50"
    assert Config.get("SEED_VOTE_MAX") == "50"
    assert Config.get("SEED_VOTE_MAX") == "50"
    assert calls == ["SEED_VOTE_MAX"]


def test_config_set_visible_immediately(app, db_session):
    Setting.set_value("OPENAI_MODEL", "before", None)
    assert Config.get("OPENAI_MODEL") == "before"
    Config.set("OPENAI_MODEL", "after")
    # Invalidation boundary: no TTL wait, no manual cache clear.
    assert Config.get("OPENAI_MODEL") == "after"


def test_direct_orm_write_visible_immediately_via_event_hook(app, db_session):
    Setting.set_value("SEED_VOTE_MAX", "before", None)
    assert Config.get("SEED_VOTE_MAX") == "before"
    row = db_session.get(Setting, "SEED_VOTE_MAX")
    row.value = "after"
    db_session.commit()
    # after_flush event hook invalidated the cache without any manual step.
    assert Config.get("SEED_VOTE_MAX") == "after"


def test_cache_expires_after_ttl(app, db_session, monkeypatch):
    real_monotonic = settings_service.time.monotonic
    offset = [0.0]
    monkeypatch.setattr(
        settings_service.time, "monotonic", lambda: real_monotonic() + offset[0]
    )
    Setting.set_value("SEED_VOTE_MAX", "50", None)
    assert Config.get("SEED_VOTE_MAX") == "50"

    # Mutate the DB behind the cache's back: Core SQL update produces no ORM
    # flush of Setting instances, so the invalidation hook must not fire.
    table = Setting.__tablename__
    db_session.execute(
        text(f"UPDATE {table} SET value = '100' WHERE key = 'SEED_VOTE_MAX'")
    )
    db_session.commit()

    # Within the TTL the cached value persists.
    assert offset[0] < settings_service.ttl_seconds()
    assert Config.get("SEED_VOTE_MAX") == "50"

    # Advance past the TTL -> next get re-reads the database.
    offset[0] = settings_service.ttl_seconds() + 1.0
    assert Config.get("SEED_VOTE_MAX") == "100"


def test_cached_unit_semantics_negative_lookup_and_expiry(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(settings_service.time, "monotonic", lambda: now[0])
    calls = []

    def resolver():
        calls.append(1)
        return "value"

    assert settings_service.cached("unit-key", resolver) == "value"
    assert settings_service.cached("unit-key", resolver) == "value"
    assert len(calls) == 1

    def missing_resolver():
        calls.append(1)
        return None

    assert settings_service.cached("absent-key", missing_resolver) is None
    assert settings_service.cached("absent-key", missing_resolver) is None
    # Negative lookups are served from the cache too (no extra resolver calls).
    now[0] += settings_service.ttl_seconds() + 1.0
    assert settings_service.cached("unit-key", resolver) == "value"
    # Calls: initial resolve(1) + first negative resolve(1) + expiry re-read(1).
    assert len(calls) == 3


def test_ttl_seconds_from_env(monkeypatch):
    monkeypatch.setenv("DEADDIT_SETTINGS_TTL_SECONDS", "0.5")
    assert settings_service.ttl_seconds() == 0.5
    monkeypatch.setenv("DEADDIT_SETTINGS_TTL_SECONDS", "not-a-number")
    assert settings_service.ttl_seconds() == settings_service.DEFAULT_TTL_SECONDS


def test_invalidation_hook_registration_is_idempotent():
    settings_service.register_invalidation_hook()
    settings_service.register_invalidation_hook()
    assert settings_service._hook_registered is True


def test_create_app_clears_settings_cache(app, monkeypatch):
    settings_service.cached("CANARY_KEY", lambda: "stale-value")
    assert "CANARY_KEY" in settings_service._cache
    create_app({"SQLALCHEMY_DATABASE_URI": "sqlite://", "TESTING": True})
    assert "CANARY_KEY" not in settings_service._cache
    assert Config.get("CANARY_KEY") is None


# ---------------------------------------------------------------------------
# Env-only secrets
# ---------------------------------------------------------------------------


def test_secret_ignores_db_row(app, db_session, monkeypatch):
    Setting.set_value("API_TOKEN", "legacy-db-token", None)
    monkeypatch.delenv("API_TOKEN", raising=False)
    # Secrets are strictly environment-only; DB rows are ignored
    assert Config.get("API_TOKEN") is None

    monkeypatch.setenv("API_TOKEN", "env-token")
    assert Config.get("API_TOKEN") == "env-token"


def test_secret_returns_default_when_neither_env_nor_row(app, db_session, monkeypatch):
    db_session.query(Setting).filter(Setting.key == "API_TOKEN").delete()
    db_session.commit()
    monkeypatch.delenv("API_TOKEN", raising=False)
    assert Config.get("API_TOKEN") is None
    assert Config.get("API_TOKEN", "fallback") == "fallback"


@pytest.mark.parametrize(
    "key", ["API_TOKEN", "SECRET_KEY", "OPENAI_KEY", "API_KEY_GROQ"]
)
def test_set_refuses_secret_keys(app, key):
    with pytest.raises(SecretNotPersistable, match="environment"):
        Config.set(key, "boom")


def test_is_secret_key_covers_prefix_and_frozenset():
    assert SECRET_KEYS == frozenset({"API_TOKEN", "SECRET_KEY", "OPENAI_KEY"})
    assert is_secret_key("API_KEY_WHATEVER")
    assert is_secret_key("API_TOKEN")
    assert not is_secret_key("PRODUCTION")
    assert not is_secret_key("SEED_VOTE_MAX")


# ---------------------------------------------------------------------------
# UX-5 defect regressions
# ---------------------------------------------------------------------------


def test_defect1_endpoint_double_write_refused_and_zero_rows(app, db_session):
    url = "https://api.groq.com/openai/v1"
    Setting.set_value("OPENAI_API_URL", url, None)
    with pytest.raises(SecretNotPersistable):
        Config.set_api_key_for_endpoint(url, "sk-test")
    secret_rows = [
        row.key for row in db_session.query(Setting).all() if is_secret_key(row.key)
    ]
    assert secret_rows == []


def test_defect2_get_all_settings_masks_every_secret(app, db_session, monkeypatch):
    monkeypatch.setenv("OPENAI_KEY", "sk-openai-plain")
    monkeypatch.setenv("API_TOKEN", "tok-plain")
    monkeypatch.setenv("API_KEY_GROQ", "gsk-plain")

    all_blob = json.dumps(Config.get_all_settings())
    for plaintext in ("sk-openai-plain", "tok-plain", "gsk-plain"):
        assert plaintext not in all_blob

    settings_view = Config.get_all_settings()
    for key in ("OPENAI_KEY", "API_TOKEN", "SECRET_KEY"):
        assert settings_view[key]["value"] in {"***set***", "***not set***"}


def test_defect3_virgin_initialize_defaults_writes_no_secret_rows(
    app, db_session, monkeypatch
):
    db_session.query(Setting).delete()
    db_session.commit()
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    Config.initialize_defaults()
    secret_rows = [
        row.key for row in db_session.query(Setting).all() if is_secret_key(row.key)
    ]
    assert secret_rows == []
    assert not Config.get_api_key_for_endpoint("https://api.openai.com/v1")
    assert Config.get("OPENAI_KEY") is None


# ---------------------------------------------------------------------------
# DEADDIT_DB_PATH (Wave-0 ruling)
# ---------------------------------------------------------------------------


def test_deaddit_db_path_sets_base_uri(tmp_path, monkeypatch):
    target = tmp_path / "data" / "custom.db"
    monkeypatch.setenv("DEADDIT_DB_PATH", str(target))
    app = create_app({"TESTING": True})
    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///" + str(target)


def test_explicit_config_override_beats_deaddit_db_path(tmp_path, monkeypatch):
    monkeypatch.setenv("DEADDIT_DB_PATH", str(tmp_path / "env.db"))
    explicit = f"sqlite:///{tmp_path}/explicit.db"
    app = create_app({"SQLALCHEMY_DATABASE_URI": explicit})
    assert app.config["SQLALCHEMY_DATABASE_URI"] == explicit


def test_engine_actually_lands_in_env_file(tmp_path, monkeypatch):
    target = tmp_path / "landed.db"
    monkeypatch.setenv("DEADDIT_DB_PATH", str(target))
    app = create_app({"TESTING": True})
    with app.app_context():
        db.create_all()
    assert os.path.exists(str(target))
