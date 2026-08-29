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
    "create_website",
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


def test_all_tools_covers_the_thirteen_builtins():
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
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "deaddit.agents" or name.startswith("deaddit.agents.")
    }
    for name in saved:
        monkeypatch.delitem(sys.modules, name)

    try:
        importlib.import_module("deaddit.agents")

        # Neither the registry nor anything that schedules or registers tools
        # is loaded by the bare package import.
        assert "deaddit.agents.registry" not in sys.modules
        assert "deaddit.agents.loop" not in sys.modules
        assert "deaddit.agents.tools_read" not in sys.modules
        assert "deaddit.agents.tools_write" not in sys.modules

        # Touching the lazy attribute loads only the registry module, still
        # with an empty tool registry...
        registry = importlib.import_module("deaddit.agents.registry")
        assert registry.TOOL_REGISTRY == {}

        # ...and only an actual lookup self-registers the builtins.
        registry.get("finish")
        assert set(registry.TOOL_REGISTRY) == ALL_TOOL_NAMES
    finally:
        # The fresh deaddit.agents* copies this test imported are throwaway:
        # leaving them in sys.modules would pin every later lazy re-import
        # (tools_read/tools_write especially) to the discarded registry copy,
        # leaving the restored registry's TOOL_REGISTRY permanently empty in
        # this xdist worker. Purge them so monkeypatch's restore of the
        # originals is the final word and later lookups re-register.
        for name in [
            name
            for name in list(sys.modules)
            if name == "deaddit.agents" or name.startswith("deaddit.agents.")
        ]:
            if name not in saved:
                del sys.modules[name]


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


def test_create_website_wire_spec_matches_the_argument_contract():
    """The registered create_website tool's wire schema, not just an
    internal helper, must match the spec's CreateWebsiteArgs field set."""
    specs_by_name = {spec.name: spec for spec in specs_for("regular")}
    wire = specs_by_name["create_website"].to_openai_tool()
    assert wire["type"] == "function"
    assert wire["function"]["name"] == "create_website"
    assert isinstance(wire["function"]["description"], str)
    assert "website_description" in wire["function"]["description"]

    schema = wire["function"]["parameters"]
    properties = schema["properties"]
    assert set(properties) == {
        "community",
        "title",
        "content",
        "website_description",
        "hostname_hint",
        "page_name_hint",
        "post_type",
    }
    assert properties["community"]["minLength"] == 1
    assert properties["community"]["maxLength"] == 50
    assert properties["title"]["minLength"] == 1
    assert properties["title"]["maxLength"] == 300
    assert properties["website_description"]["minLength"] == 100
    assert properties["website_description"]["maxLength"] == 12000
    assert properties["hostname_hint"]["minLength"] == 3
    assert properties["hostname_hint"]["maxLength"] == 253
    assert properties["page_name_hint"]["minLength"] == 1
    assert properties["page_name_hint"]["maxLength"] == 120
    assert set(schema["required"]) == {
        "community",
        "title",
        "website_description",
        "hostname_hint",
        "page_name_hint",
    }


def test_create_website_is_regular_tier_gated_like_the_other_post_tools():
    lurker_names = {tool.name for tool in tools_for("lurker")}
    regular_names = {tool.name for tool in tools_for("regular")}
    assert "create_website" not in lurker_names
    assert "create_website" in regular_names


# ---------------------------------------------------------------------------
# Website-post gating (create_website spec, Phase 3.1): website_posts_config
# mirrors image_posts_config, and a single filtering function evaluates both
# namespaced configs together against the truth table in
# CREATE_WEBSITE_TOOL_PLAN.md ("Agent configuration").


def test_website_posts_config_defaults_to_disabled_shape():
    from deaddit.agents.registry import (
        DISABLED_WEBSITE_POSTS_CONFIG,
        website_posts_config,
    )

    assert website_posts_config(_StubAgent({})) == DISABLED_WEBSITE_POSTS_CONFIG
    assert website_posts_config(_StubAgent(None)) == DISABLED_WEBSITE_POSTS_CONFIG
    assert website_posts_config(_StubAgent({"website_posts": "nonsense"})) == (
        DISABLED_WEBSITE_POSTS_CONFIG
    )
    assert website_posts_config(_StubAgent({"website_posts": {}})) == (
        DISABLED_WEBSITE_POSTS_CONFIG
    )
    assert (
        website_posts_config(
            _StubAgent({"website_posts": {"enabled": False, "policy": "website_only"}})
        )
        == DISABLED_WEBSITE_POSTS_CONFIG
    )


