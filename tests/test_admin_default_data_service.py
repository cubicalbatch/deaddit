"""A4 fix F1: /admin/api/load-default-data must persist through the content service.

Regression guard for the Resolution-1 gap: the admin route used to construct
``Subdeaddit``/``User`` rows directly. These tests spy on the symbols imported
into ``deaddit.admin`` and prove every row is created via
``deaddit.services.content`` with skip-if-exists semantics preserved.
"""

from __future__ import annotations

import pytest

from deaddit.config import Config
from deaddit.models import Subdeaddit, User
from deaddit.services.content import create_subdeaddit as svc_create_subdeaddit
from deaddit.services.content import create_user as svc_create_user

ROUTE = "/admin/api/load-default-data"


@pytest.fixture()
def admin_client(client):
    """Client that passes the admin_required gate even if API_TOKEN is set."""
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


@pytest.fixture()
def service_spies(monkeypatch):
    """Wrap deaddit.admin's imported content-service symbols, delegating for real."""
    calls: dict[str, list[str]] = {"subdeaddit": [], "user": []}

    def spy_subdeaddit(**kwargs):
        calls["subdeaddit"].append(kwargs["name"])
        return svc_create_subdeaddit(**kwargs)

    def spy_user(**kwargs):
        calls["user"].append(kwargs["username"])
        return svc_create_user(**kwargs)

    monkeypatch.setattr("deaddit.admin.create_subdeaddit", spy_subdeaddit)
    monkeypatch.setattr("deaddit.admin.create_user", spy_user)
    return calls


def test_load_default_data_persists_via_content_service(
    admin_client, service_spies, app
):
    resp = admin_client.post(ROUTE)

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    # Exact legacy response shape.
    assert set(body) == {"success", "message", "subdeaddits_loaded", "users_loaded"}
    # Counters match the number of service-mediated creations.
    assert body["subdeaddits_loaded"] == len(service_spies["subdeaddit"]) > 0
    assert body["users_loaded"] == len(service_spies["user"]) > 0
    assert len(service_spies["user"]) <= 50  # first-50 limit preserved

    with app.app_context():
        assert Subdeaddit.query.filter_by(name=service_spies["subdeaddit"][0]).first()
        assert User.query.filter_by(username=service_spies["user"][0]).first()
        assert Config.get("DEFAULT_DATA_LOADED") == "true"


def test_load_default_data_skips_existing_rows(admin_client, service_spies):
    first = admin_client.post(ROUTE)
    assert first.status_code == 200
    seen_subs = list(service_spies["subdeaddit"])
    seen_users = list(service_spies["user"])

    second = admin_client.post(ROUTE)
    assert second.status_code == 200
    body = second.get_json()

    assert body["success"] is True
    assert body["subdeaddits_loaded"] == 0
    assert body["users_loaded"] == 0
    # No additional service calls were made for already-present rows.
    assert service_spies["subdeaddit"] == seen_subs
    assert service_spies["user"] == seen_users
