"""The create_website agent tool, end to end.

Full-stack tests of deaddit.agents.executor.execute("create_website", ...)
against a real (tmp_path-rooted) website store, a FakeProvider registered on
the deaddit.llm transport seam (tests/conftest.py's fake_llm fixture), and a
real SQLite-in-memory database. Nothing here ever reaches a real LLM
endpoint: every generation is served by FakeProvider, and the autouse
_network_guard would fail any real egress attempt anyway. Mirrors
tests/test_img_agent_tool.py's conventions.
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

import pytest

import deaddit.settings.service as settings_service
from deaddit import create_app
from deaddit import db as _db
from deaddit.agents import tools_write
from deaddit.agents.executor import execute
from deaddit.agents.registry import ToolContext
from deaddit.config import Config
from deaddit.images.client import register_adapter, reset_adapters
from deaddit.images.types import Deadline, ImageGenerationResult
from deaddit.models import (
    Agent,
    AgentRun,
    GeneratedWebsite,
    ImageProvider,
    Post,
    Subdeaddit,
    ToolCall,
    User,
)
from deaddit.websites.generator import (
    WebsiteGenerationResult,
)
from tests.fakes import FakeImageAdapter

API_URL = "http://caller-endpoint.test/v1"
API_KEY = "sk-super-secret-caller-key-should-never-leak"
MODEL = "caller-model"

VALID_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Aurora Map</title>
<style>body { font-family: sans-serif; }</style>
</head>
<body>
<h1>Aurora Map</h1>
<p>Live views of the northern lights, updated hourly by volunteer spotters.</p>
</body>
</html>"""

_DESCRIPTION = (
    "A cozy fictional aurora-watching community site with a live map, "
    "a spotter leaderboard, and a blog of recent sightings. The persona "
    "landed on the single page showing tonight's forecast and a gallery "
    "of user-submitted photos from observers across the northern region."
)

WEBSITE_ARGS = {
    "community": "testsub",
    "title": "This aurora map is mesmerizing",
    "website_description": _DESCRIPTION,
    "hostname_hint": "www.fake-observatory.com",
    "page_name_hint": "aurora-map",
}


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "TESTING": True,
            "GENERATED_WEBSITES_ROOT": str(tmp_path / "websites"),
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
            User(username="bob", bio="bob builds things", interests='["coding"]'),
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


@pytest.fixture(autouse=True)
def _isolated_settings_cache():
    """Isolate the process-global Config TTL cache between tests (mirrors
    test_a6_config_secrets.py): the banned-words tests write Setting rows
    that must never leak into another test's database view."""
    settings_service.clear()
    yield
    settings_service.clear()


@pytest.fixture()
def fake_adapter(monkeypatch):
    """A FakeImageAdapter, for tests that mix create_website with create_image_post."""
    adapter = FakeImageAdapter()
    register_adapter("fal", adapter)
    monkeypatch.setenv("FALAI_API_KEY", "test-secret-value")
    return adapter


def _make_agent(db_session, *, config, tier="regular", user_username="alice") -> Agent:
    agent = Agent(
        user_username=user_username,
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


def _website_agent(
    db_session, *, policy="optional", tier="regular", user_username="alice"
) -> Agent:
    return _make_agent(
        db_session,
        config={"website_posts": {"enabled": True, "policy": policy}},
        tier=tier,
        user_username=user_username,
    )


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
        agent=agent,
        run=run,
        user_username=agent.user_username,
        llm_api_url=API_URL,
        llm_api_key=API_KEY,
        llm_model=MODEL,
        deadline=deadline,
    )


def _stored_files(app) -> list[Path]:
    root = Path(app.config["GENERATED_WEBSITES_ROOT"]) / "pages"
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def _image_generation(**overrides) -> ImageGenerationResult:
    fields = {
        "request_id": "img-req-1",
        "image_url": None,
        "image_bytes": b"\x89PNG\r\n\x1a\n" + b"0" * 64,
        "mime_type": "image/png",
        "width": 8,
        "height": 8,
    }
    fields.update(overrides)
    return ImageGenerationResult(**fields)


