"""Phase LLM-5: admin JSON surface for prompt versioning (deterministic).

The UX lane owns pages; these tests lock the JSON contract:
list/detail/version-create/pins/render-audit, all admin-gated.
"""

from __future__ import annotations

import pytest

from deaddit.config import Config
from deaddit.extensions import db
from deaddit.llm.prompts import create_template, create_version, set_pin
from deaddit.models import Agent, PromptTemplateVersion, User

LIST = "/admin/api/prompts"
PINS = "/admin/api/pins"


@pytest.fixture()
def authed_client(client):
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


def _seed_templates(app):
    with app.app_context():
        create_template("alpha", "A {x}", description="first")
        create_version("alpha", "A2 {x}")


def _seed_agent(app, username="pin_owner"):
    with app.app_context():
        user = User(username=username)
        db.session.add(user)
        db.session.flush()
        agent = Agent(
            user_username=user.username,
            autonomy_tier="regular",
            config={},
            state={},
        )
        db.session.add(agent)
        db.session.commit()


class TestPromptsApi:
    def test_requires_admin(self, app, client, monkeypatch):
        _seed_templates(app)
        monkeypatch.setattr(
            Config, "get", staticmethod(lambda key, default=None: "s3cret")
        )
        resp = client.get(LIST)
        assert resp.status_code == 302
        assert "/admin/login" in resp.headers["Location"]
        assert client.post(f"{LIST}/alpha/versions", json={}).status_code == 302

    def test_list_shows_versions_and_pins(self, app, authed_client):
        _seed_templates(app)
        agent_id = _seed_agent(app)
        with app.app_context():
            set_pin("agent", str(agent_id), "alpha", 2)
        resp = authed_client.get(LIST)
        assert resp.status_code == 200
        rows = resp.get_json()
        alpha = next(r for r in rows if r["name"] == "alpha")
        assert alpha["versions"] == [1, 2]
        assert alpha["latest_version"] == 2
        assert alpha["pinned_by"] == [f"agent:{agent_id}"]

    def test_detail_lists_immutable_versions(self, app, authed_client):
        _seed_templates(app)
        resp = authed_client.get(f"{LIST}/alpha")
        assert resp.status_code == 200
        body = resp.get_json()
        assert [v["version"] for v in body["versions"]] == [1, 2]
        assert body["versions"][0]["body"] == "A {x}"

    def test_detail_404_for_unknown_template(self, app, authed_client):
        assert authed_client.get(f"{LIST}/nope").status_code == 404

    def test_create_version_appends_leaving_old_queryable(self, app, authed_client):
        _seed_templates(app)
        resp = authed_client.post(
            f"{LIST}/alpha/versions", json={"body": "A3 {x}", "created_by": "tester"}
        )
        assert resp.status_code == 201
        assert resp.get_json()["version"] == 3
        with app.app_context():
            bodies = [
                v.body
                for v in PromptTemplateVersion.query.order_by(
                    PromptTemplateVersion.version
                )
            ]
        assert bodies == ["A {x}", "A2 {x}", "A3 {x}"]

    def test_create_version_rejects_missing_body(self, app, authed_client):
        _seed_templates(app)
        assert authed_client.post(f"{LIST}/alpha/versions", json={}).status_code == 400

    def test_create_version_404_for_unknown_template(self, app, authed_client):
        assert (
            authed_client.post(f"{LIST}/ghost/versions", json={"body": "b"}).status_code
            == 404
        )


class TestPinsApi:
    def test_set_and_list_pin(self, app, authed_client):
        _seed_templates(app)
        resp = authed_client.post(
            PINS,
            json={
                "target_kind": "cohort",
                "target_key": "parity",
                "template": "alpha",
                "version": 2,
            },
        )
        assert resp.status_code == 200
        pins = resp.get_json()
        assert pins == [
            {
                "target_kind": "cohort",
                "target_key": "parity",
                "template_id": pins[0]["template_id"],
                "template_name": "alpha",
                "version_number": 2,
                "updated_at": pins[0]["updated_at"],
            }
        ]

    def test_set_pin_rejects_bad_payload(self, app, authed_client):
        assert (
            authed_client.post(
                PINS,
                json={"target_kind": "agent", "target_key": "x", "template": "nope"},
            ).status_code
            == 400
        )

    def test_clear_pin(self, app, authed_client):
        _seed_templates(app)
        agent_id = _seed_agent(app, "clearable")
        with app.app_context():
            set_pin("agent", str(agent_id), "alpha", 1)
        assert authed_client.delete(f"{PINS}/agent/{agent_id}").status_code == 200
        assert authed_client.delete(f"{PINS}/agent/{agent_id}").status_code == 404


class TestRenderAuditApi:
    def test_lists_recent_renders(self, app, authed_client):
        _seed_templates(app)
        from deaddit.llm.prompts import render_pinned

        agent_id = _seed_agent(app, "audited")
        key = str(agent_id)
        with app.app_context():
            set_pin("agent", key, "alpha", 1)
            render_pinned("agent", key, {"x": "1"})
            db.session.commit()
        resp = authed_client.get("/admin/api/prompt-renders?limit=10")
        assert resp.status_code == 200
        rows = resp.get_json()
        assert len(rows) == 1
        assert rows[0]["subject_kind"] == "agent"
        assert rows[0]["subject_key"] == key
        assert rows[0]["subject_key"] != "audited"
        assert len(rows[0]["rendered_sha256"]) == 64
