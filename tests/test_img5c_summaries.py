"""Coverage for has_image in agent post-read summaries (plan 5C).

browse_feed, search (post type), and view_profile summaries expose a plain
has_image boolean and never a description or anything that would trigger a
vision call - pixel analysis only happens on an explicit read_post. Removed
or imageless posts must never claim has_image=True, and listing has_image
for N posts must not cost N extra queries.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from deaddit import create_app
from deaddit import db as _db
from deaddit.agents.executor import execute
from deaddit.agents.registry import ToolContext
from deaddit.extensions import db as ext_db
from deaddit.images.storage import store_variants
from deaddit.models import Agent, AgentRun, Post, PostImage, Subdeaddit, User

API_URL = "http://llm.test/v1"
MODEL = "test-model"


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "TESTING": True,
            "GENERATED_IMAGES_ROOT": str(tmp_path / "media"),
        }
    )
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def db_session(app):
    return _db.session


def _solid_png(color=(30, 60, 200), size=(16, 16)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def _seed_users_and_subs(db_session):
    db_session.add_all(
        [
            User(username="alice", bio="curious alice", interests="[]"),
            Subdeaddit(name="testsub", description="A test subdeaddit"),
        ]
    )
    db_session.commit()


def _make_agent(db_session, *, username="alice") -> Agent:
    agent = Agent(
        user_username=username,
        autonomy_tier="regular",
        is_enabled=True,
        status="idle",
        config={},
        state={},
        consecutive_failures=0,
    )
    db_session.add(agent)
    db_session.commit()
    return agent


def _new_run(db_session, agent) -> AgentRun:
    run = AgentRun(agent_id=agent.id, trigger="manual", status="running")
    db_session.add(run)
    db_session.commit()
    return run


def _ctx(agent, run, **overrides) -> ToolContext:
    fields = {
        "agent": agent,
        "run": run,
        "user_username": agent.user_username,
        "llm_api_url": API_URL,
        "llm_api_key": None,
        "llm_model": MODEL,
        "deadline": None,
    }
    fields.update(overrides)
    return ToolContext(**fields)


def _make_image_post(app, db_session, *, title="A cat photo", removed=False) -> Post:
    root = app.config["GENERATED_IMAGES_ROOT"]
    stored = store_variants(_solid_png(), Path(root))
    post = Post(
        title=title,
        content="a photo post",
        subdeaddit_name="testsub",
        user="alice",
        removed=removed,
    )
    db_session.add(post)
    db_session.flush()
    image = PostImage(
        post_id=post.id,
        original_path=stored.original_path,
        thumbnail_path=stored.thumbnail_path,
        mime_type=stored.mime_type,
        byte_size=stored.original_size,
        width=stored.width,
        height=stored.height,
        alt_text="A blue square",
        source_prompt="A vivid blue square, flat color, studio lit.",
        provider_snapshot="Fal",
        model_snapshot="fal-ai/flux-1/schnell",
        request_snapshot="req-1",
    )
    db_session.add(image)
    db_session.commit()
    return post


def _make_text_post(db_session, *, title="Just words", removed=False) -> Post:
    post = Post(
        title=title,
        content="No pixels here.",
        subdeaddit_name="testsub",
        user="alice",
        removed=removed,
    )
    db_session.add(post)
    db_session.commit()
    return post


class _QueryCounter:
    """Counts statements executed against the bound engine's connection."""

    def __init__(self):
        self.count = 0

    def __enter__(self):
        from sqlalchemy import event

        self._event = event

        def _tick(*_args, **_kwargs):
            self.count += 1

        self._listener = _tick
        event.listen(ext_db.engine, "before_cursor_execute", self._listener)
        return self

    def __exit__(self, *exc_info):
        self._event.remove(ext_db.engine, "before_cursor_execute", self._listener)


# ---------------------------------------------------------------------------
# browse_feed


