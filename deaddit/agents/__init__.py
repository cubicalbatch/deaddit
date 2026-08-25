"""Fresh ``deaddit.agents`` package (AgenticCore slice S2).

Lazy re-export of the tool registry API. Importing this package is cheap and
side-effect free: neither the tool modules nor the LLM client are loaded
until a registry lookup (or explicit import) needs them.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "TOOL_REGISTRY",
    "AutonomyTier",
    "RateClass",
    "Tool",
    "ToolContext",
    "all_tools",
    "get",
    "register",
    "specs_for",
    "tools_for",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from deaddit.agents import registry

        return getattr(registry, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
