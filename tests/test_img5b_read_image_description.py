"""Coverage for bounded image description through read_post (plan 5B).

Covers deaddit.llm.vision.describe_image directly (normalization/cap
behaviour, error propagation) and its wiring into
deaddit.agents.tools_read._read_post (vision success, every fallback path,
removed/no-image suppression, usage labeling), plus an explicit end-to-end
check that base64 image data never reaches a persisted AgentTurn or
ToolCall row.
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
from deaddit.images.storage import store_variants
from deaddit.images.types import Deadline
from deaddit.llm.capabilities import set_manual_override, set_vision_manual_override
from deaddit.llm.errors import TransientLLMError
from deaddit.llm.vision import MAX_DIMENSION, MAX_ENCODED_BYTES, describe_image
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


def _seed_users_and_sub(db_session):
    db_session.add_all(
        [
            User(username="alice", bio="curious alice", interests="[]"),
            Subdeaddit(name="testsub", description="A test subdeaddit"),
        ]
    )
    db_session.commit()


def _make_agent(db_session, *, username="alice", config=None) -> Agent:
    agent = Agent(
        user_username=username,
        autonomy_tier="regular",
        is_enabled=True,
        status="idle",
        config=config or {},
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


def _make_image_post(app, db_session, *, image_bytes=None, removed=False) -> Post:
    root = app.config["GENERATED_IMAGES_ROOT"]
    stored = store_variants(image_bytes or _solid_png(), Path(root))
    post = Post(
        title="A cat photo",
        content=None,
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


def _make_text_post(db_session) -> Post:
    post = Post(
        title="Just words",
        content="No pixels here.",
        subdeaddit_name="testsub",
        user="alice",
    )
    db_session.add(post)
    db_session.commit()
    return post


# ---------------------------------------------------------------------------
# deaddit.llm.vision.describe_image


def test_describe_image_sends_actual_pixels_and_returns_description(
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
    encoded = url.split(",", 1)[1]
    decoded = Image.open(BytesIO(base64.b64decode(encoded)))
    r, g, b = decoded.convert("RGB").getpixel((decoded.width // 2, decoded.height // 2))
    assert abs(r - 10) <= 12
    assert abs(g - 20) <= 12
    assert abs(b - 230) <= 12


def test_describe_image_labels_usage_as_image_describe(app, db_session, fake_llm):
    fake_llm.enqueue_content("A blue square.")

    describe_image(_solid_png(), api_url=API_URL, model=MODEL, agent="alice")

    row = LLMUsage.query.order_by(LLMUsage.id.desc()).first()
    assert row is not None
    assert row.action == "image_describe"
    assert row.agent == "alice"


def test_describe_image_resizes_large_images_under_the_byte_cap(
    app, db_session, fake_llm
):
    fake_llm.enqueue_content("A large solid image.")
    big = Image.new("RGB", (2400, 1600), color=(200, 50, 50))
    buf = BytesIO()
    big.save(buf, format="PNG")

    describe_image(buf.getvalue(), api_url=API_URL, model=MODEL)

    url = fake_llm.requests[0]["payload"]["messages"][0]["content"][1]["image_url"][
        "url"
    ]
    encoded = url.split(",", 1)[1]
    raw = base64.b64decode(encoded)
    assert len(raw) <= MAX_ENCODED_BYTES
    decoded = Image.open(BytesIO(raw))
    assert max(decoded.size) <= MAX_DIMENSION


def test_describe_image_raises_on_whitespace_only_reply(app, db_session, fake_llm):
    from deaddit.llm.vision import ImageDescriptionError

    # A pure whitespace reply is truthy at the transport level (so the
    # client returns it normally) but strips to empty - describe_image's
    # own check is what catches this "no real description" case.
    fake_llm.enqueue_content("   ")

    with pytest.raises(ImageDescriptionError):
        describe_image(_solid_png(), api_url=API_URL, model=MODEL)


def test_describe_image_propagates_transport_errors(app, db_session, fake_llm):
    fake_llm.enqueue_error(TransientLLMError("connection timed out"))

    with pytest.raises(TransientLLMError):
        describe_image(_solid_png(), api_url=API_URL, model=MODEL)


def test_describe_image_raises_on_undecodable_bytes(app, db_session, fake_llm):
    from deaddit.llm.vision import ImageDescriptionError

    with pytest.raises(ImageDescriptionError):
        describe_image(b"not an image", api_url=API_URL, model=MODEL)
    assert fake_llm.requests == []  # never reached the transport


# ---------------------------------------------------------------------------
# read_post wiring


def test_read_post_returns_vision_description_when_capable(app, db_session, fake_llm):
    _seed_users_and_sub(db_session)
    set_vision_manual_override(API_URL, MODEL, True)
    post = _make_image_post(app, db_session)
    agent = _make_agent(db_session)
    run = _new_run(db_session, agent)
    fake_llm.enqueue_content("A bright blue square on a plain background.")

    result = execute("read_post", {"post_id": post.id}, _ctx(agent, run))

    assert result["ok"] is True
    image = result["post"]["image"]
    assert image == {
        "present": True,
        "description": "A bright blue square on a plain background.",
        "description_source": "vision",
    }


def test_read_post_falls_back_when_not_vision_capable(app, db_session, fake_llm):
    _seed_users_and_sub(db_session)
    post = _make_image_post(app, db_session)
    agent = _make_agent(db_session)
    run = _new_run(db_session, agent)

    result = execute("read_post", {"post_id": post.id}, _ctx(agent, run))

    assert result["ok"] is True
    image = result["post"]["image"]
    assert image["present"] is True
    assert image["description"] == post.image.source_prompt
    assert image["description_source"] == "generation_prompt"
    assert fake_llm.requests == []  # no vision call was ever attempted


def test_read_post_falls_back_when_vision_request_fails(app, db_session, fake_llm):
    _seed_users_and_sub(db_session)
    set_vision_manual_override(API_URL, MODEL, True)
    post = _make_image_post(app, db_session)
    agent = _make_agent(db_session)
    run = _new_run(db_session, agent)
    fake_llm.enqueue_error(TransientLLMError("upstream exploded"))

    result = execute("read_post", {"post_id": post.id}, _ctx(agent, run))

    assert result["ok"] is True  # reading a post never fails on vision errors
    image = result["post"]["image"]
    assert image["description"] == post.image.source_prompt
    assert image["description_source"] == "generation_prompt"


def test_read_post_falls_back_when_image_file_is_missing(app, db_session, fake_llm):
    _seed_users_and_sub(db_session)
    set_vision_manual_override(API_URL, MODEL, True)
    post = _make_image_post(app, db_session)
    agent = _make_agent(db_session)
    run = _new_run(db_session, agent)

    root = Path(app.config["GENERATED_IMAGES_ROOT"])
    (root / post.image.original_path).unlink()

    result = execute("read_post", {"post_id": post.id}, _ctx(agent, run))

    assert result["ok"] is True
    image = result["post"]["image"]
    assert image["description"] == post.image.source_prompt
    assert image["description_source"] == "generation_prompt"
    assert fake_llm.requests == []  # never reached the transport


def test_read_post_falls_back_when_ctx_has_no_llm_config(app, db_session, fake_llm):
    _seed_users_and_sub(db_session)
    set_vision_manual_override(API_URL, MODEL, True)
    post = _make_image_post(app, db_session)
    agent = _make_agent(db_session)
    run = _new_run(db_session, agent)

    result = execute(
        "read_post",
        {"post_id": post.id},
        _ctx(agent, run, llm_api_url=None, llm_model=None),
    )

    assert result["ok"] is True
    image = result["post"]["image"]
    assert image["description_source"] == "generation_prompt"
    assert fake_llm.requests == []


def test_read_post_omits_image_for_removed_post(app, db_session, fake_llm):
    _seed_users_and_sub(db_session)
    set_vision_manual_override(API_URL, MODEL, True)
    post = _make_image_post(app, db_session, removed=True)
    agent = _make_agent(db_session)
    run = _new_run(db_session, agent)

    result = execute("read_post", {"post_id": post.id}, _ctx(agent, run))

    assert result["ok"] is True
    assert "image" not in result["post"]
    assert fake_llm.requests == []


def test_read_post_omits_image_for_text_post(app, db_session, fake_llm):
    _seed_users_and_sub(db_session)
    post = _make_text_post(db_session)
    agent = _make_agent(db_session)
    run = _new_run(db_session, agent)

    result = execute("read_post", {"post_id": post.id}, _ctx(agent, run))

    assert result["ok"] is True
    assert "image" not in result["post"]


def test_read_post_expired_deadline_falls_back_without_calling_vision(
    app, db_session, fake_llm
):
    _seed_users_and_sub(db_session)
    set_vision_manual_override(API_URL, MODEL, True)
    post = _make_image_post(app, db_session)
    agent = _make_agent(db_session)
    run = _new_run(db_session, agent)
    expired = Deadline(expires_at=-1.0)

    result = execute(
        "read_post", {"post_id": post.id}, _ctx(agent, run, deadline=expired)
    )

    assert result["ok"] is True
    image = result["post"]["image"]
    assert image["description_source"] == "generation_prompt"
    assert fake_llm.requests == []


# ---------------------------------------------------------------------------
# Base64 must never reach persisted turns/tool calls


def _tool_call(call_id, name, arguments) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _tool_response(calls) -> dict:
    return {"choices": [{"message": {"role": "assistant", "tool_calls": calls}}]}


def test_full_run_vision_description_reaches_next_turn_without_persisting_base64(
    app, db_session, fake_llm
):
    """A vision-derived description reaches the next agent turn (plan 5B
    acceptance) while the base64 payload sent to the vision model never
    lands in any persisted AgentTurn or ToolCall row."""
    _seed_users_and_sub(db_session)
    # The main run also needs the endpoint to pass the tools gate; the
    # vision override alone would otherwise leave supports_tools=False.
    set_manual_override(DEFAULT_API_URL, DEFAULT_MODEL, True)
    set_vision_manual_override(DEFAULT_API_URL, DEFAULT_MODEL, True)
    post = _make_image_post(app, db_session)
    _make_agent(db_session)

    fake_llm.enqueue(
        _tool_response([_tool_call("call_1", "read_post", {"post_id": post.id})])
    )
    fake_llm.enqueue_content("A vivid blue square, evenly lit, no text visible.")
    fake_llm.enqueue(
        _tool_response(
            [_tool_call("call_2", "finish", {"summary": "done", "mood": "calm"})]
        )
    )

    run = run_once("alice")

    assert run.status == "completed"

    # The vision request really did carry base64 pixel data...
    vision_payload = fake_llm.requests[1]["payload"]
    vision_url = vision_payload["messages"][0]["content"][1]["image_url"]["url"]
    assert vision_url.startswith("data:image/jpeg;base64,")

    # ...but the description (not the pixels) is what reaches the next turn.
    final_request_payload = fake_llm.requests[2]["payload"]
    final_messages = json.dumps(final_request_payload["messages"])
    assert "A vivid blue square, evenly lit, no text visible." in final_messages
    assert "base64" not in final_messages
    assert "data:image" not in final_messages

    # Persisted rows: never any base64/data-URL fragment anywhere.
    turns = AgentTurn.query.filter_by(run_id=run.id).all()
    assert turns  # sanity: turns were actually recorded
    for turn in turns:
        blob = json.dumps([turn.request_messages, turn.response_message], default=str)
        assert "base64" not in blob
        assert "data:image" not in blob

    calls = ToolCall.query.filter_by(run_id=run.id).all()
    read_calls = [c for c in calls if c.name == "read_post"]
    assert len(read_calls) == 1
    for call in calls:
        blob = json.dumps([call.arguments, call.result], default=str)
        assert "base64" not in blob
        assert "data:image" not in blob

    read_result = read_calls[0].result
    read_result = (
        json.loads(read_result) if isinstance(read_result, str) else read_result
    )
    assert read_result["post"]["image"]["description_source"] == "vision"
    assert (
        read_result["post"]["image"]["description"]
        == "A vivid blue square, evenly lit, no text visible."
    )

    # And the ledger cleanly distinguishes the nested vision call's cost.
    usage_row = LLMUsage.query.filter_by(action="image_describe").one()
    assert usage_row.agent == "alice"
    assert usage_row.api_url == DEFAULT_API_URL
    assert usage_row.model == DEFAULT_MODEL
