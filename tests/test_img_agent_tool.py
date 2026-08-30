"""The create_image_post agent tool, end to end.

Full-stack tests of deaddit.agents.executor.execute("create_image_post", ...)
against a real (tmp_path-rooted) media store, a FakeImageAdapter registered on
the deaddit.images.client seam, and a real SQLite-in-memory database. Nothing
here ever reaches fal.ai or Runware: every generation is served by
FakeImageAdapter, and the autouse conftest network guard would fail any real
egress attempt anyway.
"""

from __future__ import annotations

import random
import time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy.exc import SQLAlchemyError

from deaddit import create_app
from deaddit import db as _db
from deaddit.agents import tools_write
from deaddit.agents.executor import execute
from deaddit.agents.registry import ToolContext
from deaddit.images.client import register_adapter, reset_adapters
from deaddit.images.diversity import (
    diversity_ids,
    render_image_diversity,
    sample_image_diversity,
)
from deaddit.images.types import (
    Deadline,
    ImageGenerationResult,
    ImageProviderTransientError,
)
from deaddit.models import (
    Agent,
    AgentRun,
    Ban,
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
    _db.session.add_all(
        [
            User(username="alice", bio="curious alice", interests='["testing"]'),
            Subdeaddit(name="testsub", description="A test subdeaddit"),
        ]
    )
    _db.session.commit()
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


def _generation(**overrides) -> ImageGenerationResult:
    image = Image.new("RGB", (32, 32), color=(10, 20, 30))
    buf = BytesIO()
    image.save(buf, format="PNG")
    fields = {
        "request_id": "req-1",
        "image_url": None,
        "image_bytes": buf.getvalue(),
        "mime_type": "image/png",
        "width": 32,
        "height": 32,
    }
    fields.update(overrides)
    return ImageGenerationResult(**fields)


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


def _make_agent(db_session, *, config, tier="regular") -> Agent:
    agent = Agent(
        user_username="alice",
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


def _image_agent(db_session, provider, *, policy="optional", model=None):
    config = {
        "image_posts": {
            "enabled": True,
            "provider_id": provider.id if provider else 999,
            "policy": policy,
        }
    }
    if model:
        config["image_posts"]["model"] = model
    return _make_agent(db_session, config=config)


def _new_run(db_session, agent, *, prompt_metadata=None) -> AgentRun:
    for prev in AgentRun.query.filter_by(agent_id=agent.id, status="running").all():
        prev.status = "completed"
    run = AgentRun(
        agent_id=agent.id,
        persona_username=agent.user_username,
        trigger="manual",
        status="running",
        prompt_metadata=prompt_metadata,
    )
    db_session.add(run)
    db_session.commit()
    return run


def _ctx(agent, run, *, deadline=None) -> ToolContext:
    return ToolContext(
        agent=agent, run=run, user_username=agent.user_username, deadline=deadline
    )


def _stored_files(app) -> list[Path]:
    root = Path(app.config["GENERATED_IMAGES_ROOT"])
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def test_image_post_succeeds_and_gating_is_enforced_independently_of_tool_offering(
    app, db_session, fake_adapter
):
    # An agent with no image config is refused even when the tool is called
    # directly, without ever consulting the provider.
    plain = _make_agent(db_session, config={})
    run = _new_run(db_session, plain)
    refused = execute("create_image_post", IMAGE_ARGS, _ctx(plain, run))
    assert refused["ok"] is False and "not enabled" in refused["error"]
    assert Post.query.count() == 0
    assert fake_adapter.generate_calls == []
    row = ToolCall.query.order_by(ToolCall.id.desc()).first()
    assert row.name == "create_image_post" and row.ok is False

    plain.config = {"image_posts": {"enabled": False, "provider_id": 1}}
    db_session.commit()
    disabled = execute("create_image_post", IMAGE_ARGS, _ctx(plain, run))
    assert disabled["ok"] is False and "not enabled" in disabled["error"]

    # An image_only agent may not fall back to plain text posts.
    provider = _make_provider(db_session)
    plain.config = {
        "image_posts": {
            "enabled": True,
            "provider_id": provider.id,
            "policy": "image_only",
        }
    }
    db_session.commit()
    text = execute(
        "create_post",
        {"subdeaddit": "testsub", "title": "T", "content": "Body text"},
        _ctx(plain, run),
    )
    assert text["ok"] is False and "image posts" in text["error"]
    assert Post.query.count() == 0
    assert fake_adapter.generate_calls == []

    # The success path: one post, one image row, one ok ToolCall, two files.
    plain.config = {
        "image_posts": {
            "enabled": True,
            "provider_id": provider.id,
            "model": "fal-ai/flux-1/dev",
            "policy": "optional",
        }
    }
    db_session.commit()
    fake_adapter.enqueue_generate(_generation(request_id="req-42"))
    run = _new_run(db_session, plain)
    result = execute(
        "create_image_post", IMAGE_ARGS, _ctx(plain, run, deadline=Deadline.after(60))
    )

    assert result["ok"] is True and result["post_id"]
    assert Post.query.count() == 1
    image = PostImage.query.one()
    assert image.alt_text == IMAGE_ARGS["alt_text"]
    captured_prompt = fake_adapter.generate_calls[0]["prompt"]
    assert image.source_prompt == captured_prompt
    assert image.provider_snapshot == "Fal"
    assert image.provider_id == provider.id
    assert image.request_snapshot == "req-42"
    # A per-agent model override wins over the provider default.
    assert fake_adapter.generate_calls[0]["model_id"] == "fal-ai/flux-1/dev"
    assert image.model_snapshot == "fal-ai/flux-1/dev"

    root = Path(app.config["GENERATED_IMAGES_ROOT"])
    assert (root / image.original_path).is_file()
    assert (root / image.thumbnail_path).is_file()
    rows = ToolCall.query.filter_by(run_id=run.id).all()
    assert len(rows) == 1 and rows[0].ok is True

    # An optional-policy agent can still post text in a later run.
    later = _new_run(db_session, plain)
    text = execute(
        "create_post",
        {
            "subdeaddit": "testsub",
            "title": "A distinct text post",
            "content": "Completely different topic about gardening tools.",
        },
        _ctx(plain, later),
    )
    assert text["ok"] is True
    assert Post.query.count() == 2 and PostImage.query.count() == 1


def test_image_post_appends_diversity_suffix_and_records_full_prompt_and_ids(
    app, db_session, fake_adapter
):
    provider = _make_provider(db_session)
    agent = _image_agent(db_session, provider)
    run = _new_run(db_session, agent)
    fake_adapter.enqueue_generate(_generation())

    result = execute(
        "create_image_post",
        IMAGE_ARGS,
        _ctx(agent, run, deadline=Deadline.after(60)),
    )

    assert result["ok"] is True
    matrix = sample_image_diversity(random.Random(run.id))
    expected_suffix = render_image_diversity(matrix)
    captured_prompt = fake_adapter.generate_calls[0]["prompt"]
    assert captured_prompt == f"{IMAGE_ARGS['image_prompt']}\n\n{expected_suffix}"
    assert captured_prompt.startswith(IMAGE_ARGS["image_prompt"] + "\n\n")
    assert PostImage.query.one().source_prompt == captured_prompt
    assert result["image_diversity_ids"] == diversity_ids(matrix)


@pytest.mark.parametrize(
    ("direction_id", "is_photographic"),
    [("image.candid_snapshot", True), ("image.artwork_craft", False)],
)
def test_planned_image_direction_controls_downstream_medium(
    app, db_session, fake_adapter, direction_id, is_photographic
):
    provider = _make_provider(db_session)
    agent = _image_agent(db_session, provider)
    run = _new_run(
        db_session,
        agent,
        prompt_metadata={"direction_ids": [direction_id]},
    )
    fake_adapter.enqueue_generate(_generation())

    result = execute(
        "create_image_post",
        IMAGE_ARGS,
        _ctx(agent, run, deadline=Deadline.after(60)),
    )

    assert result["ok"] is True
    matrix = sample_image_diversity(
        random.Random(run.id),
        direction_id=direction_id,
        source_prompt=IMAGE_ARGS["image_prompt"],
    )
    assert matrix.is_photographic is is_photographic
    assert result["image_diversity_ids"] == diversity_ids(matrix)
    assert fake_adapter.generate_calls[0]["prompt"].endswith(
        render_image_diversity(matrix)
    )


def test_malformed_image_plan_uses_seeded_default_direction(
    app, db_session, fake_adapter
):
    provider = _make_provider(db_session)
    agent = _image_agent(db_session, provider)
    run = _new_run(
        db_session, agent, prompt_metadata={"direction_ids": ["image.unknown"]}
    )
    fake_adapter.enqueue_generate(_generation())

    result = execute(
        "create_image_post",
        IMAGE_ARGS,
        _ctx(agent, run, deadline=Deadline.after(60)),
    )

    assert result["ok"] is True
    matrix = sample_image_diversity(
        random.Random(run.id), source_prompt=IMAGE_ARGS["image_prompt"]
    )
    assert result["image_diversity_ids"] == diversity_ids(matrix)


def test_image_post_run_id_makes_composed_prompt_deterministic(
    app, db_session, fake_adapter, monkeypatch
):
    provider = _make_provider(db_session)
    agent = _image_agent(db_session, provider)
    run = SimpleNamespace(id=8675309)
    ctx = _ctx(agent, run)

    monkeypatch.setattr(tools_write, "_posts_created_this_run", lambda _: 0)
    monkeypatch.setattr(
        tools_write,
        "store_variants",
        lambda data, root: SimpleNamespace(
            original_path="original.png",
            thumbnail_path="thumbnail.png",
            mime_type="image/png",
            width=32,
            height=32,
            original_size=len(data),
        ),
    )
    monkeypatch.setattr(
        tools_write,
        "create_image_post",
        lambda **kwargs: SimpleNamespace(
            id=1,
            title=kwargs["title"],
            subdeaddit_name=kwargs["subdeaddit"],
        ),
    )
    fake_adapter.enqueue_generate(_generation(request_id="req-1"))
    fake_adapter.enqueue_generate(_generation(request_id="req-2"))
    global_state = random.getstate()
    first = tools_write._create_image_post(
        ctx, tools_write.CreateImagePostArgs(**IMAGE_ARGS)
    )
    assert random.getstate() == global_state
    second = tools_write._create_image_post(
        ctx, tools_write.CreateImagePostArgs(**IMAGE_ARGS)
    )
    assert random.getstate() == global_state

    matrix = sample_image_diversity(random.Random(run.id))
    expected_prompt = (
        f"{IMAGE_ARGS['image_prompt']}\n\n{render_image_diversity(matrix)}"
    )
    assert fake_adapter.generate_calls[0]["prompt"] == expected_prompt
    assert fake_adapter.generate_calls[1]["prompt"] == expected_prompt
    assert first["image_diversity_ids"] == diversity_ids(matrix)
    assert second["image_diversity_ids"] == diversity_ids(matrix)


def test_image_post_null_provider_uses_current_default(app, db_session, fake_adapter):
    provider = _make_provider(db_session, is_default=True)
    agent = _make_agent(
        db_session,
        config={
            "image_posts": {
                "enabled": True,
                "provider_id": None,
                "policy": "optional",
            }
        },
    )
    fake_adapter.enqueue_generate(_generation())

    result = execute(
        "create_image_post",
        IMAGE_ARGS,
        _ctx(agent, _new_run(db_session, agent)),
    )

    assert result["ok"] is True
    assert fake_adapter.generate_calls[0]["provider"] is provider


def test_image_post_failures_leave_no_post_no_files_and_share_the_post_budget(
    app, db_session, fake_adapter, monkeypatch
):
    provider = _make_provider(db_session)

    def attempt(agent, *, args=None, deadline=None, run=None):
        run = run or _new_run(db_session, agent)
        return execute(
            "create_image_post",
            args or IMAGE_ARGS,
            _ctx(agent, run, deadline=deadline or Deadline.after(60)),
        )

    def assert_nothing_persisted():
        assert Post.query.count() == 0
        assert PostImage.query.count() == 0
        assert _stored_files(app) == []

    agent = _image_agent(db_session, provider)

    # Rejections that must never reach (or pay) the provider.
    agent.config = {
        "image_posts": {"enabled": True, "provider_id": 999, "policy": "optional"}
    }
    db_session.commit()
    result = attempt(agent)
    assert result["ok"] is False and "provider" in result["error"]
    agent.config = {
        "image_posts": {
            "enabled": True,
            "provider_id": provider.id,
            "policy": "optional",
        }
    }
    db_session.commit()

    expired = attempt(agent, deadline=Deadline(expires_at=time.monotonic() - 5))
    assert expired["ok"] is False and "time remaining" in expired["error"]

    unknown = attempt(agent, args={**IMAGE_ARGS, "community": "nonexistent"})
    assert unknown["ok"] is False and "does not exist" in unknown["error"]
    assert fake_adapter.generate_calls == []
    assert_nothing_persisted()

    # A provider failure leaves nothing behind.
    fake_adapter.enqueue_error(
        ImageProviderTransientError("upstream 503"), method="generate"
    )
    failed = attempt(agent)
    assert failed["ok"] is False and "image generation failed" in failed["error"]
    assert_nothing_persisted()

    # A database failure after storage rolls the files back too.
    fake_adapter.enqueue_generate(_generation())
    with monkeypatch.context() as patched:
        patched.setattr(
            tools_write,
            "create_image_post",
            lambda **kwargs: (_ for _ in ()).throw(SQLAlchemyError("db exploded")),
        )
        db_failure = attempt(agent)
    assert db_failure["ok"] is False and "failed to save" in db_failure["error"]
    assert_nothing_persisted()
    assert ToolCall.query.order_by(ToolCall.id.desc()).first().ok is False

    # A ban landing mid-generation is caught by the commit-time recheck, and
    # the already-stored files are cleaned up.
    def generate_then_ban(*args, **kwargs):
        db_session.add(
            Ban(username="alice", subdeaddit_name="testsub", reason="mid-flight")
        )
        db_session.commit()
        return _generation()

    with monkeypatch.context() as patched:
        patched.setattr(tools_write, "generate_image", generate_then_ban)
        banned = attempt(agent)
    assert banned["ok"] is False and "banned" in banned["error"]
    assert_nothing_persisted()
    Ban.query.delete()
    db_session.commit()

    # Guardrails are shared across create_post and create_image_post: one post
    # per run, and an hourly cap counted across both tool names.
    fake_adapter.enqueue_generate(_generation())
    generations_before = len(fake_adapter.generate_calls)
    run = _new_run(db_session, agent)
    first = attempt(agent, run=run)
    assert first["ok"] is True
    same_run = execute(
        "create_post",
        {
            "subdeaddit": "testsub",
            "title": "Second post same run",
            "content": "This should be blocked by the shared per-run cap.",
        },
        _ctx(agent, run),
    )
    assert same_run["ok"] is False and "already created a post" in same_run["error"]
    assert Post.query.count() == 1
    assert len(fake_adapter.generate_calls) == generations_before + 1

    # A near-duplicate image post is suppressed before the provider is called.
    duplicate = attempt(agent, args={**IMAGE_ARGS, "title": IMAGE_ARGS["title"] + "!"})
    assert duplicate["ok"] is False and "too similar" in duplicate["error"]
    assert len(fake_adapter.generate_calls) == generations_before + 1

    second = execute(
        "create_post",
        {
            "subdeaddit": "testsub",
            "title": "Second post in a new run",
            "content": "Distinct unrelated body about cooking pasta.",
        },
        _ctx(agent, _new_run(db_session, agent)),
    )
    third = execute(
        "create_post",
        {
            "subdeaddit": "testsub",
            "title": "Third post hits the hourly cap",
            "content": "Yet another distinct unrelated body about kayaking.",
        },
        _ctx(agent, _new_run(db_session, agent)),
    )
    assert second["ok"] is True
    assert third["ok"] is False and "recently" in third["error"]
    assert Post.query.count() == 2
