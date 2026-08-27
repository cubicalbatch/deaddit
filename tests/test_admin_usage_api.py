"""Phase LLM-3: admin JSON API for LLM usage accounting and model routing.

Covers the two read-only, admin-protected endpoints added to
deaddit/admin.py:

- GET /admin/api/usage/summary — SQL aggregates over ``LLMUsage``:
  totals (null-safe cost sum), by_day and by_action breakdowns.
- GET /admin/api/routes — ModelRoute rows plus the currently resolved
  default (api_url, model_name) for context.

The summary tests exercise both paths: ORM-seeded rows for aggregate
shape/null-safety (independent of ledger wiring) and real FakeProvider
generations for end-to-end nonzero totals.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from deaddit.config import Config
from deaddit.llm.client import ChatRequest, LLMClient
from deaddit.models import LLMUsage, ModelRoute

API_URL = "http://localhost:11434/v1"
MODEL = "llama3"


def _seed_usage(db_session, **overrides):
    row = LLMUsage(
        created_at=overrides.pop("created_at", datetime.utcnow()),
        request_id=overrides.pop("request_id", "req0000000000000001"),
        attempt=overrides.pop("attempt", 1),
        api_url=API_URL,
        model=MODEL,
        status="ok",
        **overrides,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _get_json(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


# ---------------------------------------------------------------------------
# /admin/api/usage/summary — aggregates over seeded rows
# ---------------------------------------------------------------------------


def test_summary_totals_and_breakdowns_from_seeded_rows(app, client, db_session):
    today = datetime(2026, 8, 24, 12, 0, 0)
    yesterday = today - timedelta(days=1)
    _seed_usage(
        db_session,
        created_at=today,
        action="post",
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        estimated_cost=0.5,
    )
    _seed_usage(
        db_session,
        created_at=today,
        request_id="req0000000000000002",
        action="post",
        prompt_tokens=1,
        completion_tokens=2,
        total_tokens=3,
        # Unknown price: contributes tokens but NOT cost.
        estimated_cost=None,
    )
    _seed_usage(
        db_session,
        created_at=yesterday,
        request_id="req0000000000000003",
        action="comment",
        total_tokens=7,
        estimated_cost=0.0,  # local/free endpoint: exactly zero
    )

    data = _get_json(client, "/admin/api/usage/summary")

    totals = data["totals"]
    assert totals["rows"] == 3
    assert totals["prompt_tokens"] == 11
    assert totals["completion_tokens"] == 22
    assert totals["total_tokens"] == 40
    # Null-safe: unknown-price row never counts as $0; free row stays 0.0.
    assert totals["estimated_cost_sum"] == 0.5 + 0.0

    by_day = {row["day"]: row for row in data["by_day"]}
    assert set(by_day) == {"2026-08-23", "2026-08-24"}
    assert by_day["2026-08-24"]["tokens"] == 33
    assert by_day["2026-08-24"]["cost"] == 0.5
    assert by_day["2026-08-23"]["tokens"] == 7
    assert by_day["2026-08-23"]["cost"] == 0.0

    by_action = {row["action"]: row for row in data["by_action"]}
    assert set(by_action) == {"post", "comment"}
    assert by_action["post"]["rows"] == 2
    assert by_action["post"]["tokens"] == 33
    assert by_action["comment"]["tokens"] == 7


def test_summary_all_unknown_costs_stay_null_not_zero(app, client, db_session):
    """An unpriced ledger must not masquerade as a free one."""
    _seed_usage(
        db_session,
        prompt_tokens=5,
        completion_tokens=5,
        total_tokens=10,
        estimated_cost=None,
    )

    data = _get_json(client, "/admin/api/usage/summary")

    assert data["totals"]["rows"] == 1
    assert data["totals"]["estimated_cost_sum"] is None


def test_summary_empty_ledger(app, client):
    data = _get_json(client, "/admin/api/usage/summary")

    assert data["totals"]["rows"] == 0
    assert data["totals"]["total_tokens"] == 0
    assert data["totals"]["estimated_cost_sum"] is None
    assert data["by_day"] == []
    assert data["by_action"] == []


# ---------------------------------------------------------------------------
# /admin/api/usage/summary — end-to-end via FakeProvider generations
# ---------------------------------------------------------------------------


def test_summary_nonzero_after_fake_generations(app, client, db_session, fake_llm):
    fake_llm.enqueue_content("first")
    fake_llm.enqueue_content("second")

    llm = LLMClient()
    for content in ("first prompt", "second prompt"):
        llm.complete(
            ChatRequest(
                system_prompt="sys",
                user_prompt=content,
                model=MODEL,
                api_url=API_URL,
            )
        )

    data = _get_json(client, "/admin/api/usage/summary")

    # Two generations x usage {1, 1, 2} from the fake content response.
    assert data["totals"]["rows"] == 2
    assert data["totals"]["total_tokens"] == 4


# ---------------------------------------------------------------------------
# /admin/api/routes
# ---------------------------------------------------------------------------


def test_routes_endpoint_lists_rows_and_resolved_default(app, client, db_session):
    db_session.add_all(
        [
            ModelRoute(
                tier="creative",
                api_url=None,
                model_name="imagine-2",
                priority=10,
            ),
            ModelRoute(
                tier="default",
                api_url="http://127.0.0.1:8080/v1",
                model_name="local-model",
                priority=0,
            ),
        ]
    )
    db_session.commit()

    data = _get_json(client, "/admin/api/routes")

    routes = {(r["tier"], r["model_name"]): r for r in data["routes"]}
    assert len(data["routes"]) == 2
    creative = routes[("creative", "imagine-2")]
    assert creative["api_url"] is None
    assert creative["priority"] == 10
    assert creative["is_active"] is True
    default = routes[("default", "local-model")]
    assert default["api_url"] == "http://127.0.0.1:8080/v1"

    resolved = data["resolved_default"]
    assert resolved["model_name"] == "local-model"


def test_routes_endpoint_empty_table(app, client):
    data = _get_json(client, "/admin/api/routes")

    assert data["routes"] == []
    # Resolution still falls through to a usable default.
    assert data["resolved_default"]["model_name"]


# ---------------------------------------------------------------------------
# Admin auth protection
# ---------------------------------------------------------------------------


def test_endpoints_require_admin_when_token_set(app, client, monkeypatch):
    monkeypatch.setattr(Config, "get", staticmethod(lambda key, default=None: "s3cret"))

    for path in ("/admin/api/usage/summary", "/admin/api/routes"):
        resp = client.get(path)
        assert resp.status_code == 302
        assert "/admin/login" in resp.headers["Location"]

    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True

    for path in ("/admin/api/usage/summary", "/admin/api/routes"):
        assert client.get(path).status_code == 200
