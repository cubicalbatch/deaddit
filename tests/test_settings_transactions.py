"""Transaction-boundary invalidation for the settings cache."""

from __future__ import annotations

import pytest

import deaddit.settings.service as settings_service
from deaddit.config import Config
from deaddit.models import Setting


@pytest.fixture(autouse=True)
def _fresh_settings_cache():
    settings_service.clear()
    yield
    settings_service.clear()


def test_flush_read_rollback_drops_uncommitted_setting_cache(app, db_session):
    key = "AGENT_RUNTIME_ENABLED"
    Config.set(key, "false")
    assert Config.get(key) == "false"

    row = db_session.get(Setting, key)
    row.value = "true"
    db_session.flush()
    assert Config.get(key) == "true"

    db_session.rollback()
    assert Setting.get_value(key) == "false"
    assert Config.get(key) == "false"


def test_inserted_setting_cache_is_invalidated_on_rollback(app, db_session):
    key = "TRANSACTION_INSERTED_SETTING"
    assert Setting.get_value(key) is None
    assert Config.get(key) is None

    db_session.add(Setting(key=key, value="true"))
    db_session.flush()
    assert Config.get(key) == "true"

    db_session.rollback()
    assert Setting.get_value(key) is None
    assert Config.get(key) is None


def test_deleted_setting_cache_is_invalidated_on_rollback(app, db_session):
    key = "TRANSACTION_DELETED_SETTING"
    Config.set(key, "before")
    assert Config.get(key) == "before"

    db_session.delete(db_session.get(Setting, key))
    db_session.flush()
    assert Config.get(key) is None

    db_session.rollback()
    assert Setting.get_value(key) == "before"
    assert Config.get(key) == "before"


def test_savepoint_rollback_invalidates_nested_setting_cache(app, db_session):
    key = "TRANSACTION_SAVEPOINT_SETTING"
    Config.set(key, "initial")
    row = db_session.get(Setting, key)
    row.value = "outer"
    db_session.flush()
    assert Config.get(key) == "outer"

    nested = db_session.begin_nested()
    row.value = "nested"
    db_session.flush()
    assert Config.get(key) == "nested"

    nested.rollback()
    assert Setting.get_value(key) == "outer"
    assert Config.get(key) == "outer"
    db_session.rollback()
    assert Setting.get_value(key) == "initial"
    assert Config.get(key) == "initial"
