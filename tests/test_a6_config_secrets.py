"""A6: config/secrets split, settings TTL cache, and the secrets-drain CLI."""

from __future__ import annotations

import json
import logging
import os

import pytest
from click.testing import CliRunner
from flask import Flask
from sqlalchemy import text

import deaddit.cli as cli_module
import deaddit.settings.service as settings_service
from deaddit import create_app, db
from deaddit.cli import cli
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


@pytest.fixture(autouse=True)
def _fresh_stale_warning_state():
    """Reset the once-per-process stale-secret warning set around each test."""
    from deaddit import config as config_module

    config_module._warned_stale_secrets.clear()
    yield
    config_module._warned_stale_secrets.clear()


# ---------------------------------------------------------------------------
# Non-secret precedence (unchanged: DB > env > DEFAULTS > default param)
# ---------------------------------------------------------------------------


def test_nonsecret_db_beats_env(app, db_session, monkeypatch):
    monkeypatch.setenv("MODELS", "from-env")
    Setting.set_value("MODELS", "from-db", None)
    assert Config.get("MODELS") == "from-db"


def test_nonsecret_env_beats_default_when_no_row(app, db_session, monkeypatch):
    db_session.query(Setting).filter(Setting.key == "MODELS").delete()
    db_session.commit()
    monkeypatch.setenv("MODELS", "from-env")
    assert Config.get("MODELS") == "from-env"


def test_nonsecret_default_used_when_db_and_env_unset(app, db_session, monkeypatch):
    db_session.query(Setting).filter(Setting.key == "MODELS").delete()
    db_session.commit()
    monkeypatch.delenv("MODELS", raising=False)
    assert Config.get("MODELS") == Config.DEFAULTS["MODELS"]


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
    Setting.set_value("API_BASE_URL", "http://db-base", None)
    calls = []
    original = Setting.get_value

    def counting_get_value(key, default=None):
        calls.append(key)
        return original(key, default)

    monkeypatch.setattr(Setting, "get_value", counting_get_value)
    assert Config.get("API_BASE_URL") == "http://db-base"
    assert Config.get("API_BASE_URL") == "http://db-base"
    assert Config.get("API_BASE_URL") == "http://db-base"
    assert calls == ["API_BASE_URL"]


def test_config_set_visible_immediately(app, db_session):
    Setting.set_value("OPENAI_MODEL", "before", None)
    assert Config.get("OPENAI_MODEL") == "before"
    Config.set("OPENAI_MODEL", "after")
    # Invalidation boundary: no TTL wait, no manual cache clear.
    assert Config.get("OPENAI_MODEL") == "after"


def test_direct_orm_write_visible_immediately_via_event_hook(app, db_session):
    Setting.set_value("MODELS", "before", None)
    assert Config.get("MODELS") == "before"
    row = db_session.get(Setting, "MODELS")
    row.value = "after"
    db_session.commit()
    # after_flush event hook invalidated the cache without any manual step.
    assert Config.get("MODELS") == "after"


def test_cache_expires_after_ttl(app, db_session, monkeypatch):
    real_monotonic = settings_service.time.monotonic
    offset = [0.0]
    monkeypatch.setattr(
        settings_service.time, "monotonic", lambda: real_monotonic() + offset[0]
    )
    Setting.set_value("API_BASE_URL", "http://first", None)
    assert Config.get("API_BASE_URL") == "http://first"

    # Mutate the DB behind the cache's back: Core SQL update produces no ORM
    # flush of Setting instances, so the invalidation hook must not fire.
    table = Setting.__tablename__
    db_session.execute(
        text(f"UPDATE {table} SET value = 'http://second' WHERE key = 'API_BASE_URL'")
    )
    db_session.commit()

    # Within the TTL the cached value persists.
    assert offset[0] < settings_service.ttl_seconds()
    assert Config.get("API_BASE_URL") == "http://first"

    # Advance past the TTL -> next get re-reads the database.
    offset[0] = settings_service.ttl_seconds() + 1.0
    assert Config.get("API_BASE_URL") == "http://second"


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


def test_secret_env_beats_stale_db_row_without_warning(
    app, db_session, monkeypatch, caplog
):
    Setting.set_value("API_TOKEN", "legacy-db-token", None)
    monkeypatch.setenv("API_TOKEN", "env-token")
    with caplog.at_level(logging.WARNING, logger="deaddit.config"):
        assert Config.get("API_TOKEN") == "env-token"
    assert "secrets-drain" not in caplog.text


def test_secret_falls_back_to_stale_db_row_with_once_warning(
    app, db_session, monkeypatch, caplog
):
    Setting.set_value("OPENAI_KEY", "sk-legacy", None)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    with caplog.at_level(logging.WARNING, logger="deaddit.config"):
        assert Config.get("OPENAI_KEY") == "sk-legacy"
        assert Config.get("OPENAI_KEY") == "sk-legacy"
        assert Config.get("OPENAI_KEY") == "sk-legacy"
    assert caplog.text.count("secrets-drain") == 1


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
    assert not is_secret_key("API_BASE_URL")


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


def test_defect2_get_all_settings_masks_every_secret(app, db_session):
    Setting.set_value("OPENAI_KEY", "sk-openai-plain", None)
    Setting.set_value("API_TOKEN", "tok-plain", None)
    Setting.set_value("API_KEY_GROQ", "gsk-plain", None)

    all_blob = json.dumps(Config.get_all_settings())
    for plaintext in ("sk-openai-plain", "tok-plain", "gsk-plain"):
        assert plaintext not in all_blob

    endpoint_blob = json.dumps(Config.get_all_endpoint_keys())
    for plaintext in ("sk-openai-plain", "tok-plain", "gsk-plain"):
        assert plaintext not in endpoint_blob
    assert '"key"' not in endpoint_blob

    keys = Config.get_all_endpoint_keys()
    groq = keys["https://api.groq.com/openai/v1"]
    assert groq["has_key"] is True
    assert groq["masked"]
    openai_entry = keys["https://api.openai.com/v1"]
    assert set(openai_entry) == {"name", "masked", "has_key"}

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