def test_browse_feed_exposes_has_image_boolean_only(app, db_session):
    _seed_users_and_subs(db_session)
    image_post = _make_image_post(app, db_session, title="With image")
    text_post = _make_text_post(db_session, title="No image")
    agent = _make_agent(db_session)
    run = _new_run(db_session, agent)

    result = execute("browse_feed", {"subdeaddit": "testsub"}, _ctx(agent, run))

    by_id = {p["id"]: p for p in result["posts"]}
    assert by_id[image_post.id]["has_image"] is True
    assert by_id[text_post.id]["has_image"] is False
    # Only the plain boolean is exposed - no description, no source marker.
    assert "description" not in by_id[image_post.id]
    assert "image" not in by_id[image_post.id]


def test_browse_feed_suppresses_has_image_for_removed_post(app, db_session):
    _seed_users_and_subs(db_session)
    removed_post = _make_image_post(app, db_session, title="Removed", removed=True)
    agent = _make_agent(db_session)
    run = _new_run(db_session, agent)

    result = execute("browse_feed", {"subdeaddit": "testsub"}, _ctx(agent, run))

    by_id = {p["id"]: p for p in result["posts"]}
    assert by_id[removed_post.id]["has_image"] is False


def test_browse_feed_has_image_is_not_n_plus_one(app, db_session):
    _seed_users_and_subs(db_session)
    for i in range(5):
        _make_image_post(app, db_session, title=f"Image {i}")
    for i in range(5):
        _make_text_post(db_session, title=f"Text {i}")
    agent = _make_agent(db_session)
    run = _new_run(db_session, agent)

    with _QueryCounter() as counter:
        result = execute("browse_feed", {"subdeaddit": "testsub"}, _ctx(agent, run))

    assert len(result["posts"]) == 10
    # A handful of fixed queries (posts, comment counts, image ids, plus
    # session bookkeeping) regardless of post count - not one per post.
    assert counter.count < 10


# ---------------------------------------------------------------------------
# search


def test_search_posts_exposes_has_image_and_suppresses_removed(app, db_session):
    _seed_users_and_subs(db_session)
    shown = _make_image_post(app, db_session, title="searchable image post")
    removed = _make_image_post(
        app, db_session, title="searchable removed image post", removed=True
    )
    text = _make_text_post(db_session, title="searchable text post")
    agent = _make_agent(db_session)
    run = _new_run(db_session, agent)

    result = execute(
        "search", {"query": "searchable", "type": "post"}, _ctx(agent, run)
    )

    by_id = {r["id"]: r for r in result["results"]}
    assert by_id[shown.id]["has_image"] is True
    assert by_id[removed.id]["has_image"] is False
    assert by_id[text.id]["has_image"] is False
    assert "description" not in by_id[shown.id]


# ---------------------------------------------------------------------------
# view_profile


def test_view_profile_exposes_has_image_and_suppresses_removed(app, db_session):
    _seed_users_and_subs(db_session)
    shown = _make_image_post(app, db_session, title="profile image post")
    removed = _make_image_post(
        app, db_session, title="profile removed image post", removed=True
    )
    text = _make_text_post(db_session, title="profile text post")
    agent = _make_agent(db_session)
    run = _new_run(db_session, agent)

    result = execute("view_profile", {"username": "alice"}, _ctx(agent, run))

    by_id = {p["id"]: p for p in result["posts"]}
    assert by_id[shown.id]["has_image"] is True
    assert by_id[removed.id]["has_image"] is False
    assert by_id[text.id]["has_image"] is False
    assert "description" not in by_id[shown.id]


# ---------------------------------------------------------------------------
# read_post still carries the full normalized image object (plan 5B, unchanged)


def test_read_post_still_returns_full_image_object_not_just_a_boolean(app, db_session):
    _seed_users_and_subs(db_session)
    post = _make_image_post(app, db_session)
    agent = _make_agent(db_session)
    run = _new_run(db_session, agent)

    result = execute("read_post", {"post_id": post.id}, _ctx(agent, run))

    image = result["post"]["image"]
    assert image["present"] is True
    assert image["description_source"] == "generation_prompt"
    assert "has_image" not in result["post"]
