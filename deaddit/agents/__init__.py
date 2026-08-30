"""Fresh ``deaddit.agents`` package (AgenticCore slice S2).

Lazy re-export of the tool registry API. Importing this package is cheap and
side-effect free: neither the tool modules nor the LLM client are loaded
until a registry lookup (or explicit import) needs them.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "BACKSTAGE_SUBDEADDIT_NAME",
    "DISABLED_IMAGE_POSTS_CONFIG",
    "DISABLED_WEBSITE_POSTS_CONFIG",
    "POST_TOOL_NAMES",
    "TOOL_REGISTRY",
    "AutonomyTier",
    "RateClass",
    "Tool",
    "ToolContext",
    "all_tools",
    "effective_post_configs",
    "get",
    "image_posts_config",
    "offered_post_tool_names",
    "register",
    "specs_for",
    "subscribe_nudge",
    "tools_for",
    "website_posts_config",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from deaddit.agents import registry

        return getattr(registry, name)
    try:
        import importlib

        return importlib.import_module(f"deaddit.agents.{name}")
    except ImportError:
        pass
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