def test_website_post_succeeds_and_gating_is_enforced_independently_of_tool_offering(
    app, db_session, fake_llm
):
    # An agent with no website config is refused even when the tool is
    # called directly, without ever consulting the LLM.
    plain = _make_agent(db_session, config={})
    run = _new_run(db_session, plain)
    refused = execute("create_website", WEBSITE_ARGS, _ctx(plain, run))
    assert refused["ok"] is False and "not enabled" in refused["error"]
    assert Post.query.count() == 0
    assert fake_llm.requests == []
    row = ToolCall.query.order_by(ToolCall.id.desc()).first()
    assert row.name == "create_website" and row.ok is False

    # A forged call while explicitly disabled is rejected the same way.
    plain.config = {"website_posts": {"enabled": False, "policy": "optional"}}
    db_session.commit()
    disabled = execute("create_website", WEBSITE_ARGS, _ctx(plain, run))
    assert disabled["ok"] is False and "not enabled" in disabled["error"]
    assert fake_llm.requests == []

    # image_only + website_only is an invalid, mutually exclusive
    # configuration: the registry would never offer any post tool for it,
    # so the executor must reject a forged call to any of the three tools
    # rather than honoring one side.
    plain.config = {
        "image_posts": {"enabled": True, "provider_id": 1, "policy": "image_only"},
        "website_posts": {"enabled": True, "policy": "website_only"},
    }
    db_session.commit()
    conflict_website = execute("create_website", WEBSITE_ARGS, _ctx(plain, run))
    assert conflict_website["ok"] is False
    assert Post.query.count() == 0
    assert fake_llm.requests == []
    conflict_image = execute(
        "create_image_post",
        {
            "community": "testsub",
            "title": "T",
            "image_prompt": "a cat",
            "alt_text": "a cat",
        },
        _ctx(plain, run),
    )
    assert conflict_image["ok"] is False
    conflict_text = execute(
        "create_post",
        {"subdeaddit": "testsub", "title": "T", "content": "Body"},
        _ctx(plain, run),
    )
    assert conflict_text["ok"] is False
    assert Post.query.count() == 0

    # The success path: one post, one website row, exactly one ok ToolCall,
    # exactly one billed LLM call, and the stored HTML file on disk.
    agent = plain
    agent.config = {"website_posts": {"enabled": True, "policy": "optional"}}
    db_session.commit()
    fake_llm.enqueue_content(VALID_HTML, finish_reason="stop")
    run = _new_run(db_session, agent)
    result = execute(
        "create_website", WEBSITE_ARGS, _ctx(agent, run, deadline=Deadline.after(120))
    )

    assert result["ok"] is True
    assert set(result) == {
        "ok",
        "post_id",
        "title",
        "subdeaddit",
        "website_url",
        "hostname",
        "hint",
        "website_diversity_ids",
    }
    assert result["post_id"]
    assert result["title"] == WEBSITE_ARGS["title"]
    assert result["subdeaddit"] == "testsub"
    assert result["hostname"] == "www.fake-observatory.com"
    assert result["website_url"] == "/out/www.fake-observatory.com/aurora-map.html"
    assert result["hint"]

    assert Post.query.count() == 1
    website = GeneratedWebsite.query.one()
    assert website.hostname == "www.fake-observatory.com"
    assert website.public_path == "www.fake-observatory.com/aurora-map.html"
    provenance_prefix = f"{_DESCRIPTION}\n\n"
    assert website.source_description.startswith(provenance_prefix)
    assert json.loads(website.source_description[len(provenance_prefix) :]) == {
        "website_diversity_ids": {
            axis: list(ids) for axis, ids in result["website_diversity_ids"].items()
        }
    }
    assert website.creator_username_snapshot == "alice"
    assert website.agent_id == agent.id
    assert website.agent_run_id == run.id
    assert website.api_url_snapshot == API_URL
    assert website.model_snapshot == MODEL
    assert website.request_id
    assert website.prompt_tokens == 1
    assert website.completion_tokens == 1
    assert website.total_tokens == 2
    assert website.finish_reason == "stop"
    assert API_KEY not in repr(website)

    root = Path(app.config["GENERATED_WEBSITES_ROOT"])
    assert (root / website.storage_path).is_file()
    assert (root / website.storage_path).read_text() == VALID_HTML

    assert len(fake_llm.requests) == 1
    rows = ToolCall.query.filter_by(run_id=run.id).all()
    assert len(rows) == 1 and rows[0].ok is True

    # website_only excludes create_post as a fallback, even after a
    # successful website post in an earlier run.
    only_agent = _website_agent(db_session, policy="website_only", user_username="bob")
    later = _new_run(db_session, only_agent)
    text = execute(
        "create_post",
        {"subdeaddit": "testsub", "title": "T2", "content": "Body text"},
        _ctx(only_agent, later),
    )
    assert text["ok"] is False and "website posts" in text["error"]


