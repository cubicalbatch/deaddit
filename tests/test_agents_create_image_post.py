"""Coverage for the create_image_post agent tool (plan 4B).

Full-stack tests of deaddit.agents.executor.execute("create_image_post", ...)
against a real (tmp_path-rooted) media store, a FakeImageAdapter registered
on the deaddit.images.client seam, and a real SQLite-in-memory database.
Nothing here ever reaches fal.ai or Runware: every generation is served by
FakeImageAdapter, and the autouse conftest network guard would fail any real
egress attempt anyway.
"""

from __future__ import annotations

import time
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy.exc import SQLAlchemyError

from deaddit import create_app
from deaddit import db as _db
from deaddit.agents.executor import execute
from deaddit.agents.registry import ToolContext
from deaddit.images.client import register_adapter, reset_adapters
from deaddit.images.types import (
    Deadline,
    ImageGenerationResult,
    ImageProviderTransientError,
)
from deaddit.models import (
    Agent,
    AgentRun,
    ImageProvider,
    Post,
    PostImage,
    Subdeaddit,
    ToolCall,
    User,
)
from tests.fakes import FakeImageAdapter

IMAGE_ARGS = {
    "community": "testsub",
    "title": "A cat photo",
    "image_prompt": "A fluffy orange cat sitting on a windowsill in soft light",
    "alt_text": "An orange cat on a windowsill",
}


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "TESTING": True,
            "GENERATED_IMAGES_ROOT": str(tmp_path / "media"),
        }
    )
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def db_session(app):
    return _db.session


@pytest.fixture(autouse=True)
def _clean_adapters():
    reset_adapters()
    yield
    reset_adapters()


@pytest.fixture()
def fake_adapter(monkeypatch):
    adapter = FakeImageAdapter()
    register_adapter("fal", adapter)
    monkeypatch.setenv("FALAI_API_KEY", "test-secret-value")
    return adapter


def _png_bytes() -> bytes:
    image = Image.new("RGB", (32, 32), color=(10, 20, 30))
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _generation(**overrides) -> ImageGenerationResult:
    fields = {
        "request_id": "req-1",
        "image_url": None,
        "image_bytes": _png_bytes(),
        "mime_type": "image/png",
        "width": 32,
        "height": 32,
    }
    fields.update(overrides)
    return ImageGenerationResult(**fields)


def _seed(db_session):
    db_session.add_all(
        [
            User(username="alice", bio="curious alice", interests='["testing"]'),
            Subdeaddit(name="testsub", description="A test subdeaddit"),
        ]
    )
    db_session.commit()


def _make_provider(db_session, **overrides) -> ImageProvider:
    fields = {
        "name": "Fal",
        "provider_type": "fal",
        "credential_env": "FALAI_API_KEY",
        "default_model": "fal-ai/flux-1/schnell",
        "is_enabled": True,
    }
    fields.update(overrides)
    provider = ImageProvider(**fields)
    db_session.add(provider)
    db_session.commit()
    return provider


def _make_agent(db_session, *, config, tier="regular", username="alice") -> Agent:
    agent = Agent(
        user_username=username,
        autonomy_tier=tier,
        is_enabled=True,
        status="idle",
        config=config,
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


def _ctx(agent, run, *, deadline=None) -> ToolContext:
    return ToolContext(
        agent=agent, run=run, user_username=agent.user_username, deadline=deadline
    )


def _media_root(app) -> Path:
    return Path(app.config["GENERATED_IMAGES_ROOT"])


def _stored_files(app) -> list[Path]:
    root = _media_root(app)
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.is_file()]


# ---------------------------------------------------------------------------
# Gating: authorization is independent of what specs_for offered.


def test_disabled_agent_rejects_create_image_post_even_called_directly(app, db_session):
    _seed(db_session)
    agent = _make_agent(db_session, config={})
    run = _new_run(db_session, agent)

    result = execute("create_image_post", IMAGE_ARGS, _ctx(agent, run))

    assert result["ok"] is False
    assert "not enabled" in result["error"]
    assert Post.query.count() == 0
    row = ToolCall.query.order_by(ToolCall.id.desc()).first()
    assert row.name == "create_image_post"
    assert row.ok is False


def test_disabled_flag_rejects_create_image_post(app, db_session):
    _seed(db_session)
    agent = _make_agent(
        db_session, config={"image_posts": {"enabled": False, "provider_id": 1}}
    )
    run = _new_run(db_session, agent)

    result = execute("create_image_post", IMAGE_ARGS, _ctx(agent, run))

    assert result["ok"] is False
    assert "not enabled" in result["error"]


