"""Bounded image description, and its wiring into read_post.

Covers deaddit.llm.vision.describe_image directly (normalization/cap behaviour,
error propagation) and its use by deaddit.agents.tools_read._read_post (vision
success, every fallback path, removed/no-image suppression, usage labeling),
plus an explicit end-to-end check that base64 image data never reaches a
persisted AgentTurn or ToolCall row.
"""

from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from deaddit import create_app
from deaddit import db as _db
from deaddit.agents.executor import execute
from deaddit.agents.loop import run_once
from deaddit.agents.registry import ToolContext
from deaddit.extensions import db as ext_db
from deaddit.images.storage import store_variants
from deaddit.images.types import Deadline
from deaddit.llm.capabilities import set_manual_override, set_vision_manual_override
from deaddit.llm.errors import TransientLLMError
from deaddit.llm.vision import (
    MAX_DIMENSION,
    MAX_ENCODED_BYTES,
    ImageDescriptionError,
    describe_image,
)
from deaddit.models import (
    Agent,
    AgentRun,
    AgentTurn,
    LLMUsage,
    Post,
    PostImage,
    Subdeaddit,
    ToolCall,
    User,
)

API_URL = "http://llm.test/v1"
MODEL = "test-model"
DEFAULT_API_URL = "http://localhost/v1"
DEFAULT_MODEL = "llama3"


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