def test_create_website_provenance_records_diversity_ids(app, db_session, monkeypatch):
    agent = _website_agent(db_session)
    run = _new_run(
        db_session,
        agent,
        prompt_metadata={"direction_ids": ["website.personal_blog"]},
    )
    diversity_ids = {
        "genres": ("genre.newsroom", "genre.portfolio"),
        "layouts": ("layout.editorial_grid", "layout.split_hero"),
        "moods": ("mood.neon_night", "mood.deep_ocean"),
        "typography": ("type.modern_grotesk", "type.display_serif"),
        "rhythms": ("rhythm.discovery",),
    }

    def fake_generate(**kwargs):
        assert kwargs["rng"].getstate() == random.Random(run.id).getstate()
        assert kwargs["direction_id"] == "website.personal_blog"
        return WebsiteGenerationResult(
            html=VALID_HTML,
            request_id="website-request-1",
            prompt_tokens=3,
            completion_tokens=4,
            total_tokens=7,
            finish_reason="stop",
            api_url=API_URL,
            model=MODEL,
            diversity_ids=diversity_ids,
        )

    monkeypatch.setattr(tools_write, "generate_website_html", fake_generate)
    result = execute(
        "create_website",
        WEBSITE_ARGS,
        _ctx(agent, run, deadline=Deadline.after(120)),
    )

    assert result["ok"] is True
    assert result["website_diversity_ids"] == diversity_ids
    website = GeneratedWebsite.query.one()
    provenance = json.dumps(
        {"website_diversity_ids": diversity_ids},
        sort_keys=True,
        separators=(",", ":"),
    )
    assert website.source_description == f"{_DESCRIPTION}\n\n{provenance}"
    assert website.source_description.endswith(f"\n\n{provenance}")


def test_malformed_website_plan_uses_sampler_default(app, db_session, monkeypatch):
    agent = _website_agent(db_session)
    run = _new_run(
        db_session,
        agent,
        prompt_metadata={"direction_ids": ["website.unknown"]},
    )

    def fake_generate(**kwargs):
        assert kwargs["direction_id"] is None
        return WebsiteGenerationResult(
            html=VALID_HTML,
            request_id="website-request-default",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            finish_reason="stop",
            api_url=API_URL,
            model=MODEL,
            diversity_ids={},
        )

    monkeypatch.setattr(tools_write, "generate_website_html", fake_generate)
    result = execute(
        "create_website",
        WEBSITE_ARGS,
        _ctx(agent, run, deadline=Deadline.after(120)),
    )
    assert result["ok"] is True


