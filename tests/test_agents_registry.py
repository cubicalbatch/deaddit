"""Deterministic coverage for the agent tool registry and runtime flag."""

from __future__ import annotations

import importlib
import sys

from deaddit.agents.registry import (
    TOOL_REGISTRY,
    all_tools,
    parse_tier,
    specs_for,
    tools_for,
)

ALL_TOOL_NAMES = {
    "browse_feed",
    "read_post",
    "search",
    "view_inbox",
    "view_profile",
    "vote",
    "create_post",
    "create_comment",
    "subscribe",
    "unsubscribe",
    "finish",
}

READ_ONLY_NAMES = {
    "browse_feed",
    "read_post",
    "search",
    "view_inbox",
    "view_profile",
    "vote",
    "finish",
}


# ---------------------------------------------------------------------------
# Tier filtering


def test_all_tools_covers_the_eleven_builtins():
    assert {tool.name for tool in all_tools()} == ALL_TOOL_NAMES


def test_lurker_tier_excludes_write_tools():
    names = {tool.name for tool in tools_for("lurker")}
    assert names == READ_ONLY_NAMES
    assert "create_post" not in names
    assert "create_comment" not in names
    assert "subscribe" not in names
    assert "unsubscribe" not in names


def test_regular_tier_includes_everything():
    names = {tool.name for tool in tools_for("regular")}
    assert names == ALL_TOOL_NAMES


def test_specs_for_produces_openai_function_shapes():
    specs_by_name = {spec.name: spec for spec in specs_for("regular")}
    assert set(specs_by_name) == ALL_TOOL_NAMES
    wire = specs_by_name["create_post"].to_openai_tool()
    assert wire["type"] == "function"
    assert wire["function"]["name"] == "create_post"
    assert isinstance(wire["function"]["description"], str)
    # Parameter schemas come straight from the pydantic argument models.
    schema = wire["function"]["parameters"]
    properties = schema["properties"]
    assert properties["title"]["maxLength"] == 300
    assert properties["title"]["minLength"] == 1
    assert "title" in schema["required"]
    assert properties["content"]["maxLength"] == 20000


def test_parse_tier_rejects_unknown_values():
    try:
        parse_tier("admin")
    except ValueError as exc:
        assert "unknown autonomy tier" in str(exc)
    else:
        raise AssertionError("parse_tier accepted an unknown tier")


# ---------------------------------------------------------------------------
# Import hygiene: the package is lazy and registers nothing until lookup


def test_package_import_is_side_effect_free(monkeypatch):
    # Purge every previously loaded deaddit.agents* module so this test sees
    # a genuinely fresh package import, wherever it runs in the session.
    for name in [
        name
        for name in list(sys.modules)
        if name == "deaddit.agents" or name.startswith("deaddit.agents.")
    ]:
        monkeypatch.delitem(sys.modules, name)

    importlib.import_module("deaddit.agents")

    # Neither the registry nor anything that schedules or registers tools is
    # loaded by the bare package import.
    assert "deaddit.agents.registry" not in sys.modules
    assert "deaddit.agents.loop" not in sys.modules
    assert "deaddit.agents.tools_read" not in sys.modules
    assert "deaddit.agents.tools_write" not in sys.modules

    # Touching the lazy attribute loads only the registry module, still with
    # an empty tool registry...
    registry = importlib.import_module("deaddit.agents.registry")
    assert registry.TOOL_REGISTRY == {}

    # ...and only an actual lookup self-registers the builtins.
    registry.get("finish")
    assert set(registry.TOOL_REGISTRY) == ALL_TOOL_NAMES


# ---------------------------------------------------------------------------
# Feature flag


def test_runtime_flag_defaults_to_false(app):
    from deaddit.agents.loop import is_runtime_enabled

    assert is_runtime_enabled() is False


def test_runtime_flag_follows_setting_row(app, db_session):
    from deaddit.agents.loop import is_runtime_enabled
    from deaddit.models import Setting

    Setting.set_value("AGENT_RUNTIME_ENABLED", "true")
    assert is_runtime_enabled() is True

    Setting.set_value("AGENT_RUNTIME_ENABLED", "false")
    assert is_runtime_enabled() is False


def test_registry_module_exposes_expected_api():
    import deaddit.agents as agents_pkg

    for name in agents_pkg.__all__:
        assert getattr(agents_pkg, name) is not None or name == "TOOL_REGISTRY"
    assert isinstance(TOOL_REGISTRY, dict)
