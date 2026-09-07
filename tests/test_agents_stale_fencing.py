"""Regression coverage for stale agent-run publication fencing."""

from __future__ import annotations

from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

from PIL import Image

from deaddit import db
from deaddit.agents.executor import execute
from deaddit.agents.registry import ToolContext
from deaddit.images.types import ImageGenerationResult
from deaddit.models import (
    Agent,
    AgentRun,
    GeneratedWebsite,
    ImageProvider,
    Post,
    PostImage,
)
from deaddit.runtime.wakes import WakeScheduler
from deaddit.websites.generator import WebsiteGenerationResult


_VALID_WEBSITE_HTML = "<!doctype html><html><body><h1>hi</h1></body></html>"


def _agent_and_run(db_session, *, config: dict) -> tuple[Agent, AgentRun]:
    agent = Agent(
        user_username="bob",
        autonomy_tier="regular",
        is_enabled=True,
        status="running",
        config=config,
    )
    db_session.add(agent)
    db_session.commit()
    run = AgentRun(
        agent_id=agent.id,
        persona_username="bob",
        trigger="manual",
        status="running",
        started_at=datetime.utcnow(),
        token_usage={},
    )
    db_session.add(run)
    db_session.commit()
    return agent, run


def _age_out_seeded_posts(db_session):
    """Move seeded posts outside the hourly post rate-limit window."""
    cutoff = datetime.utcnow() - timedelta(hours=2)
    for post in db_session.query(Post).all():
        post.created_at = cutoff
    db_session.commit()


def _recover_and_replace(app, db_session, run: AgentRun) -> AgentRun:
    run.started_at = datetime.utcnow() - timedelta(seconds=400)
    db_session.commit()
    db_session.expire_all()
    assert WakeScheduler(app)._interrupt_stale_runs(datetime.utcnow()) == 1
    db_session.expire_all()
    agent = db_session.get(Agent, run.agent_id)
    replacement = AgentRun(
        agent_id=agent.id,
        persona_username="bob",
        trigger="schedule",
        status="running",
        started_at=datetime.utcnow(),
        token_usage={},
    )
    agent.status = "running"
    db_session.add(replacement)
    db_session.commit()
    return replacement


def _replacement_context(agent: Agent, replacement: AgentRun) -> ToolContext:
    return ToolContext(
        agent=agent,
        run=replacement,
        user_username="bob",
        post_intent="browse",
    )


def _assert_replacement_can_use_session(
    db_session, agent: Agent, replacement: AgentRun
) -> None:
    result = execute("view_profile", {}, _replacement_context(agent, replacement))
    assert result["ok"] is True
    assert db_session.get(AgentRun, replacement.id).status == "running"


def test_stale_image_handler_cannot_publish_after_recovery(
    seeded_db, db_session, app, monkeypatch, tmp_path
):
    app.config.update(
        GENERATED_IMAGES_ROOT=str(tmp_path / "images"),
        GENERATED_WEBSITES_ROOT=str(tmp_path / "websites"),
    )
    _age_out_seeded_posts(db_session)
    provider = ImageProvider(
        name="fake",
        provider_type="fake",
        credential_env="FAKE_IMAGE_KEY",
        default_model="fake-model",
        is_enabled=True,
    )
    db_session.add(provider)
    db_session.commit()
    agent, run = _agent_and_run(
        db_session,
        config={
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "policy": "optional",
            }
        },
    )
    replacement_box: list[AgentRun] = []
    image = Image.new("RGB", (8, 8), color=(10, 20, 30))
    image_bytes = BytesIO()
    image.save(image_bytes, format="PNG")

    def generate(*args, **kwargs):
        replacement_box.append(_recover_and_replace(app, db_session, run))
        return ImageGenerationResult(
            request_id="stale-image",
            image_url=None,
            image_bytes=image_bytes.getvalue(),
            mime_type="image/png",
            width=8,
            height=8,
        )

    from deaddit.agents import tools_write

    monkeypatch.setattr(tools_write, "generate_image", generate)
    result = execute(
        "create_image_post",
        {
            "community": "testsub",
            "title": "stale image",
            "content": "late",
            "image_prompt": "a test image",
            "alt_text": "test image",
        },
        ToolContext(
            agent=agent,
            run=run,
            user_username="bob",
            post_intent="post",
            llm_model="fake-llm",
        ),
    )

    assert result["kind"] == "rejected"
    assert Post.query.count() == 3
    assert PostImage.query.count() == 0
    assert db_session.get(AgentRun, run.id).status == "interrupted"
    replacement = replacement_box[0]
    assert db_session.get(AgentRun, replacement.id).status == "running"
    assert not [
        path
        for path in Path(app.config["GENERATED_IMAGES_ROOT"]).rglob("*")
        if path.is_file()
    ]
    _assert_replacement_can_use_session(db_session, agent, replacement)


def test_stale_website_handler_cannot_publish_after_recovery(
    seeded_db, db_session, app, monkeypatch, tmp_path
):
    app.config.update(
        GENERATED_IMAGES_ROOT=str(tmp_path / "images"),
        GENERATED_WEBSITES_ROOT=str(tmp_path / "websites"),
    )
    _age_out_seeded_posts(db_session)
    agent, run = _agent_and_run(
        db_session,
        config={"website_posts": {"enabled": True, "policy": "optional"}},
    )
    replacement_box: list[AgentRun] = []

    def generate(**kwargs):
        replacement_box.append(_recover_and_replace(app, db_session, run))
        return WebsiteGenerationResult(
            html=_VALID_WEBSITE_HTML,
            request_id="stale-website",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            finish_reason="stop",
            api_url="https://llm.example.test/v1",
            model="fake-llm",
            diversity_ids={},
        )

    from deaddit.agents import tools_write

    monkeypatch.setattr(tools_write, "generate_website_html", generate)
    result = execute(
        "create_website",
        {
            "community": "testsub",
            "title": "stale website",
            "content": "late",
            "hostname_hint": "stale-test.example",
            "website_description": (
                "A small test website with enough detail to satisfy the brief, "
                "carry a distinctive layout, and make a useful page for visitors."
            ),
            "page_name_hint": "index",
        },
        ToolContext(
            agent=agent,
            run=run,
            user_username="bob",
            post_intent="post",
            llm_api_url="https://llm.example.test/v1",
            llm_model="fake-llm",
        ),
    )

    assert result["kind"] == "rejected"
    assert Post.query.count() == 3
    assert GeneratedWebsite.query.count() == 0
    assert db_session.get(AgentRun, run.id).status == "interrupted"
    replacement = replacement_box[0]
    assert db_session.get(AgentRun, replacement.id).status == "running"
    assert not [
        path
        for path in Path(app.config["GENERATED_WEBSITES_ROOT"]).rglob("*")
        if path.is_file()
    ]
    _assert_replacement_can_use_session(db_session, agent, replacement)