def test_website_post_failures_leave_no_post_no_files_and_share_the_post_budget(
    app, db_session, fake_llm, fake_adapter, monkeypatch
):
    agent = _website_agent(db_session)

    def attempt(agent, *, args=None, deadline=None, run=None):
        run = run or _new_run(db_session, agent)
        return execute(
            "create_website",
            args or WEBSITE_ARGS,
            _ctx(agent, run, deadline=deadline or Deadline.after(120)),
        )

    def assert_nothing_persisted():
        assert Post.query.count() == 0
        assert GeneratedWebsite.query.count() == 0
        assert _stored_files(app) == []

    # Rejections that must never reach (or pay) the LLM.
    unknown = attempt(agent, args={**WEBSITE_ARGS, "community": "nonexistent"})
    assert unknown["ok"] is False and "does not exist" in unknown["error"]
    assert fake_llm.requests == []
    assert_nothing_persisted()

    expired = attempt(agent, deadline=Deadline(expires_at=time.monotonic() - 5))
    assert expired["ok"] is False and "time remaining" in expired["error"]
    assert fake_llm.requests == []
    assert_nothing_persisted()

    # A length-truncated response is never published, and the API key never
    # leaks into the error.
    fake_llm.enqueue_content("<!doctype html><html>", finish_reason="length")
    run = _new_run(db_session, agent)
    truncated = attempt(agent, run=run)
    assert truncated["ok"] is False
    assert "length" in truncated["error"] or "stopped" in truncated["error"]
    assert API_KEY not in truncated["error"]
    assert "<html" not in truncated["error"]
    assert_nothing_persisted()

    # A second attempt in the same run is blocked by the one-billed-attempt
    # guard *without* a second LLM call, even though the first attempt
    # failed validation rather than succeeding.
    blocked = attempt(agent, run=run)
    assert blocked["ok"] is False
    assert "already attempted" in blocked["error"]
    assert len(fake_llm.requests) == 1
    assert_nothing_persisted()

    # Malformed (non-fenced but structurally invalid) HTML in a fresh run
    # also fails cleanly and never leaks the raw document.
    fake_llm.enqueue_content(
        "just some plain text, not a document", finish_reason="stop"
    )
    run2 = _new_run(db_session, agent)
    invalid = attempt(agent, run=run2)
    assert invalid["ok"] is False
    assert "plain text" not in invalid["error"]
    assert_nothing_persisted()

    # A storage failure after a successful, billed generation leaves no
    # post/row/file and still reports the attempt as billed.
    fake_llm.enqueue_content(VALID_HTML, finish_reason="stop")
    run3 = _new_run(db_session, agent)
    with monkeypatch.context() as patched:
        patched.setattr(
            tools_write,
            "store_website",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )
        storage_failed = attempt(agent, run=run3)
    assert storage_failed["ok"] is False and "storage" in storage_failed["error"]
    assert_nothing_persisted()
    blocked_after_storage_failure = attempt(agent, run=run3)
    assert blocked_after_storage_failure["ok"] is False
    assert "already attempted" in blocked_after_storage_failure["error"]

    # Guardrails are shared across create_post, create_image_post, and
    # create_website: one post per run...
    fake_llm.enqueue_content(VALID_HTML, finish_reason="stop")
    run5 = _new_run(db_session, agent)
    first = attempt(agent, run=run5)
    assert first["ok"] is True
    same_run = execute(
        "create_post",
        {
            "subdeaddit": "testsub",
            "title": "Second post same run",
            "content": "This should be blocked by the shared per-run cap.",
        },
        _ctx(agent, run5),
    )
    assert same_run["ok"] is False and "already created a post" in same_run["error"]
    assert Post.query.count() == 1

    # A public_path collision at commit time (state can change during the
    # slow generation, e.g. two runs landing on the same fictional address)
    # is caught by create_website_post's own unique-constraint recheck, and
    # *that* function - not this tool - deletes the just-stored file. Force
    # the race by bypassing the pre-check so allocate_public_path reuses the
    # already-published path; this exercises the real cleanup path rather
    # than a stubbed one, so no double-delete bug can hide behind a mock.
    baseline_posts = Post.query.count()
    baseline_websites = GeneratedWebsite.query.count()
    baseline_files = len(_stored_files(app))
    fake_llm.enqueue_content(VALID_HTML, finish_reason="stop")
    run6 = _new_run(db_session, agent)
    collision_args = {
        **WEBSITE_ARGS,
        "title": "A totally different headline about the same fictional map",
        "content": "Distinct commentary unrelated to the earlier post's wording.",
    }
    with monkeypatch.context() as patched:
        patched.setattr(tools_write, "_is_public_path_taken", lambda public_path: False)
        collision = attempt(agent, args=collision_args, run=run6)
    assert collision["ok"] is False and "failed to save" in collision["error"]
    assert Post.query.count() == baseline_posts
    assert GeneratedWebsite.query.count() == baseline_websites
    assert len(_stored_files(app)) == baseline_files

    # ...and one shared hourly cap across all three tools.
    provider = ImageProvider(
        name="Fal",
        provider_type="fal",
        credential_env="FALAI_API_KEY",
        default_model="fal-ai/flux-1/schnell",
        is_enabled=True,
    )
    db_session.add(provider)
    db_session.commit()
    mixed = _make_agent(
        db_session,
        config={
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "policy": "optional",
            },
            "website_posts": {"enabled": True, "policy": "optional"},
        },
        user_username="bob",
    )
    text_post = execute(
        "create_post",
        {
            "subdeaddit": "testsub",
            "title": "First hourly post",
            "content": "Distinct unrelated body about kayaking gear reviews.",
        },
        _ctx(mixed, _new_run(db_session, mixed)),
    )
    assert text_post["ok"] is True

    fake_llm.enqueue_content(VALID_HTML, finish_reason="stop")
    website_post = execute(
        "create_website",
        {**WEBSITE_ARGS, "title": "A completely different fictional site"},
        _ctx(mixed, _new_run(db_session, mixed), deadline=Deadline.after(120)),
    )
    assert website_post["ok"] is True

    fake_adapter.enqueue_generate(_image_generation())
    hourly_capped = execute(
        "create_image_post",
        {
            "community": "testsub",
            "title": "Third post hits the hourly cap",
            "image_prompt": "a distinct unrelated cat photo prompt",
            "alt_text": "a cat",
        },
        _ctx(mixed, _new_run(db_session, mixed)),
    )
    assert hourly_capped["ok"] is False and "recently" in hourly_capped["error"]
    assert fake_adapter.generate_calls == []