def test_website_posts_config_normalizes_invalid_policy_to_optional():
    from deaddit.agents.registry import website_posts_config

    agent = _StubAgent({"website_posts": {"enabled": True, "policy": "bogus"}})
    assert website_posts_config(agent) == {"enabled": True, "policy": "optional"}

    agent = _StubAgent({"website_posts": {"enabled": True, "policy": 123}})
    assert website_posts_config(agent) == {"enabled": True, "policy": "optional"}

    agent = _StubAgent({"website_posts": {"enabled": True}})
    assert website_posts_config(agent) == {"enabled": True, "policy": "optional"}


def test_website_posts_config_accepts_website_only():
    from deaddit.agents.registry import website_posts_config

    agent = _StubAgent({"website_posts": {"enabled": True, "policy": "website_only"}})
    assert website_posts_config(agent) == {"enabled": True, "policy": "website_only"}


def _image_cfg(enabled=False, policy="optional", provider_id=1):
    if not enabled:
        return {}
    return {
        "image_posts": {"enabled": True, "provider_id": provider_id, "policy": policy}
    }


def _website_cfg(enabled=False, policy="optional"):
    if not enabled:
        return {}
    return {"website_posts": {"enabled": True, "policy": policy}}


def _offered(image_kwargs, website_kwargs):
    """Offered post-tool names from the *actual wire specs* for a config."""
    config = {**_image_cfg(**image_kwargs), **_website_cfg(**website_kwargs)}
    agent = _StubAgent(config)
    tools_names = {tool.name for tool in tools_for("regular", agent=agent)}
    spec_names = {spec.name for spec in specs_for("regular", agent=agent)}
    # tools_for and specs_for must agree on exactly which post tools are
    # offered - checking both closes the gap where a helper could look right
    # while the wire payload the LLM actually receives differs.
    post_tool_names = {"create_post", "create_image_post", "create_website"}
    assert tools_names & post_tool_names == spec_names & post_tool_names
    return spec_names & post_tool_names


def test_truth_table_disabled_disabled_offers_only_create_post():
    assert _offered({"enabled": False}, {"enabled": False}) == {"create_post"}


def test_truth_table_image_optional_website_disabled():
    offered = _offered({"enabled": True, "policy": "optional"}, {"enabled": False})
    assert offered == {"create_post", "create_image_post"}


def test_truth_table_image_disabled_website_optional():
    offered = _offered({"enabled": False}, {"enabled": True, "policy": "optional"})
    assert offered == {"create_post", "create_website"}


def test_truth_table_both_optional_offers_all_three():
    offered = _offered(
        {"enabled": True, "policy": "optional"},
        {"enabled": True, "policy": "optional"},
    )
    assert offered == {"create_post", "create_image_post", "create_website"}


def test_truth_table_image_only_website_disabled_offers_only_image():
    offered = _offered({"enabled": True, "policy": "image_only"}, {"enabled": False})
    assert offered == {"create_image_post"}


def test_truth_table_image_only_website_optional_offers_only_image():
    offered = _offered(
        {"enabled": True, "policy": "image_only"},
        {"enabled": True, "policy": "optional"},
    )
    assert offered == {"create_image_post"}


def test_truth_table_website_only_image_disabled_offers_only_website():
    offered = _offered({"enabled": False}, {"enabled": True, "policy": "website_only"})
    assert offered == {"create_website"}


def test_truth_table_website_only_image_optional_offers_only_website():
    offered = _offered(
        {"enabled": True, "policy": "optional"},
        {"enabled": True, "policy": "website_only"},
    )
    assert offered == {"create_website"}


def test_truth_table_invalid_image_only_website_only_fails_closed_to_neither():
    """image_only and website_only are mutually exclusive locks; a stored
    config with both (invalid - 3.4's job to reject at admin time) must not
    grant either lock's tool, and must not fall back to create_post either,
    since both policies explicitly forbid plain text posts."""
    offered = _offered(
        {"enabled": True, "policy": "image_only"},
        {"enabled": True, "policy": "website_only"},
    )
    assert offered == set()

    # And to be doubly sure the wire specs actually carry no post tool at
    # all in this state (not just that the set difference is empty):
    agent = _StubAgent(
        {
            "image_posts": {"enabled": True, "provider_id": 1, "policy": "image_only"},
            "website_posts": {"enabled": True, "policy": "website_only"},
        }
    )
    spec_names = {spec.name for spec in specs_for("regular", agent=agent)}
    assert "create_post" not in spec_names
    assert "create_image_post" not in spec_names
    assert "create_website" not in spec_names
    # Every other tool is unaffected by the conflict.
    assert spec_names == ALL_TOOL_NAMES - {
        "create_post",
        "create_image_post",
        "create_website",
    }
