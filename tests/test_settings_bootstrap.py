"""Regression coverage for first-start settings seeding."""

from __future__ import annotations

from deaddit.config import Config, is_secret_key
from deaddit.models import Setting
from deaddit.settings.service import clear


def _empty_settings(db_session):
    db_session.query(Setting).delete()
    db_session.commit()
    clear()


def test_initialize_defaults_leaves_environment_overrides_unseeded(
    app, db_session, monkeypatch
):
    _empty_settings(db_session)
    monkeypatch.setenv("OPENAI_API_URL", "https://configured.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "configured-model")

    Config.initialize_defaults()
    clear()

    assert Setting.get_value("OPENAI_API_URL") is None
    assert Setting.get_value("OPENAI_MODEL") is None
    assert Config.get("OPENAI_API_URL") == "https://configured.example/v1"
    assert Config.get("OPENAI_MODEL") == "configured-model"


def test_initialize_defaults_preserves_explicit_database_values(
    app, db_session, monkeypatch
):
    _empty_settings(db_session)
    Setting.set_value("OPENAI_API_URL", "https://database.example/v1")
    monkeypatch.setenv("OPENAI_API_URL", "https://configured.example/v1")

    Config.initialize_defaults()
    clear()

    assert Setting.get_value("OPENAI_API_URL") == "https://database.example/v1"
    assert Config.get("OPENAI_API_URL") == "https://database.example/v1"


def test_initialize_defaults_seeds_unset_nonsecret_defaults(app, db_session, monkeypatch):
    _empty_settings(db_session)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    Config.initialize_defaults()
    clear()

    assert Setting.get_value("OPENAI_MODEL") == Config.DEFAULTS["OPENAI_MODEL"]
    assert Config.get("OPENAI_MODEL") == Config.DEFAULTS["OPENAI_MODEL"]


def test_initialize_defaults_never_persists_secrets_or_deploy_flags(
    app, db_session, monkeypatch
):
    _empty_settings(db_session)
    monkeypatch.setenv("OPENAI_KEY", "env-secret")
    monkeypatch.setenv("API_TOKEN", "env-token")
    monkeypatch.setenv("SECRET_KEY", "env-session-secret")
    monkeypatch.setenv("PRODUCTION", "true")

    Config.initialize_defaults()

    keys = {row.key for row in db_session.query(Setting).all()}
    assert not keys.intersection({"OPENAI_KEY", "API_TOKEN", "SECRET_KEY", "PRODUCTION"})
    assert not any(is_secret_key(key) for key in keys)
