"""Generated-website deletion and soft-removal lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from deaddit import create_app
from deaddit import db as _db
from deaddit.models import (
    Agent,
    AgentMemory,
    AgentRun,
    AgentTurn,
    GeneratedWebsite,
    Post,
    Subdeaddit,
    ToolCall,
    User,
)
from deaddit.websites import service as website_service
from deaddit.websites.storage import WebsiteStorageError, store_website


@dataclass(frozen=True)
class _WebsitePaths:
    post_id: int
    public_path: str
    storage_path: str


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "TESTING": True,
            "GENERATED_WEBSITES_ROOT": str(tmp_path / "websites"),
        }
    )
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def admin_client(client):
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


@pytest.fixture()
def db_session(app):
    _db.session.add_all(
        [
            User(username="alice", bio="", interests="[]"),
            User(username="bob", bio="", interests="[]"),
            Subdeaddit(name="testsub", description="A test subdeaddit"),
            Subdeaddit(name="othersub", description="Another test subdeaddit"),
        ]
    )
    _db.session.commit()
    return _db.session


def _make_website_post(
    app,
    db_session,
    *,
    user="alice",
    subdeaddit="testsub",
    hostname="www.example.test",
    page_name="page.html",
    html="<html><body>hello</body></html>",
    removed=False,
) -> _WebsitePaths:
    stored = store_website(
        html.encode("utf-8"), Path(app.config["GENERATED_WEBSITES_ROOT"])
    )
    post = Post(
        title="Generated page",
        content="A generated website",
        subdeaddit_name=subdeaddit,
        user=user,
        removed=removed,
    )
    db_session.add(post)
    db_session.flush()
    website = GeneratedWebsite(
        post_id=post.id,
        public_path=f"{hostname}/{page_name}",
        storage_path=stored.storage_path,
        hostname=hostname,
        page_name=page_name,
        source_description="A private source description",
        byte_size=stored.byte_size,
        sha256=stored.sha256,
        creator_username_snapshot=user,
        api_url_snapshot="https://llm.example/v1",
        model_snapshot="test-model",
    )
    db_session.add(website)
    db_session.commit()
    return _WebsitePaths(post.id, website.public_path, website.storage_path)


def _website_file(app, paths: _WebsitePaths) -> Path:
    return Path(app.config["GENERATED_WEBSITES_ROOT"]) / paths.storage_path


def _assert_deleted(db_session, app, paths: _WebsitePaths) -> None:
    assert db_session.get(GeneratedWebsite, paths.post_id) is None
    assert db_session.get(Post, paths.post_id) is None
    assert not _website_file(app, paths).exists()


def test_soft_removal_preserves_file_and_unremoval_restores_serving(
    app, client, db_session
):
    paths = _make_website_post(app, db_session, hostname="soft.example.test")
    assert client.get(f"/out/{paths.public_path}").status_code == 200

    post = db_session.get(Post, paths.post_id)
    post.removed = True
    db_session.commit()
    assert client.get(f"/out/{paths.public_path}").status_code == 404
    assert _website_file(app, paths).is_file()

    post.removed = False
    db_session.commit()
    assert client.get(f"/out/{paths.public_path}").status_code == 200
    assert _website_file(app, paths).is_file()


def test_hard_delete_removes_website_files_on_every_admin_path(
    app, admin_client, db_session
):
    def ok(response):
        assert response.status_code == 200, response.get_data(as_text=True)
        assert response.get_json()["success"] is True

    single = _make_website_post(app, db_session, hostname="single.example.test")
    ok(admin_client.delete(f"/admin/api/posts/{single.post_id}"))
    _assert_deleted(db_session, app, single)

    bulk_first = _make_website_post(
        app, db_session, hostname="bulk-first.example.test", page_name="one.html"
    )
    bulk_second = _make_website_post(
        app, db_session, hostname="bulk-second.example.test", page_name="two.html"
    )
    ok(
        admin_client.post(
            "/admin/api/posts/bulk-delete",
            json={"post_ids": [bulk_first.post_id, bulk_second.post_id]},
        )
    )
    _assert_deleted(db_session, app, bulk_first)
    _assert_deleted(db_session, app, bulk_second)

    text_post = Post(
        title="Text only",
        content="No website",
        subdeaddit_name="testsub",
        user="alice",
    )
    db_session.add(text_post)
    db_session.commit()
    ok(admin_client.delete(f"/admin/api/posts/{text_post.id}"))
    assert db_session.get(Post, text_post.id) is None

    owner = User(username="owner", bio="", interests="[]")
    db_session.add(owner)
    db_session.commit()
    owned = _make_website_post(
        app,
        db_session,
        user="owner",
        hostname="owned.example.test",
    )
    agent = Agent(
        user_username="owner",
        autonomy_tier="regular",
        is_enabled=False,
        status="idle",
        config={},
        state={},
    )
    db_session.add(agent)
    db_session.flush()
    run = AgentRun(
        agent_id=agent.id,
        persona_username="owner",
        trigger="manual",
        status="completed",
    )
    db_session.add(run)
    db_session.flush()
    turn = AgentTurn(
        run_id=run.id,
        seq=0,
        request_messages={},
        response_message={},
    )
    db_session.add(turn)
    db_session.flush()
    db_session.add(
        ToolCall(
            turn_id=turn.id,
            run_id=run.id,
            name="finish",
            ok=True,
        )
    )
    db_session.add(AgentMemory(user_username="owner", kind="episode", content="memory"))
    db_session.commit()
    run_id, turn_id, agent_id = run.id, turn.id, agent.id
    ok(admin_client.delete("/admin/api/users/owner"))
    _assert_deleted(db_session, app, owned)
    assert db_session.get(User, "owner") is None
    assert db_session.get(Agent, agent_id) is None
    assert db_session.get(AgentRun, run_id) is None
    assert db_session.get(AgentTurn, turn_id) is None
    assert db_session.query(ToolCall).filter_by(run_id=run_id).first() is None
    assert (
        db_session.query(AgentMemory).filter_by(user_username="owner").first() is None
    )

    bulk_user_a = User(username="bulk-user-a", bio="", interests="[]")
    bulk_user_b = User(username="bulk-user-b", bio="", interests="[]")
    db_session.add_all([bulk_user_a, bulk_user_b])
    db_session.commit()
    bulk_user_paths = [
        _make_website_post(
            app,
            db_session,
            user="bulk-user-a",
            hostname="bulk-user-a.example.test",
        ),
        _make_website_post(
            app,
            db_session,
            user="bulk-user-b",
            hostname="bulk-user-b.example.test",
        ),
    ]
    ok(
        admin_client.post(
            "/admin/api/users/bulk-delete",
            json={"usernames": ["bulk-user-a", "bulk-user-b"]},
        )
    )
    for paths in bulk_user_paths:
        _assert_deleted(db_session, app, paths)

    subdeaddit_paths = [
        _make_website_post(
            app,
            db_session,
            subdeaddit="testsub",
            hostname="sub-single.example.test",
        ),
        _make_website_post(
            app,
            db_session,
            subdeaddit="othersub",
            hostname="spared.example.test",
        ),
    ]
    ok(admin_client.delete("/admin/api/subdeaddits/testsub"))
    _assert_deleted(db_session, app, subdeaddit_paths[0])
    assert db_session.get(Post, subdeaddit_paths[1].post_id) is not None
    assert _website_file(app, subdeaddit_paths[1]).is_file()

    bulk_sub_a = Subdeaddit(name="bulk-sub-a", description="A")
    bulk_sub_b = Subdeaddit(name="bulk-sub-b", description="B")
    db_session.add_all([bulk_sub_a, bulk_sub_b])
    db_session.commit()
    bulk_sub_paths = [
        _make_website_post(
            app,
            db_session,
            subdeaddit="bulk-sub-a",
            hostname="bulk-sub-a.example.test",
        ),
        _make_website_post(
            app,
            db_session,
            subdeaddit="bulk-sub-b",
            hostname="bulk-sub-b.example.test",
        ),
    ]
    ok(
        admin_client.post(
            "/admin/api/subdeaddits/bulk-delete",
            json={"names": ["bulk-sub-a", "bulk-sub-b"]},
        )
    )
    for paths in bulk_sub_paths:
        _assert_deleted(db_session, app, paths)


def test_failed_post_delete_commit_preserves_website_file_and_rows(
    app, admin_client, db_session, monkeypatch
):
    paths = _make_website_post(app, db_session, hostname="failed.example.test")

    def raiser():
        raise RuntimeError("forced commit failure")

    monkeypatch.setattr(_db.session, "commit", raiser)
    response = admin_client.delete(f"/admin/api/posts/{paths.post_id}")

    assert response.status_code == 500
    assert db_session.get(Post, paths.post_id) is not None
    assert db_session.get(GeneratedWebsite, paths.post_id) is not None
    assert _website_file(app, paths).is_file()


def test_website_service_handles_empty_ids_and_storage_errors(
    app, db_session, monkeypatch, caplog
):
    class ExplodingQuery:
        def filter(self, *_args):
            raise AssertionError("empty IDs must not query")

    monkeypatch.setattr(GeneratedWebsite, "query", ExplodingQuery())
    assert website_service.website_paths_for_posts([]) == []

    paths = [
        website_service.WebsitePaths(1, "pages/one.html"),
        website_service.WebsitePaths(2, "pages/two.html"),
    ]
    deleted = []

    def fake_delete(root, storage_path):
        deleted.append(storage_path)
        if storage_path.endswith("one.html"):
            raise WebsiteStorageError("bad file")

    monkeypatch.setattr(website_service, "delete_website", fake_delete)
    with caplog.at_level("WARNING", logger="deaddit.websites.service"):
        website_service.delete_website_files(app, paths)

    assert deleted == ["pages/one.html", "pages/two.html"]
    assert "post 1" in caplog.text


def test_orm_agent_delete_cascades_runtime_rows(app, db_session):
    agent = Agent(
        user_username="alice",
        autonomy_tier="regular",
        is_enabled=False,
        status="idle",
        config={},
        state={},
    )
    db_session.add(agent)
    db_session.flush()
    run = AgentRun(
        agent_id=agent.id,
        persona_username="alice",
        trigger="manual",
        status="completed",
    )
    db_session.add(run)
    db_session.flush()
    turn = AgentTurn(
        run_id=run.id,
        seq=0,
        request_messages={},
        response_message={},
    )
    db_session.add(turn)
    db_session.flush()
    call = ToolCall(turn_id=turn.id, run_id=run.id, name="finish", ok=True)
    db_session.add(call)
    db_session.commit()
    agent_id, run_id, turn_id, call_id = agent.id, run.id, turn.id, call.id

    db_session.delete(agent)
    db_session.commit()

    assert db_session.get(Agent, agent_id) is None
    assert db_session.get(AgentRun, run_id) is None
    assert db_session.get(AgentTurn, turn_id) is None
    assert db_session.get(ToolCall, call_id) is None
