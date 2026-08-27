"""Phase LLM-3: model routing resolution (deaddit.llm.routing).

Covers the precedence chain (CLI/process override > tier route > default-tier
route > ApiEndpointConfig > OPENAI_MODEL), persona_tier mapping, route
changes taking effect on the very next generation call (the stale-global-bug
regression), and ensure_seeded() creating a default route from Config.
"""

from __future__ import annotations

import pytest

from deaddit.config import Config
from deaddit.llm import routing
from deaddit.models import ApiEndpointConfig, ModelRoute

DEFAULT_URL = "http://localhost/v1"


@pytest.fixture(autouse=True)
def _reset_routing_state():
    """Routing keeps process-local override/seed-gate state; isolate tests."""
    routing._override = None
    routing._routes_checked = False
    yield
    routing._override = None
    routing._routes_checked = False


# ---------------------------------------------------------------------------
# persona_tier


def test_persona_tier_creative():
    assert routing.persona_tier("creative and artistic") == "creative"
    assert routing.persona_tier("an imaginative storyteller") == "creative"
    assert routing.persona_tier("highly EXPRESSIVE") == "creative"


def test_persona_tier_analytical():
    assert routing.persona_tier("logical and methodical") == "analytical"
    assert routing.persona_tier("systematic thinker") == "analytical"


def test_persona_tier_default():
    assert routing.persona_tier(None) == "default"
    assert routing.persona_tier("") == "default"
    assert routing.persona_tier("friendly and outgoing") == "default"


# ---------------------------------------------------------------------------
# ensure_seeded


def test_ensure_seeded_creates_default_route_from_config(app, db_session):
    assert ModelRoute.query.count() == 0

    api_url, model_name = routing.resolve()

    seeded = ModelRoute.query.filter_by(tier="default").one()
    assert seeded.is_active is True
    assert seeded.model_name == Config.get("OPENAI_MODEL", "llama3")
    assert (api_url, model_name) == (DEFAULT_URL, "llama3")


def test_ensure_seeded_runs_once_per_routes_generation(app, db_session):
    routing.resolve()
    routing.resolve()
    routing.resolve()
    # Seeding must not duplicate rows across repeated resolves.
    assert ModelRoute.query.filter_by(tier="default").count() == 1


def test_set_route_resets_seed_gate(app, db_session, monkeypatch):
    routing.resolve()
    assert routing._routes_checked is True

    monkeypatch.setattr(
        routing, "ensure_seeded", lambda: pytest.fail("must not re-seed")
    )
    routing.set_route("creative", "route-model")
    # set_route invalidated the gate; resolve() may hit ensure_seeded again.
    routing.set_models_override(["cli-model"])
    assert routing.resolve()[1] == "cli-model"


# ---------------------------------------------------------------------------
# precedence chain


def test_override_beats_everything(app, db_session):
    routing.set_route("creative", "route-model")
    routing.set_models_override(["cli-model", "cli-model-2"])

    assert routing.resolve("very creative persona") == (DEFAULT_URL, "cli-model")
    assert routing.resolve(None) == (DEFAULT_URL, "cli-model")


def test_set_models_override_none_clears(app, db_session):
    routing.set_models_override(["cli-model"])
    routing.set_models_override(None)

    _, model_name = routing.resolve()
    assert model_name == Config.get("OPENAI_MODEL", "llama3")


def test_tier_route_wins_over_default_route(app, db_session):
    routing.set_route("default", "default-route-model")
    routing.set_route("creative", "creative-route-model")

    assert routing.resolve("creative persona") == (
        DEFAULT_URL,
        "creative-route-model",
    )
    # A persona with no dedicated tier falls back to the default-tier route.
    assert routing.resolve("friendly") == (DEFAULT_URL, "default-route-model")


def test_set_route_and_clear_routes_reset_seed_gate(app, db_session):
    routing.resolve()
    assert routing._routes_checked is True

    routing.set_route("creative", "route-model")
    assert routing._routes_checked is False

    routing.resolve()
    assert routing._routes_checked is True

    routing.clear_routes()
    assert routing._routes_checked is False


def test_inactive_route_is_ignored(app, db_session):
    db_session.add(ModelRoute(tier="default", model_name="inactive", is_active=False))
    db_session.commit()
    routing._routes_checked = True

    # Falls through to OPENAI_MODEL because no active default route exists
    # and seeding was suppressed for this scenario.
    assert routing.resolve()[1] == "llama3"


def test_route_null_api_url_falls_back_to_config(app, db_session):
    db_session.add(ModelRoute(tier="default", model_name="m", api_url=None))
    db_session.commit()
    routing._routes_checked = True

    assert routing.resolve()[0] == DEFAULT_URL


def test_endpoint_config_falls_back_to_openai_model(app, db_session):
    # Suppress seeding so steps 4/5 of the chain are reachable.
    routing._routes_checked = True
    db_session.query(ModelRoute).delete()

    db_session.add(
        ApiEndpointConfig(api_url=DEFAULT_URL, default_model="endpoint-model")
    )
    db_session.commit()
    assert routing.resolve()[1] == "endpoint-model"

    db_session.query(ApiEndpointConfig).delete()
    db_session.commit()
    assert routing.resolve() == (DEFAULT_URL, "llama3")


# ---------------------------------------------------------------------------
# live reroute: no restart, no caching


def test_route_change_applies_on_next_resolution(app, db_session):
    """Route changes take effect on the very next resolve() — no restart."""
    routing.set_route("creative", "model-a")
    assert routing.resolve("creative")[1] == "model-a"

    routing.set_route("creative", "model-b")

    assert routing.resolve("creative")[1] == "model-b"
