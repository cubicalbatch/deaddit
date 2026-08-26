"""Route-map equality acceptance for Phase A1.

Compares the url_map produced by create_app() against the pre-refactor
baseline captured in tests/a1_route_map_baseline.json (82 routes).

Endpoint-name normalization: the baseline uses pre-blueprint names
("index", "subdeaddit", "available_models", "admin.dashboard"). After A1
the same view functions live on blueprints, so their endpoint names gain a
blueprint prefix ("web.index", "api.available_models", "admin.dashboard"). The
comparison strips a single leading known-blueprint prefix ("api.", "web.",
"admin.") from the actual endpoint name before comparing to the baseline.

Comparison shape: exact multiset equality of (rule, frozenset(methods
minus auto-added HEAD/OPTIONS), normalized endpoint) triples. A multiset,
not a rule-keyed mapping, is required because four admin rules are served
by two view functions each (PUT and DELETE on the same rule); Flask keeps
them as distinct Rule entries and the baseline lists them twice. Route
count equality follows from multiset equality but is asserted separately
for a clearer failure message.
"""

import json
from pathlib import Path

from deaddit import create_app

BASELINE = Path(__file__).parent / "a1_route_map_baseline.json"

KNOWN_PREFIXES = ("admin.", "web.", "api.")


def _normalize(endpoint: str) -> str:
    for prefix in KNOWN_PREFIXES:
        if endpoint.startswith(prefix) and endpoint != prefix.rstrip("."):
            return endpoint[len(prefix):]
    return endpoint


def _actual_routes():
    app = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite://"})
    return [
        (r.rule, frozenset(r.methods - {"HEAD", "OPTIONS"}), _normalize(r.endpoint))
        for r in app.url_map.iter_rules()
    ]


def _expected_routes():
    return [
        (
            entry["rule"],
            frozenset(entry["methods"]),
            _normalize(entry["endpoint"]),
        )
        for entry in json.loads(BASELINE.read_text())
    ]


def test_route_count_matches_baseline():
    assert len(_actual_routes()) == len(_expected_routes()), (
        f"route count {len(_actual_routes())} != baseline "
        f"{len(_expected_routes())}"
    )


def test_rules_and_methods_match_exactly():
    actual = sorted(_actual_routes())
    expected = sorted(_expected_routes())
    assert actual == expected