def test_website_post_duplicate_guardrail_blocks_before_generation(
    app, db_session, fake_llm
):
    agent = _website_agent(db_session)
    fake_llm.enqueue_content(VALID_HTML, finish_reason="stop")
    first = execute(
        "create_website",
        WEBSITE_ARGS,
        _ctx(agent, _new_run(db_session, agent), deadline=Deadline.after(120)),
    )
    assert first["ok"] is True
    assert len(fake_llm.requests) == 1

    duplicate = execute(
        "create_website",
        WEBSITE_ARGS,
        _ctx(agent, _new_run(db_session, agent), deadline=Deadline.after(120)),
    )
    assert duplicate["ok"] is False and "too similar" in duplicate["error"]
    # The duplicate guardrail runs in the executor before dispatch, so no
    # second (billed) generation attempt is made.
    assert len(fake_llm.requests) == 1


@pytest.mark.parametrize(
    ("image_cfg", "website_cfg", "expected"),
    [
        ({}, {}, {"create_post"}),
        (
            {"enabled": True, "policy": "optional"},
            {},
            {"create_post", "create_image_post"},
        ),
        (
            {},
            {"enabled": True, "policy": "optional"},
            {"create_post", "create_website"},
        ),
        (
            {"enabled": True, "policy": "optional"},
            {"enabled": True, "policy": "optional"},
            {"create_post", "create_image_post", "create_website"},
        ),
        ({"enabled": True, "policy": "image_only"}, {}, {"create_image_post"}),
        (
            {"enabled": True, "policy": "image_only"},
            {"enabled": True, "policy": "optional"},
            {"create_image_post"},
        ),
        ({}, {"enabled": True, "policy": "website_only"}, {"create_website"}),
        (
            {"enabled": True, "policy": "optional"},
            {"enabled": True, "policy": "website_only"},
            {"create_website"},
        ),
        (
            {"enabled": True, "policy": "image_only"},
            {"enabled": True, "policy": "website_only"},
            set(),
        ),
    ],
)
def test_post_policy_truth_table_matches_direct_helper_and_wire_specs(
    image_cfg, website_cfg, expected
):
    """The direct policy helper and both registry surfaces must agree."""
    from deaddit.agents.registry import (
        image_posts_config,
        offered_post_tool_names,
        specs_for,
        tools_for,
        website_posts_config,
    )

    config = {}
    if image_cfg:
        config["image_posts"] = {"provider_id": 1, **image_cfg}
    if website_cfg:
        config["website_posts"] = website_cfg
    agent = type("AgentStub", (), {"config": config})()
    normalized_image = image_posts_config(agent)
    normalized_website = website_posts_config(agent)
    assert (
        set(offered_post_tool_names(normalized_image, normalized_website)) == expected
    )

    post_names = {"create_post", "create_image_post", "create_website"}
    assert {
        tool.name for tool in tools_for("regular", agent=agent)
    } & post_names == expected
    assert {
        spec.name for spec in specs_for("regular", agent=agent)
    } & post_names == expected