def _solid_png(color=(30, 60, 200), size=(64, 64)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def _seed(db_session):
    db_session.add_all(
        [
            User(username="alice", bio="curious alice", interests="[]"),
            Subdeaddit(name="testsub", description="A test subdeaddit"),
        ]
    )
    db_session.commit()


def _make_agent(db_session) -> Agent:
    agent = Agent(
        user_username="alice",
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
        "deadline": Deadline.after(60),
    }
    fields.update(overrides)
    return ToolContext(**fields)


def _make_image_post(app, db_session, *, removed=False) -> Post:
    stored = store_variants(_solid_png(), Path(app.config["GENERATED_IMAGES_ROOT"]))
    post = Post(
        title="A cat photo",
        content=None,
        subdeaddit_name="testsub",
        user="alice",
        removed=removed,
    )
    db_session.add(post)
    db_session.flush()
    db_session.add(
        PostImage(
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
    )
    db_session.commit()
    return post


def test_describe_image_sends_bounded_real_pixels_and_reports_its_own_usage(
    app, db_session, fake_llm
):
    fake_llm.enqueue_content("A vivid blue square.")

    description = describe_image(
        _solid_png(color=(10, 20, 230), size=(40, 40)),
        api_url=API_URL,
        model=MODEL,
        agent="alice",
    )

    assert description == "A vivid blue square."
    payload = fake_llm.requests[0]["payload"]
    assert payload["model"] == MODEL
    content = payload["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    url = content[1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")

    # The payload carries the actual pixels, not a placeholder: decoding it
    # yields an image close to the original solid color.
    decoded = Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1])))
    r, g, b = decoded.convert("RGB").getpixel((decoded.width // 2, decoded.height // 2))
    assert abs(r - 10) <= 12 and abs(g - 20) <= 12 and abs(b - 230) <= 12

    # The nested call is billed under its own action, not the caller's.
    usage = LLMUsage.query.order_by(LLMUsage.id.desc()).first()
    assert usage.action == "image_describe"
    assert usage.agent == "alice"

    # A large image is resized and re-encoded under both caps.
    fake_llm.enqueue_content("A large solid image.")
    buf = BytesIO()
    Image.new("RGB", (2400, 1600), color=(200, 50, 50)).save(buf, format="PNG")
    describe_image(buf.getvalue(), api_url=API_URL, model=MODEL)
    big_url = fake_llm.requests[1]["payload"]["messages"][0]["content"][1]["image_url"][
        "url"
    ]
    raw = base64.b64decode(big_url.split(",", 1)[1])
    assert len(raw) <= MAX_ENCODED_BYTES
    assert max(Image.open(BytesIO(raw)).size) <= MAX_DIMENSION

    # A whitespace-only reply is truthy at the transport level but carries no
    # description, so describe_image rejects it itself.
    fake_llm.enqueue_content("   ")
    with pytest.raises(ImageDescriptionError):
        describe_image(_solid_png(), api_url=API_URL, model=MODEL)

    fake_llm.enqueue_error(TransientLLMError("connection timed out"))
    with pytest.raises(TransientLLMError):
        describe_image(_solid_png(), api_url=API_URL, model=MODEL)

    before = len(fake_llm.requests)
    with pytest.raises(ImageDescriptionError):
        describe_image(b"not an image", api_url=API_URL, model=MODEL)
    assert len(fake_llm.requests) == before, "undecodable bytes never reach the model"


def test_read_post_describes_images_when_capable_and_always_falls_back_safely(
    app, db_session, fake_llm
):
    _seed(db_session)
    agent = _make_agent(db_session)

    def read(post, **ctx_overrides):
        run = _new_run(db_session, agent)
        return execute(
            "read_post", {"post_id": post.id}, _ctx(agent, run, **ctx_overrides)
        )

    # Without a vision-capable endpoint, no vision call is even attempted.
    post = _make_image_post(app, db_session)
    result = read(post)
    assert result["ok"] is True
    assert result["post"]["image"] == {
        "present": True,
        "description": post.image.source_prompt,
        "description_source": "generation_prompt",
    }
    assert fake_llm.requests == []

    set_vision_manual_override(API_URL, MODEL, True)
    fake_llm.enqueue_content("A bright blue square on a plain background.")
    described = read(post)
    assert described["post"]["image"] == {
        "present": True,
        "description": "A bright blue square on a plain background.",
        "description_source": "vision",
    }

    # Reading a post never fails because vision failed.
    fake_llm.enqueue_error(TransientLLMError("upstream exploded"))
    degraded = read(post)
    assert degraded["ok"] is True
    assert degraded["post"]["image"]["description_source"] == "generation_prompt"

    # Fallbacks that must not even reach the transport.
    before = len(fake_llm.requests)
    no_llm = read(post, llm_api_url=None, llm_model=None)
    assert no_llm["post"]["image"]["description_source"] == "generation_prompt"
    expired = read(post, deadline=Deadline(expires_at=-1.0))
    assert expired["post"]["image"]["description_source"] == "generation_prompt"

    (Path(app.config["GENERATED_IMAGES_ROOT"]) / post.image.original_path).unlink()
    missing_file = read(post)
    assert missing_file["post"]["image"]["description_source"] == "generation_prompt"
    assert len(fake_llm.requests) == before

    # Removed and text-only posts expose no image at all.
    removed = _make_image_post(app, db_session, removed=True)
    assert "image" not in read(removed)["post"]
    text_post = Post(
        title="A cat described in words only",
        content="No pixels here.",
        subdeaddit_name="testsub",
        user="alice",
    )
    db_session.add(text_post)
    db_session.commit()
    assert "image" not in read(text_post)["post"]
    assert len(fake_llm.requests) == before

    # Feed-shaped tools expose only a boolean, never a description: an agent
    # has to spend a read_post call to learn what an image actually shows.
    for tool, args, key in (
        ("browse_feed", {"subdeaddit": "testsub"}, "posts"),
        ("search", {"query": "cat", "type": "post"}, "results"),
        ("view_profile", {"username": "alice"}, "posts"),
    ):
        listing = execute(tool, args, _ctx(agent, _new_run(db_session, agent)))
        assert listing["ok"] is True
        entries = {entry["id"]: entry for entry in listing[key]}
        assert entries[post.id]["has_image"] is True
        assert entries[text_post.id]["has_image"] is False
        # A removed post never advertises an image where it is listed at all.
        assert entries.get(removed.id, {"has_image": False})["has_image"] is False
        assert "description" not in entries[post.id]
        assert "image" not in entries[post.id]

    # has_image is resolved in bulk: adding posts must not add queries.
    from sqlalchemy import event

    def count_feed_queries():
        statements = 0

        def tick(*_args, **_kwargs):
            nonlocal statements
            statements += 1

        event.listen(ext_db.engine, "before_cursor_execute", tick)
        try:
            feed = execute(
                "browse_feed",
                {"subdeaddit": "testsub"},
                _ctx(agent, _new_run(db_session, agent)),
            )
        finally:
            event.remove(ext_db.engine, "before_cursor_execute", tick)
        return statements, len(feed["posts"])

    small_queries, small_posts = count_feed_queries()
    for _ in range(8):
        _make_image_post(app, db_session)
    large_queries, large_posts = count_feed_queries()

    assert large_posts > small_posts
    assert large_queries == small_queries, "has_image must not be an N+1"


def test_full_run_passes_the_description_on_without_persisting_base64(
    app, db_session, fake_llm
):
    """A vision-derived description reaches the next agent turn while the
    base64 payload sent to the vision model never lands in any persisted
    AgentTurn or ToolCall row."""
    _seed(db_session)
    # The main run also needs the endpoint to pass the tools gate; the vision
    # override alone would otherwise leave supports_tools=False.
    set_manual_override(DEFAULT_API_URL, DEFAULT_MODEL, True)
    set_vision_manual_override(DEFAULT_API_URL, DEFAULT_MODEL, True)
    post = _make_image_post(app, db_session)
    _make_agent(db_session)

    def _tool_response(name, arguments, call_id):
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    }
                }
            ]
        }

    fake_llm.enqueue(_tool_response("read_post", {"post_id": post.id}, "call_1"))
    fake_llm.enqueue_content("A vivid blue square, evenly lit, no text visible.")
    fake_llm.enqueue(
        _tool_response("finish", {"summary": "done", "mood": "calm"}, "call_2")
    )

    run = run_once("alice")
    assert run.status == "completed"

    # The vision request really did carry base64 pixel data...
    vision_url = fake_llm.requests[1]["payload"]["messages"][0]["content"][1][
        "image_url"
    ]["url"]
    assert vision_url.startswith("data:image/jpeg;base64,")

    # ...but the description, not the pixels, is what reaches the next turn.
    final_messages = json.dumps(fake_llm.requests[2]["payload"]["messages"])
    assert "A vivid blue square, evenly lit, no text visible." in final_messages
    assert "base64" not in final_messages and "data:image" not in final_messages

    turns = AgentTurn.query.filter_by(run_id=run.id).all()
    assert turns
    for turn in turns:
        blob = json.dumps([turn.request_messages, turn.response_message], default=str)
        assert "base64" not in blob and "data:image" not in blob

    calls = ToolCall.query.filter_by(run_id=run.id).all()
    read_calls = [call for call in calls if call.name == "read_post"]
    assert len(read_calls) == 1
    for call in calls:
        blob = json.dumps([call.arguments, call.result], default=str)
        assert "base64" not in blob and "data:image" not in blob

    result = read_calls[0].result
    result = json.loads(result) if isinstance(result, str) else result
    assert result["post"]["image"]["description_source"] == "vision"
    assert (
        result["post"]["image"]["description"]
        == "A vivid blue square, evenly lit, no text visible."
    )

    # The ledger cleanly distinguishes the nested vision call's cost.
    usage = LLMUsage.query.filter_by(action="image_describe").one()
    assert usage.agent == "alice"
    assert (usage.api_url, usage.model) == (DEFAULT_API_URL, DEFAULT_MODEL)
