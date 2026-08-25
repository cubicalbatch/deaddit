"""A1 render smoke test: key pages must render after the blueprint move.

Guards against template url_for() references to pre-blueprint endpoint
names (e.g. url_for('index')) which raise BuildError and 500 once the web
views moved onto the 'web' blueprint.
"""

import pytest

from deaddit import create_app
from deaddit import db as _db
from deaddit.models import Post, Subdeaddit, User


@pytest.fixture()
def app():
    app = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite://", "TESTING": True})
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


def test_front_page_renders(app):
    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200


def test_subdeaddit_and_user_pages_render_with_data(app):
    with app.app_context():
        user = User(username="alice", bio="a bio", interests='["testing"]')
        sub = Subdeaddit(
            name="testsub",
            description="A test subdeaddit",
        )
        post = Post(
            title="Hello",
            content="World",
            user="alice",
            subdeaddit_name="testsub",
            model="test-model",
        )
        _db.session.add_all([user, sub, post])
        _db.session.commit()

    client = app.test_client()
    assert client.get("/d/testsub").status_code == 200
    assert client.get("/user/alice").status_code == 200


def test_live_page_renders_on_empty_db(app):
    """UX-6: the public live ticker joins the smoke set (empty-DB render)."""
    client = app.test_client()
    resp = client.get("/live")
    assert resp.status_code == 200
    assert b"<h1>Live</h1>" in resp.data