def test_image_only_agent_rejects_create_post_even_called_directly(
    app, db_session, fake_adapter
):
    _seed(db_session)
    provider = _make_provider(db_session)
    agent = _make_agent(
        db_session,
        config={
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "policy": "image_only",
            }
        },
    )
    run = _new_run(db_session, agent)

    result = execute(
        "create_post",
        {"subdeaddit": "testsub", "title": "T", "content": "Body text"},
        _ctx(agent, run),
    )

    assert result["ok"] is False
    assert "image posts" in result["error"]
    assert Post.query.count() == 0
    assert fake_adapter.generate_calls == []


def test_optional_agent_permits_both_tools_in_separate_runs(
    app, db_session, fake_adapter
):
    _seed(db_session)
    provider = _make_provider(db_session)
    agent = _make_agent(
        db_session,
        config={
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "policy": "optional",
            }
        },
    )
    fake_adapter.enqueue_generate(_generation())
    run1 = _new_run(db_session, agent)
    image_result = execute(
        "create_image_post", IMAGE_ARGS, _ctx(agent, run1, deadline=Deadline.after(60))
    )

    run2 = _new_run(db_session, agent)
    text_result = execute(
        "create_post",
        {
            "subdeaddit": "testsub",
            "title": "A distinct text post",
            "content": "Completely different topic about gardening tools.",
        },
        _ctx(agent, run2),
    )

    assert image_result["ok"] is True
    assert text_result["ok"] is True
    assert Post.query.count() == 2
    assert PostImage.query.count() == 1


# ---------------------------------------------------------------------------
# Success path


def test_success_writes_one_post_one_image_one_ok_toolcall(
    app, db_session, fake_adapter
):
    _seed(db_session)
    provider = _make_provider(db_session)
    agent = _make_agent(
        db_session,
        config={
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "policy": "optional",
            }
        },
    )
    run = _new_run(db_session, agent)
    fake_adapter.enqueue_generate(_generation(request_id="req-42"))

    result = execute(
        "create_image_post", IMAGE_ARGS, _ctx(agent, run, deadline=Deadline.after(60))
    )

    assert result["ok"] is True
    assert result["post_id"]
    assert Post.query.count() == 1
    assert PostImage.query.count() == 1
    image = PostImage.query.one()
    assert image.alt_text == IMAGE_ARGS["alt_text"]
    assert image.source_prompt == IMAGE_ARGS["image_prompt"]
    assert image.provider_snapshot == "Fal"
    assert image.model_snapshot == "fal-ai/flux-1/schnell"
    assert image.provider_id == provider.id
    assert image.request_snapshot == "req-42"

    root = _media_root(app)
    assert (root / image.original_path).is_file()
    assert (root / image.thumbnail_path).is_file()

    rows = ToolCall.query.filter_by(run_id=run.id).all()
    assert len(rows) == 1
    assert rows[0].ok is True
    assert rows[0].name == "create_image_post"


def test_model_override_wins_over_provider_default(app, db_session, fake_adapter):
    _seed(db_session)
    provider = _make_provider(db_session, default_model="fal-ai/flux-1/schnell")
    agent = _make_agent(
        db_session,
        config={
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "model": "fal-ai/flux-1/dev",
                "policy": "optional",
            }
        },
    )
    run = _new_run(db_session, agent)
    fake_adapter.enqueue_generate(_generation())

    result = execute(
        "create_image_post", IMAGE_ARGS, _ctx(agent, run, deadline=Deadline.after(60))
    )

    assert result["ok"] is True
    assert fake_adapter.generate_calls[0]["model_id"] == "fal-ai/flux-1/dev"
    assert PostImage.query.one().model_snapshot == "fal-ai/flux-1/dev"


# ---------------------------------------------------------------------------
# Failure/cleanup paths


def test_provider_failure_creates_no_post_and_no_files(app, db_session, fake_adapter):
    _seed(db_session)
    provider = _make_provider(db_session)
    agent = _make_agent(
        db_session,
        config={
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "policy": "optional",
            }
        },
    )
    run = _new_run(db_session, agent)
    fake_adapter.enqueue_error(
        ImageProviderTransientError("upstream 503"), method="generate"
    )

    result = execute(
        "create_image_post", IMAGE_ARGS, _ctx(agent, run, deadline=Deadline.after(60))
    )

    assert result["ok"] is False
    assert "image generation failed" in result["error"]
    assert Post.query.count() == 0
    assert PostImage.query.count() == 0
    assert _stored_files(app) == []