def test_create_website_wire_schema_and_registration_match_contract():
    from deaddit.agents.registry import AutonomyTier, RateClass, get

    tool = get("create_website")
    assert tool.min_tier is AutonomyTier.REGULAR
    assert tool.rate_class is RateClass.WRITE
    wire = {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters.model_json_schema(),
        },
    }
    function = wire["function"]
    assert function["name"] == "create_website"
    assert "website_description" in function["description"].lower()
    assert "generator brief" in function["description"].lower()
    properties = function["parameters"]["properties"]
    assert set(properties) == {
        "community",
        "title",
        "content",
        "website_description",
        "hostname_hint",
        "page_name_hint",
        "post_type",
    }
    assert properties["community"]["minLength"] == 1
    assert properties["community"]["maxLength"] == 50
    assert properties["title"]["minLength"] == 1
    assert properties["title"]["maxLength"] == 300
    assert properties["content"]["anyOf"][0]["maxLength"] == 20000
    assert properties["website_description"]["minLength"] == 100
    assert properties["website_description"]["maxLength"] == 12000
    assert properties["hostname_hint"]["minLength"] == 3
    assert properties["hostname_hint"]["maxLength"] == 253
    assert properties["page_name_hint"]["minLength"] == 1
    assert properties["page_name_hint"]["maxLength"] == 120
    assert properties["post_type"]["anyOf"][0]["maxLength"] == 50
    assert set(function["parameters"]["required"]) == {
        "community",
        "title",
        "website_description",
        "hostname_hint",
        "page_name_hint",
    }


def test_forged_conflicting_website_call_is_one_safe_audit_rejection(
    app, db_session, fake_llm
):
    disabled_agent = _make_agent(db_session, config={}, user_username="bob")
    disabled_run = _new_run(db_session, disabled_agent)
    disabled = execute(
        "create_website", WEBSITE_ARGS, _ctx(disabled_agent, disabled_run)
    )
    assert disabled["ok"] is False
    assert "not enabled" in disabled["error"]
    assert fake_llm.requests == []
    disabled_rows = ToolCall.query.filter_by(run_id=disabled_run.id).all()
    assert len(disabled_rows) == 1
    assert disabled_rows[0].name == "create_website"
    assert disabled_rows[0].ok is False
    assert disabled_rows[0].error == disabled["error"]

    agent = _make_agent(
        db_session,
        config={
            "image_posts": {
                "enabled": True,
                "provider_id": 1,
                "policy": "image_only",
            },
            "website_posts": {"enabled": True, "policy": "website_only"},
        },
    )
    run = _new_run(db_session, agent)
    result = execute("create_website", WEBSITE_ARGS, _ctx(agent, run))

    assert result["ok"] is False
    assert "website posts" in result["error"]
    assert API_KEY not in str(result)
    assert "<html" not in str(result).lower()
    assert fake_llm.requests == []
    rows = ToolCall.query.filter_by(run_id=run.id).all()
    assert len(rows) == 1
    assert rows[0].name == "create_website"
    assert rows[0].ok is False
    assert rows[0].error == result["error"]
    assert API_KEY not in str(rows[0].result)
    assert "<html" not in str(rows[0].result).lower()


