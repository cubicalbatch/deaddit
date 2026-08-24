"""Shared fixtures: app/client/db, fake LLM provider, seed data, network guard."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from deaddit import create_app
from deaddit import db as _db
from deaddit.llm.provider import reset_provider, set_provider
from deaddit.models import Comment, Post, Subdeaddit, User
from tests.fakes import FakeProvider

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


@pytest.fixture()
def app():
    app = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite://", "TESTING": True})
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db_session(app):
    """An active SQLAlchemy session inside the app context."""
    return _db.session


@pytest.fixture()
def fake_llm():
    """Register a FakeProvider on the LLM transport seam; always reset."""
    provider = FakeProvider()
    set_provider(provider)
    yield provider
    reset_provider()


@pytest.fixture()
def seeded_db(app, db_session):
    """2 users, 2 subdeaddits, 3 posts and a few comments via the ORM."""
    users = [
        User(username="alice", bio="curious alice", interests='["testing"]'),
        User(username="bob", bio="bob builds things", interests='["coding"]'),
    ]
    subs = [
        Subdeaddit(name="testsub", description="A test subdeaddit"),
        Subdeaddit(name="askdeaddit", description="Questions and answers"),
    ]
    posts = [
        Post(
            title="Hello World",
            content="First post",
            user="alice",
            subdeaddit_name="testsub",
            model="test-model",
        ),
        Post(
            title="Seeded Post",
            content="Seeded content",
            user="bob",
            subdeaddit_name="testsub",
            model="test-model",
        ),
        Post(
            title="What is TDD?",
            content="Test-driven development?",
            user="alice",
            subdeaddit_name="askdeaddit",
            model="test-model",
        ),
    ]
    db_session.add_all(users + subs + posts)
    db_session.commit()

    comments = [
        Comment(
            post_id=posts[0].id,
            user="bob",
            content="Welcome!",
            model="test-model",
        ),
        Comment(
            post_id=posts[1].id,
            user="alice",
            content="Nice seed data.",
            model="test-model",
        ),
    ]
    db_session.add_all(comments)
    db_session.commit()
    return {"users": users, "subs": subs, "posts": posts, "comments": comments}


@pytest.fixture(autouse=True)
def _network_guard(request, monkeypatch):
    """Fail any attempt to open a non-localhost connection.

    llm_live-marked tests bypass the guard (they intentionally hit a real
    endpoint).
    """
    if request.node.get_closest_marker("llm_live"):
        yield
        return
    real_connect = socket.socket.connect

    def _guard(sock, address):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host in _LOCAL_HOSTS:
            return real_connect(sock, address)
        raise AssertionError(f"network egress attempted in tests (to {host!r})")

    def _guard_ex(sock, address):
        try:
            _guard(sock, address)
        except AssertionError:
            raise
        except OSError as exc:
            return exc.errno
        return 0

    monkeypatch.setattr(socket.socket, "connect", _guard)
    monkeypatch.setattr(socket.socket, "connect_ex", _guard_ex)
    yield


_FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    """Load a JSON file from tests/fixtures/."""
    return json.loads((_FIXTURES_DIR / name).read_text())
