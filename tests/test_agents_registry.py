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
    "create_image_post",
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


def test_all_tools_covers_the_twelve_builtins():
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


# ---------------------------------------------------------------------------
# Image-post gating (plan 4B): tools_for/specs_for take an optional agent and
# filter create_post/create_image_post by Agent.config["image_posts"].


class _StubAgent:
    def __init__(self, config):
        self.config = config


def test_image_posts_absent_key_omits_image_tool_offers_text():
    agent = _StubAgent({})
    names = {tool.name for tool in tools_for("regular", agent=agent)}
    assert "create_image_post" not in names
    assert "create_post" in names


def test_image_posts_disabled_flag_omits_image_tool_offers_text():
    agent = _StubAgent({"image_posts": {"enabled": False}})
    names = {tool.name for tool in tools_for("regular", agent=agent)}
    assert "create_image_post" not in names
    assert "create_post" in names


def test_image_posts_optional_offers_both_tools():
    agent = _StubAgent(
        {"image_posts": {"enabled": True, "provider_id": 1, "policy": "optional"}}
    )
    names = {tool.name for tool in tools_for("regular", agent=agent)}
    assert "create_image_post" in names
    assert "create_post" in names


def test_image_posts_image_only_omits_text_offers_image():
    agent = _StubAgent(
        {"image_posts": {"enabled": True, "provider_id": 1, "policy": "image_only"}}
    )
    names = {tool.name for tool in tools_for("regular", agent=agent)}
    assert "create_image_post" in names
    assert "create_post" not in names


def test_specs_for_applies_the_same_gating_as_tools_for():
    agent = _StubAgent(
        {"image_posts": {"enabled": True, "provider_id": 1, "policy": "image_only"}}
    )
    names = {spec.name for spec in specs_for("regular", agent=agent)}
    assert "create_image_post" in names
    assert "create_post" not in names


def test_image_posts_config_normalizes_invalid_policy_to_optional():
    from deaddit.agents.registry import image_posts_config

    agent = _StubAgent(
        {"image_posts": {"enabled": True, "provider_id": 1, "policy": "bogus"}}
    )
    cfg = image_posts_config(agent)
    assert cfg == {
        "enabled": True,
        "policy": "optional",
        "provider_id": 1,
        "model": None,
    }


def test_image_posts_config_defaults_to_disabled_shape():
    from deaddit.agents.registry import DISABLED_IMAGE_POSTS_CONFIG, image_posts_config

    assert image_posts_config(_StubAgent({})) == DISABLED_IMAGE_POSTS_CONFIG
    assert image_posts_config(_StubAgent(None)) == DISABLED_IMAGE_POSTS_CONFIG
    assert image_posts_config(_StubAgent({"image_posts": "nonsense"})) == (
        DISABLED_IMAGE_POSTS_CONFIG
    )