def test_sqlalchemy_failure_removes_stored_files_and_returns_ok_false(
    app, db_session, fake_adapter, monkeypatch
):
    _seed(db_session)
    provider = _make_provider(db_session)
    agent = _make_agent(
        db_session,
        config={
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "policy": "optional",
            }
        },
    )
    run = _new_run(db_session, agent)
    fake_adapter.enqueue_generate(_generation())

    from deaddit.agents import tools_write

    def _boom(**kwargs):
        raise SQLAlchemyError("db exploded")

    monkeypatch.setattr(tools_write, "create_image_post", _boom)

    result = execute(
        "create_image_post", IMAGE_ARGS, _ctx(agent, run, deadline=Deadline.after(60))
    )

    assert result["ok"] is False
    assert "failed to save" in result["error"]
    assert Post.query.count() == 0
    assert PostImage.query.count() == 0
    assert _stored_files(app) == []
    row = ToolCall.query.order_by(ToolCall.id.desc()).first()
    assert row.ok is False
    assert row.name == "create_image_post"


def test_recheck_ban_after_generation_removes_stored_files(
    app, db_session, fake_adapter, monkeypatch
):
    """A ban lands mid-flight (after preflight, during 'generation') - the
    content service's commit-time recheck must still catch it, and the
    already-stored files must be cleaned up (plan 4B: 'a DB failure removes
    stored files')."""
    _seed(db_session)
    provider = _make_provider(db_session)
    agent = _make_agent(
        db_session,
        config={
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "policy": "optional",
            }
        },
    )
    run = _new_run(db_session, agent)

    from deaddit.agents import tools_write
    from deaddit.models import Ban

    def _generate_then_ban(*args, **kwargs):
        db_session.add(
            Ban(username="alice", subdeaddit_name="testsub", reason="mid-flight")
        )
        db_session.commit()
        return _generation()

    monkeypatch.setattr(tools_write, "generate_image", _generate_then_ban)

    result = execute(
        "create_image_post", IMAGE_ARGS, _ctx(agent, run, deadline=Deadline.after(60))
    )

    assert result["ok"] is False
    assert "banned" in result["error"]
    assert Post.query.count() == 0
    assert PostImage.query.count() == 0
    assert _stored_files(app) == []


def test_missing_provider_row_rejects_without_calling_generate(
    app, db_session, fake_adapter
):
    _seed(db_session)
    agent = _make_agent(
        db_session,
        config={
            "image_posts": {"enabled": True, "provider_id": 999, "policy": "optional"}
        },
    )
    run = _new_run(db_session, agent)

    result = execute(
        "create_image_post", IMAGE_ARGS, _ctx(agent, run, deadline=Deadline.after(60))
    )

    assert result["ok"] is False
    assert "provider" in result["error"]
    assert fake_adapter.generate_calls == []
    assert Post.query.count() == 0


def test_expired_deadline_rejects_without_calling_provider(
    app, db_session, fake_adapter
):
    _seed(db_session)
    provider = _make_provider(db_session)
    agent = _make_agent(
        db_session,
        config={
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "policy": "optional",
            }
        },
    )
    run = _new_run(db_session, agent)
    expired = Deadline(expires_at=time.monotonic() - 5)

    result = execute(
        "create_image_post", IMAGE_ARGS, _ctx(agent, run, deadline=expired)
    )

    assert result["ok"] is False
    assert "time remaining" in result["error"]
    assert fake_adapter.generate_calls == []
    assert Post.query.count() == 0


def test_unknown_community_rejects_without_calling_generate(
    app, db_session, fake_adapter
):
    _seed(db_session)
    provider = _make_provider(db_session)
    agent = _make_agent(
        db_session,
        config={
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "policy": "optional",
            }
        },
    )
    run = _new_run(db_session, agent)

    result = execute(
        "create_image_post",
        {**IMAGE_ARGS, "community": "nonexistent"},
        _ctx(agent, run, deadline=Deadline.after(60)),
    )

    assert result["ok"] is False
    assert "does not exist" in result["error"]
    assert fake_adapter.generate_calls == []


# ---------------------------------------------------------------------------
# Shared post budget/guardrails across create_post and create_image_post


