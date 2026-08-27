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
    "DISABLED_IMAGE_POSTS_CONFIG",
    "POST_TOOL_NAMES",
    "TOOL_REGISTRY",
    "AutonomyTier",
    "RateClass",
    "Tool",
    "ToolContext",
    "all_tools",
    "get",
    "image_posts_config",
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
    """Everything a tool handler needs besides its arguments.

    ``llm_api_url``/``llm_api_key``/``llm_model`` mirror the effective LLM
    configuration the run resolved before its first request (plan 4B: needed
    by later vision-description reads); they live only on this in-memory
    object and are never written to ``ToolCall`` or any other persisted row.
    ``deadline`` is the run's overall wall-clock budget as a
    :class:`~deaddit.images.types.Deadline` (or ``None`` outside a real run);
    handlers that spend real time - image generation chief among them - read
    ``deadline.remaining()`` to bound their own work instead of overrunning
    the run.
    """

    agent: Any  # deaddit.models.Agent row
    run: Any  # deaddit.models.AgentRun row
    user_username: str  # persona username (= agent.user_username)
    llm_api_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    deadline: Any = None  # deaddit.images.types.Deadline | None


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


#: The two tools that publish a post. Both draw from the same per-run/hourly
#: post budget and the same duplicate/loop guardrails (plan 4B) - an
#: image-post failure must never leave a text post as an unthrottled
#: fallback, and vice versa.
POST_TOOL_NAMES: tuple[str, ...] = ("create_post", "create_image_post")

#: Canonical shape of a disabled/absent ``Agent.config["image_posts"]``.
DISABLED_IMAGE_POSTS_CONFIG: dict[str, Any] = {
    "enabled": False,
    "policy": "optional",
    "provider_id": None,
    "model": None,
}


def image_posts_config(agent: Any) -> dict[str, Any]:
    """Normalize *agent*'s namespaced image-post configuration.

    Missing ``image_posts``, a non-dict value, or ``enabled: false`` all
    normalize to :data:`DISABLED_IMAGE_POSTS_CONFIG`. An enabled config with
    a missing/invalid ``policy`` defaults to ``"optional"`` here rather than
    being rejected - admin-side validation (3B) is what keeps stored config
    well-formed; this function only has to be safe to call on anything that
    might be sitting in the database.
    """
    config = getattr(agent, "config", None)
    raw = config.get("image_posts") if isinstance(config, dict) else None
    if not isinstance(raw, dict) or not raw.get("enabled"):
        return dict(DISABLED_IMAGE_POSTS_CONFIG)
    policy = raw.get("policy")
    if policy not in ("optional", "image_only"):
        policy = "optional"
    return {
        "enabled": True,
        "policy": policy,
        "provider_id": raw.get("provider_id"),
        "model": raw.get("model"),
    }


def _offer_post_tool(name: str, cfg: dict[str, Any]) -> bool:
    """Whether *name* (one of :data:`POST_TOOL_NAMES`) should be offered."""
    if name == "create_image_post":
        return cfg["enabled"]
    if name == "create_post":
        return not (cfg["enabled"] and cfg["policy"] == "image_only")
    return True


def tools_for(tier: str | AutonomyTier, agent: Any = None) -> list[Tool]:
    """Tools whose min_tier is met or exceeded by the given tier.

    When *agent* is given, ``create_post``/``create_image_post`` are also
    filtered by its namespaced ``image_posts`` configuration: disabled omits
    ``create_image_post``, and the ``image_only`` policy omits ``create_post``
    (plan 4B). Omitting *agent* skips this filter entirely (tier-only
    behaviour), which non-agent-aware callers rely on.
    """
    active = parse_tier(tier)
    tools = [tool for tool in all_tools() if active.allows(tool.min_tier)]
    if agent is None:
        return tools
    cfg = image_posts_config(agent)
    return [
        tool
        for tool in tools
        if tool.name not in POST_TOOL_NAMES or _offer_post_tool(tool.name, cfg)
    ]


def specs_for(tier: str | AutonomyTier, agent: Any = None) -> list[ToolSpec]:
    """Wire-format tool payloads for the LLM at the given tier (see tools_for)."""
    from deaddit.llm import ToolSpec

    return [
        ToolSpec(tool.name, tool.description, tool.parameters)
        for tool in tools_for(tier, agent=agent)
    ]