def test_create_website_hourly_cap_checks_two_prior_shared_posts(
    app, db_session, fake_llm
):
    from deaddit.agents import executor as executor_module

    assert "create_website" in executor_module.RATE_CAPS
    assert "create_website" in executor_module._RATE_CAP_MESSAGES
    agent = _website_agent(db_session)
    for index in range(2):
        result = execute(
            "create_post",
            {
                "subdeaddit": "testsub",
                "title": f"Unique hourly headline {index} zyx",
                "content": f"Independent body {index} qwv",
            },
            _ctx(agent, _new_run(db_session, agent)),
        )
        assert result["ok"] is True

    fake_llm.enqueue_content(VALID_HTML, finish_reason="stop")
    capped = execute(
        "create_website",
        {**WEBSITE_ARGS, "title": "A third independent hourly headline"},
        _ctx(agent, _new_run(db_session, agent), deadline=Deadline.after(120)),
    )
    assert capped["ok"] is False
    assert "recently" in capped["error"]
    assert fake_llm.requests == []


def test_shared_one_post_run_budget_blocks_website_after_text_post(
    app, db_session, fake_llm
):
    agent = _website_agent(db_session)
    run = _new_run(db_session, agent)
    text = execute(
        "create_post",
        {
            "subdeaddit": "testsub",
            "title": "A standalone text post",
            "content": "A standalone body about trail maps.",
        },
        _ctx(agent, run),
    )
    assert text["ok"] is True
    blocked = execute(
        "create_website",
        WEBSITE_ARGS,
        _ctx(agent, run, deadline=Deadline.after(120)),
    )
    assert blocked["ok"] is False
    assert "already created a post" in blocked["error"]
    assert fake_llm.requests == []
    assert len(ToolCall.query.filter_by(run_id=run.id).all()) == 2


def test_website_duplicate_is_suppressed_after_text_post_before_generation(
    app, db_session, fake_llm
):
    agent = _website_agent(db_session)
    title = "A shared normalized title for duplicate detection"
    content = "A shared normalized body for duplicate detection."
    text = execute(
        "create_post",
        {"subdeaddit": "testsub", "title": title, "content": content},
        _ctx(agent, _new_run(db_session, agent)),
    )
    assert text["ok"] is True
    duplicate = execute(
        "create_website",
        {
            **WEBSITE_ARGS,
            "title": title,
            "content": content,
        },
        _ctx(agent, _new_run(db_session, agent), deadline=Deadline.after(120)),
    )
    assert duplicate["ok"] is False
    assert "too similar" in duplicate["error"]
    assert fake_llm.requests == []


