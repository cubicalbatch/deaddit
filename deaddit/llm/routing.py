"""Model routing: resolve ``(api_url, model_name)`` per generation request.

This module replaces loader.py's old module-level model list (the source of
a stale-state bug: in-place list mutations leaked between commands until
process restart). Every :func:`resolve` call reads Config and the
:class:`~deaddit.models.ModelRoute` table fresh, so route changes take effect
on the very next call — no restart, no caching.

Precedence chain:

1. CLI/process override (:func:`set_models_override`)
2. Active :class:`ModelRoute` for the persona's tier (highest priority wins)
3. Active ``ModelRoute`` for tier ``'default'``
4. ``ApiEndpointConfig.get_default_model_for_endpoint`` for the configured URL
5. ``Config.OPENAI_MODEL`` (``'llama3'`` fallback)
"""

from __future__ import annotations

import logging

from deaddit.config import Config
from deaddit.extensions import db
from deaddit.models import ApiEndpointConfig, ModelRoute

logger = logging.getLogger(__name__)

_CREATIVE_TRAITS = ("creative", "artistic", "imaginative", "expressive")
_ANALYTICAL_TRAITS = ("analytical", "logical", "methodical", "systematic")

# Process-local state only — never a cache of DB contents. `_override` stands
# in for loader.py's retired model-list global (CLI --model flags); `_routes_checked` gates
# the cheap ensure_seeded() existence query and is reset by every helper that
# mutates the routes table below.
_override: list[str] | None = None
_routes_checked = False


def persona_tier(user_persona: str | None) -> str:
    """Map a persona string to a routing tier ('creative'|'analytical'|'default')."""
    if not user_persona:
        return "default"
    persona_lower = user_persona.lower()
    if any(trait in persona_lower for trait in _CREATIVE_TRAITS):
        return "creative"
    if any(trait in persona_lower for trait in _ANALYTICAL_TRAITS):
        return "analytical"
    return "default"


def set_models_override(models: list[str] | None) -> None:
    """Install a process-wide model override (CLI --model); ``None`` clears it.

    With an override installed, :func:`resolve` returns the first entry against
    the configured ``OPENAI_API_URL``, bypassing DB routes entirely.
    """
    global _override
    cleaned = [m.strip() for m in (models or []) if m and m.strip()]
    _override = cleaned or None


def set_route(
    tier: str, model_name: str, api_url: str | None = None, priority: int = 0
) -> ModelRoute:
    """Create or update the active route for ``tier``. Resets the seed gate."""
    global _routes_checked
    route = (
        ModelRoute.query.filter_by(tier=tier)
        .order_by(ModelRoute.priority.desc(), ModelRoute.id.desc())
        .first()
    )
    if route:
        route.model_name = model_name
        route.api_url = api_url
        route.priority = priority
        route.is_active = True
    else:
        route = ModelRoute(
            tier=tier, model_name=model_name, api_url=api_url, priority=priority
        )
        db.session.add(route)
    db.session.commit()
    _routes_checked = False
    return route


def clear_routes() -> None:
    """Delete every route row (admin reset / test isolation)."""
    global _routes_checked
    ModelRoute.query.delete()
    db.session.commit()
    _routes_checked = False


def ensure_seeded() -> None:
    """Guarantee an active 'default'-tier route exists, seeded from Config.

    Called lazily from :func:`resolve`; the process-local ``_routes_checked``
    flag keeps the existence query to one per routes-table generation.
    """
    global _routes_checked
    if _routes_checked:
        return
    if not ModelRoute.query.filter_by(tier="default", is_active=True).first():
        db.session.add(
            ModelRoute(
                tier="default",
                model_name=Config.get("OPENAI_MODEL", "llama3"),
                api_url=Config.get("OPENAI_API_URL", "http://localhost/v1"),
            )
        )
        db.session.commit()
        logger.info("Seeded default model route from OPENAI_MODEL config")
    _routes_checked = True


def resolve(user_persona: str | None = None) -> tuple[str, str]:
    """Resolve ``(api_url, model_name)`` for a request, reading state fresh."""
    default_api_url = Config.get("OPENAI_API_URL", "http://localhost/v1")

    # 1. CLI/process override wins outright.
    if _override:
        return default_api_url, _override[0]

    tier = persona_tier(user_persona)
    ensure_seeded()

    # 2./3. Persona-tier route first, then the default-tier route.
    for candidate_tier in dict.fromkeys((tier, "default")):
        route = (
            ModelRoute.query.filter_by(tier=candidate_tier, is_active=True)
            .order_by(ModelRoute.priority.desc(), ModelRoute.id.desc())
            .first()
        )
        if route:
            return route.api_url or default_api_url, route.model_name

    # 4. Per-endpoint configured default model.
    endpoint_model = ApiEndpointConfig.get_default_model_for_endpoint(default_api_url)
    if endpoint_model:
        return default_api_url, endpoint_model

    # 5. Global config fallback.
    return default_api_url, Config.get("OPENAI_MODEL", "llama3")
