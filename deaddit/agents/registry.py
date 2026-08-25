"""Agent tool registry (slice S2).

Holds :class:`Tool` descriptors and exposes tier-filtered views plus the
OpenAI wire format via ``deaddit.llm.ToolSpec``. Importing this module has no
side effects: the default tool set is registered lazily and idempotently by
``register_default_tools()`` on first use.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from deaddit.llm import ToolSpec

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


class AutonomyTier(str, Enum):
    """Ordered agent autonomy tiers: lurker < regular < power_user."""

    LURKER = "lurker"
    REGULAR = "regular"
    POWER_USER = "power_user"

    @property
    def rank(self) -> int:
        """Position in the ordering (lurker=0 < regular=1 < power_user=2)."""
        return list(AutonomyTier).index(self)

    def allows(self, required: AutonomyTier | str) -> bool:
        """Whether this tier meets or exceeds the required tier."""
        return self.rank >= parse_tier(required).rank


def parse_tier(tier: str | AutonomyTier) -> AutonomyTier:
    """Coerce a tier value (enum or string) to :class:`AutonomyTier`."""
    if isinstance(tier, AutonomyTier):
        return tier
    try:
        return AutonomyTier(tier)
    except ValueError as exc:
        raise ValueError(f"unknown autonomy tier: {tier!r}") from exc


class RateClass(str, Enum):
    """Coarse rate-limit class of a tool."""

    READ = "read"
    WRITE = "write"
    META = "meta"


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool handler needs besides its arguments."""

    agent: Any  # deaddit.models.Agent row
    run: Any  # deaddit.models.AgentRun row
    user_username: str  # persona username (= agent.user_username)


@dataclass(frozen=True)
class Tool:
    """A single callable tool exposed to an agent."""

    name: str
    description: str
    parameters: type[BaseModel]
    handler: Callable[[ToolContext, BaseModel], dict]
    min_tier: AutonomyTier
    rate_class: RateClass


TOOL_REGISTRY: dict[str, Tool] = {}

_defaults_registered = False


def register(tool: Tool) -> None:
    """Add a tool to the registry (replaces any same-name tool)."""
    TOOL_REGISTRY[tool.name] = tool


def register_default_tools() -> None:
    """Import the built-in read/write tool modules so they self-register.

    Idempotent; called lazily by lookups so that a bare
    ``import deaddit.agents.registry`` stays side-effect free.
    """
    global _defaults_registered
    if _defaults_registered:
        return
    # Imported for their registration side effects only.
    from deaddit.agents import tools_read, tools_write  # noqa: F401

    _defaults_registered = True


def get(name: str) -> Tool:
    """Look up a registered tool by name; KeyError if missing."""
    register_default_tools()
    return TOOL_REGISTRY[name]


def all_tools() -> list[Tool]:
    """Every registered tool."""
    register_default_tools()
    return list(TOOL_REGISTRY.values())


def tools_for(tier: str | AutonomyTier) -> list[Tool]:
    """Tools whose min_tier is met or exceeded by the given tier."""
    active = parse_tier(tier)
    return [tool for tool in all_tools() if active.allows(tool.min_tier)]


def specs_for(tier: str | AutonomyTier) -> list[ToolSpec]:
    """Wire-format tool payloads for the LLM at the given tier."""
    from deaddit.llm import ToolSpec

    return [
        ToolSpec(tool.name, tool.description, tool.parameters)
        for tool in tools_for(tier)
    ]