def test_website_loop_detection_is_applied_by_executor(
    app, db_session, fake_llm, monkeypatch
):
    from dataclasses import replace

    from deaddit.agents import executor as executor_module
    from deaddit.agents import registry as registry_module
    from deaddit.agents.registry import get

    agent = _website_agent(db_session)
    run = _new_run(db_session, agent)
    tool = get("create_website")
    monkeypatch.setattr(executor_module, "_check_duplicate", lambda *args: None)
    monkeypatch.setattr(executor_module, "_check_rate_cap", lambda *args: None)
    monkeypatch.setitem(
        registry_module.TOOL_REGISTRY,
        "create_website",
        replace(
            tool, handler=lambda _ctx, _params: {"ok": True, "marker": "loop-test"}
        ),
    )
    first = execute("create_website", WEBSITE_ARGS, _ctx(agent, run))
    second = execute("create_website", WEBSITE_ARGS, _ctx(agent, run))
    third = execute("create_website", WEBSITE_ARGS, _ctx(agent, run))
    assert first == {"ok": True, "marker": "loop-test"}
    assert second["ok"] is True
    assert second["warning"] == "you are repeating the same action; vary your behaviour"
    assert third["ok"] is False
    assert third["force_finish"] is True
    assert "repeating the same action" in third["error"]
    assert fake_llm.requests == []


# ---------------------------------------------------------------------------
# Banned proposal words (WEBSITE_BANNED_WORDS, default "ledger"): the refusal
# must happen on the proposal, before any HTML generation is billed.


def test_create_website_refuses_banned_word_in_brief_before_generation(
    app, db_session, fake_llm
):
    agent = _website_agent(db_session)
    run = _new_run(db_session, agent)
    refused = execute(
        "create_website",
        {
            **WEBSITE_ARGS,
            "website_description": _DESCRIPTION.replace(
                "aurora-watching community site",
                "community site built around a shared ledger of aurora sightings",
            ),
        },
        _ctx(agent, run, deadline=Deadline.after(120)),
    )
    assert refused["ok"] is False
    assert "banned word 'ledger'" in refused["error"]
    assert "different website idea" in refused["hint"]
    assert fake_llm.requests == []
    assert Post.query.count() == 0
    assert _stored_files(app) == []
    row = ToolCall.query.order_by(ToolCall.id.desc()).first()
    assert row.name == "create_website" and row.ok is False


def test_create_website_banned_word_retry_with_new_idea_succeeds_same_run(
    app, db_session, fake_llm
):
    """The refusal is not a billed generation attempt, so a new idea in the
    same visit still gets its one attempt."""
    agent = _website_agent(db_session)
    run = _new_run(db_session, agent)
    banned = execute(
        "create_website",
        {**WEBSITE_ARGS, "hostname_hint": "www.ledger-tools.com"},
        _ctx(agent, run, deadline=Deadline.after(120)),
    )
    assert banned["ok"] is False and "banned word 'ledger'" in banned["error"]
    assert fake_llm.requests == []

    fake_llm.enqueue_content(VALID_HTML, finish_reason="stop")
    retry = execute(
        "create_website",
        WEBSITE_ARGS,
        _ctx(agent, run, deadline=Deadline.after(120)),
    )
    assert retry["ok"] is True
    assert len(fake_llm.requests) == 1


def test_create_website_banned_word_matches_case_and_word_start(app, db_session):
    agent = _website_agent(db_session)
    for variant in ("Ledgers", "ledger-like"):
        refused = execute(
            "create_website",
            {
                **WEBSITE_ARGS,
                "website_description": _DESCRIPTION.replace("aurora-watching", variant),
            },
            _ctx(agent, _new_run(db_session, agent), deadline=Deadline.after(120)),
        )
        assert refused["ok"] is False
        assert "banned word 'ledger'" in refused["error"]


def test_create_website_empty_ban_list_allows_any_proposal(app, db_session, fake_llm):
    Config.set("WEBSITE_BANNED_WORDS", "")
    agent = _website_agent(db_session)
    fake_llm.enqueue_content(VALID_HTML, finish_reason="stop")
    result = execute(
        "create_website",
        {
            **WEBSITE_ARGS,
            "website_description": _DESCRIPTION.replace(
                "aurora-watching community site",
                "community site built around a shared ledger of aurora sightings",
            ),
        },
        _ctx(agent, _new_run(db_session, agent), deadline=Deadline.after(120)),
    )
    assert result["ok"] is True