def test_per_run_post_budget_shared_across_tool_names(app, db_session, fake_adapter):
    _seed(db_session)
    provider = _make_provider(db_session)
    agent = _make_agent(
        db_session,
        config={
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "policy": "optional",
            }
        },
    )
    run = _new_run(db_session, agent)
    fake_adapter.enqueue_generate(_generation())

    first = execute(
        "create_image_post", IMAGE_ARGS, _ctx(agent, run, deadline=Deadline.after(60))
    )
    second = execute(
        "create_post",
        {
            "subdeaddit": "testsub",
            "title": "Second post same run",
            "content": "This should be blocked by the shared per-run cap.",
        },
        _ctx(agent, run),
    )

    assert first["ok"] is True
    assert second["ok"] is False
    assert "already created a post" in second["error"]
    assert Post.query.count() == 1
    assert (
        len(fake_adapter.generate_calls) == 1
    )  # second call never reached the handler


def test_image_post_failure_does_not_authorize_fallback_text_post(
    app, db_session, fake_adapter
):
    """An image-post failure must not leave create_post as an unthrottled
    fallback within the same run (plan 4B)."""
    _seed(db_session)
    provider = _make_provider(db_session)
    agent = _make_agent(
        db_session,
        config={
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "policy": "optional",
            }
        },
    )
    run = _new_run(db_session, agent)
    fake_adapter.enqueue_error(
        ImageProviderTransientError("upstream 503"), method="generate"
    )

    failed = execute(
        "create_image_post", IMAGE_ARGS, _ctx(agent, run, deadline=Deadline.after(60))
    )
    # The failed attempt did not consume the run's post slot (it never
    # produced an ok=True ToolCall), so a subsequent text post in the SAME
    # run is still allowed - a failure is not "already posted", but it also
    # never lets image failure force a compensating fallback the model
    # didn't ask for.
    followup = execute(
        "create_post",
        {
            "subdeaddit": "testsub",
            "title": "A real distinct topic about hiking trails",
            "content": "Completely unrelated content about hiking trails.",
        },
        _ctx(agent, run),
    )

    assert failed["ok"] is False
    assert followup["ok"] is True
    assert Post.query.count() == 1  # only the text post


def test_shared_hourly_rate_cap_across_tool_names(app, db_session, fake_adapter):
    _seed(db_session)
    provider = _make_provider(db_session)
    agent = _make_agent(
        db_session,
        config={
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "policy": "optional",
            }
        },
    )
    fake_adapter.enqueue_generate(_generation())
    run1 = _new_run(db_session, agent)
    first = execute(
        "create_image_post", IMAGE_ARGS, _ctx(agent, run1, deadline=Deadline.after(60))
    )

    run2 = _new_run(db_session, agent)
    second = execute(
        "create_post",
        {
            "subdeaddit": "testsub",
            "title": "Second post in a new run",
            "content": "Distinct unrelated body about cooking pasta.",
        },
        _ctx(agent, run2),
    )

    run3 = _new_run(db_session, agent)
    third = execute(
        "create_post",
        {
            "subdeaddit": "testsub",
            "title": "Third post hits the hourly cap",
            "content": "Yet another distinct unrelated body about kayaking.",
        },
        _ctx(agent, run3),
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert third["ok"] is False
    assert "recently" in third["error"]
    assert Post.query.count() == 2
    assert len(fake_adapter.generate_calls) == 1  # third never reached a handler


def test_duplicate_suppression_applies_to_image_posts(app, db_session, fake_adapter):
    _seed(db_session)
    provider = _make_provider(db_session)
    agent = _make_agent(
        db_session,
        config={
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "policy": "optional",
            }
        },
    )
    fake_adapter.enqueue_generate(_generation())
    run1 = _new_run(db_session, agent)
    first = execute(
        "create_image_post", IMAGE_ARGS, _ctx(agent, run1, deadline=Deadline.after(60))
    )

    run2 = _new_run(db_session, agent)
    near_dup = dict(IMAGE_ARGS)
    near_dup["title"] = IMAGE_ARGS["title"] + "!"
    second = execute(
        "create_image_post", near_dup, _ctx(agent, run2, deadline=Deadline.after(60))
    )

    assert first["ok"] is True
    assert second["ok"] is False
    assert "too similar" in second["error"]
    assert Post.query.count() == 1
    assert len(fake_adapter.generate_calls) == 1  # duplicate never reached the handler