# ---------------------------------------------------------------------------
# Drain CLI round-trip
# ---------------------------------------------------------------------------


@pytest.fixture()
def env_pointed_db(monkeypatch, tmp_path):
    """DEADDIT_DB_PATH pointing at a tmp sqlite file (non-prod-shaped URI)."""
    db_file = tmp_path / "drain-target.db"
    monkeypatch.setenv("DEADDIT_DB_PATH", str(db_file))
    return db_file


@pytest.fixture()
def isolated_instance_dir(monkeypatch, tmp_path):
    """Point every Flask app's instance dir at a tmp dir during drain tests."""
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        Flask, "auto_find_instance_path", lambda self: str(instance_dir)
    )
    return instance_dir


def _seed_file_db(db_file: str, rows: dict[str, str]) -> None:
    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}"})
    with app.app_context():
        db.create_all()
        for key, value in rows.items():
            Setting.set_value(key, value, None)


def _all_output(result) -> str:
    combined = result.output
    try:
        combined += result.stderr
    except Exception:
        pass
    return combined


LEGACY_ROWS = {
    "OPENAI_KEY": "sk legacy space",
    "API_TOKEN": "tok#hash",
    "API_KEY_GROQ": "gsk_plain",
    "DEFAULT_DATA_LOADED": "true",
}


def test_drain_exports_once_and_scrubs(tmp_path, env_pointed_db):
    db_file = str(env_pointed_db)
    _seed_file_db(db_file, LEGACY_ROWS)

    runner = CliRunner()
    result = runner.invoke(cli, ["secrets-drain"])
    assert result.exit_code == 0, _all_output(result)

    out = result.output
    assert out.count("sk legacy space") == 1
    assert out.count("tok#hash") == 1
    assert "OPENAI_KEY=" in out
    assert "'sk legacy space'" in out  # shlex.quote applied
    assert "API_KEY_GROQ=gsk_plain" in out  # plain values unquoted
    assert "# DRY RUN" not in out
    summary = json.loads(out.strip().splitlines()[-1])
    assert summary == {"found": 3, "removed": 3, "dry_run": False}

    verify = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}"})
    with verify.app_context():
        remaining = {row.key: row.value for row in db.session.query(Setting).all()}
    assert "OPENAI_KEY" not in remaining
    assert "API_TOKEN" not in remaining
    assert "API_KEY_GROQ" not in remaining
    assert remaining["DEFAULT_DATA_LOADED"] == "true"

    # Idempotent by construction: a rerun finds nothing.
    rerun = runner.invoke(cli, ["secrets-drain"])
    assert rerun.exit_code == 0
    rerun_summary = json.loads(rerun.output.strip().splitlines()[-1])
    assert rerun_summary["found"] == 0
    assert rerun_summary["removed"] == 0


def test_drain_dry_run_leaves_rows_in_place(tmp_path, env_pointed_db):
    db_file = str(env_pointed_db)
    _seed_file_db(db_file, LEGACY_ROWS)

    runner = CliRunner()
    result = runner.invoke(cli, ["secrets-drain", "--dry-run"])
    assert result.exit_code == 0, _all_output(result)
    assert "# DRY RUN" in result.output
    assert result.output.count("tok#hash") == 1
    summary = json.loads(result.output.strip().splitlines()[-1])
    assert summary == {"found": 3, "removed": 0, "dry_run": True}

    verify = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}"})
    with verify.app_context():
        remaining = {row.key for row in db.session.query(Setting).all()}
    assert {
        "OPENAI_KEY",
        "API_TOKEN",
        "API_KEY_GROQ",
        "DEFAULT_DATA_LOADED",
    } <= remaining


def test_drain_refuses_prod_shape_unless_forced(tmp_path, isolated_instance_dir):
    db_file = str(isolated_instance_dir / "deaddit.db")
    _seed_file_db(db_file, LEGACY_ROWS)

    runner = CliRunner()
    # Bare create_app() resolves sqlite:///deaddit.db against the instance dir,
    # i.e. exactly the production shape.
    refused = runner.invoke(cli, ["secrets-drain"])
    assert refused.exit_code != 0
    combined = _all_output(refused)
    assert "Refusing to drain the production database" in combined
    assert "keep-me" not in combined
    assert "--i-know-this-is-prod" in combined

    verify = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}"})
    with verify.app_context():
        rows_before = {row.key: row.value for row in db.session.query(Setting).all()}
    assert rows_before["API_TOKEN"] == LEGACY_ROWS["API_TOKEN"]

    forced = runner.invoke(cli, ["secrets-drain", "--i-know-this-is-prod"])
    assert forced.exit_code == 0, _all_output(forced)
    forced_verify = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}"})
    with forced_verify.app_context():
        rows_after = {row.key: row.value for row in db.session.query(Setting).all()}
    assert "API_TOKEN" not in rows_after
    assert rows_after["DEFAULT_DATA_LOADED"] == "true"


def test_drain_cli_module_uses_lazy_create_app_binding(monkeypatch):
    """The command must call create_app through deaddit.cli's own binding."""
    assert cli_module.create_app.__module__ == "deaddit"


def test_flask_cli_exposes_secrets_drain_command(app):
    assert "secrets-drain" in app.cli.commands
