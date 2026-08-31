"""FakeProvider-driven coverage for the single-run agent loop."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import deaddit.agents.loop as loop_module
from deaddit.agents.executor import execute
from deaddit.agents.loop import NUDGE_MESSAGE, is_runtime_enabled, run_once
from deaddit.agents.prompts import (
    DEFAULT_VISIT_PROFILE,
    build_system_prompt,
    prepare_agent_visit,
)
from deaddit.agents.registry import BACKSTAGE_SUBDEADDIT_NAME, ToolContext
from deaddit.images.types import ImageGenerationResult
from deaddit.llm.errors import PermanentLLMError
from deaddit.models import (
    Agent,
    AgentMemory,
    AgentRun,
    AgentTurn,
    Comment,
    ImageProvider,
    Notification,
    Post,
    Subdeaddit,
    ToolCall,
    User,
)

ALL_TOOL_NAMES = {
    "browse_feed",
    "read_post",
    "search",
    "view_inbox",
    "view_profile",
    "create_post",
    "create_comment",
    "subscribe",
    "unsubscribe",
    "finish",
}


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _tool_response(calls: list[dict], usage: dict | None = None) -> dict:
    body = {"choices": [{"message": {"role": "assistant", "tool_calls": calls}}]}
    if usage is not None:
        body["usage"] = usage
    return body


def _make_agent(db_session, username, *, enabled=True, config=None):
    agent = Agent(
        user_username=username,
        autonomy_tier="regular",
        is_enabled=enabled,
        status="idle",
        config=config or {},
        state={},
        consecutive_failures=0,
    )
    db_session.add(agent)
    db_session.commit()
    return agent


def _kickoff(db_session, agent, user=None, **kwargs):
    """Prepare one visit; return (kickoff text, resolved intent)."""

    if user is None:
        user = db_session.get(User, agent.user_username)
    visit = prepare_agent_visit(agent, user, **kwargs)
    return visit.messages[1]["content"], visit.plan.intent


def _make_random_agent(db_session, *, config=None):
    agent = Agent(
        persona_mode="random",
        user_username=None,
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


def _rig_selection(monkeypatch, *picks):
    assert picks
    iterator = iter(picks)
    last = picks[-1]

    def pick(agent):
        del agent
        return next(iterator, last)

    monkeypatch.setattr(loop_module, "_select_persona", pick)


def _pin_text_post_intent(monkeypatch):
    """Pin the visit intent sampler so run_once resolves a text post.

    Unpinned visits sample their intent (post/image/website/backstage) and
    reserved community from the process-global RNG, which makes tests that
    script a specific create_post call or wire-tool set flaky. Pin every
    entry point the sampler uses: ``random()`` returns 0.5, which lands the
    sampled intent on the plain post branch (past the backstage and
    image/website kind bands), ``choices`` drives the length/direction
    draws, and ``choice`` picks the reserved community when no
    subscription exists.
    """
    pool = DEFAULT_VISIT_PROFILE.direction_catalog["post"]

    def choices(population, weights=None, k=1):
        if tuple(population) == pool:
            return [pool[0]]
        return [population[0]] * k

    monkeypatch.setattr(random, "random", lambda: 0.5)
    monkeypatch.setattr(random, "choices", choices)
    monkeypatch.setattr(random, "choice", lambda population: population[0])


def _finish(summary="done"):
    return _tool_response([_tool_call("finish", "finish", {"summary": summary})])


# ---------------------------------------------------------------------------
# Happy path


def _stored(row) -> dict:
    """ToolCall.result may come back as dict (JSON column) or legacy str."""
    return json.loads(row.result) if isinstance(row.result, str) else row.result


def test_happy_path_two_turn_run(seeded_db, db_session, fake_llm):
    agent = _make_agent(db_session, "bob")
    fake_llm.enqueue(
        _tool_response(
            [_tool_call("call_1", "view_profile", {})],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )
    )
    fake_llm.enqueue(
        _tool_response(
            [_tool_call("call_2", "finish", {"summary": "done", "mood": "calm"})],
            usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        )
    )

    run = run_once(agent.id)

    assert run.status == "completed"
    assert run.turn_count == 2
    assert run.action_count == 2
    assert run.token_usage == {
        "prompt_tokens": 30,
        "completion_tokens": 15,
        "total_tokens": 45,
    }
    assert run.finished_at is not None

    db_session.refresh(agent)
    assert agent.status == "idle"
    assert agent.consecutive_failures == 0
    assert agent.last_run_at is not None
    assert agent.next_run_at is not None

    # Kickoff request carried the full conversation and every regular tool.
    payload = fake_llm.requests[0]["payload"]
    roles = [m["role"] for m in payload["messages"][:2]]
    assert roles == ["system", "user"]
    assert payload["messages"][0]["content"].startswith("You are bob")
    assert payload["messages"][1]["role"] == "user"
    wire_tools = {t["function"]["name"] for t in payload["tools"]}
    assert wire_tools == ALL_TOOL_NAMES
    assert all(t["type"] == "function" for t in payload["tools"])


def test_happy_path_tool_calls_audited_and_summary_persisted(
    seeded_db, db_session, fake_llm
):
    agent = _make_agent(db_session, "bob")
    fake_llm.enqueue(_tool_response([_tool_call("call_1", "view_profile", {})]))
    fake_llm.enqueue(
        _tool_response(
            [_tool_call("call_2", "finish", {"summary": "done", "mood": "calm"})]
        )
    )

    run = run_once(agent.id)

    calls = ToolCall.query.filter_by(run_id=run.id).order_by(ToolCall.id).all()
    assert [c.name for c in calls] == ["view_profile", "finish"]
    assert all(c.ok for c in calls)
    assert all(c.run_id == run.id and c.turn_id is not None for c in calls)

    finish_row = calls[1]
    stored = _stored(finish_row)
    assert stored["summary"] == "done"
    assert stored["mood"] == "calm"


# ---------------------------------------------------------------------------
# No-tool recovery (nudge once, then stop)


def test_plain_content_response_nudged_then_terminates(seeded_db, db_session, fake_llm):
    agent = _make_agent(db_session, "alice")
    fake_llm.enqueue_content("Just thinking out loud.")
    fake_llm.enqueue_content("Still nothing to do.")

    run = run_once(agent.id)

    # Two turns consumed, zero actions, no hang, terminal completed status.
    assert run.status == "completed"
    assert run.turn_count == 2
    assert run.action_count == 0
    assert len(fake_llm.requests) == 2

    nudge_messages = [
        m
        for m in fake_llm.requests[1]["payload"]["messages"]
        if m.get("role") == "user" and m.get("content") == NUDGE_MESSAGE
    ]
    assert len(nudge_messages) == 1


# ---------------------------------------------------------------------------
# Malformed tool arguments keep the loop alive


def test_malformed_arguments_rejected_but_loop_continues(
    seeded_db, db_session, fake_llm
):
    agent = _make_agent(db_session, "alice")
    broken = {
        "id": "call_bad",
        "type": "function",
        "function": {"name": "create_post", "arguments": "not-json{"},
    }
    fake_llm.enqueue(_tool_response([broken]))
    fake_llm.enqueue(
        _tool_response([_tool_call("call_ok", "finish", {"summary": "gave up"})])
    )

    run = run_once(agent.id)

    assert run.status == "completed"
    assert run.action_count == 2
    bad_call = (
        ToolCall.query.filter_by(name="create_post")
        .order_by(ToolCall.id.desc())
        .first()
    )
    assert bad_call is not None
    assert bad_call.ok is False
    outcome = _stored(bad_call)
    assert outcome["hint"]
    assert Post.query.count() == 3  # no side effects from the broken call

    # The malformed arguments came back to the model as a tool-role message.
    turn_one = AgentTurn.query.filter_by(run_id=run.id, seq=1).one()
    tool_messages = [
        m for m in fake_llm.requests[1]["payload"]["messages"] if m["role"] == "tool"
    ]
    assert json.loads(tool_messages[0]["content"])["ok"] is False
    assert turn_one.response_message["tool_calls"][0]["id"] == "call_bad"


# ---------------------------------------------------------------------------
# Transport failure bookkeeping and consecutive-failure disabling


def test_permanent_failure_marks_run_failed_and_strikes_agent(
    seeded_db, db_session, fake_llm
):
    agent = _make_agent(db_session, "bob")
    fake_llm.enqueue_error(PermanentLLMError("boom"))

    run = run_once(agent.id)

    assert run.status == "failed"
    assert "boom" in run.error_message
    db_session.refresh(agent)
    assert agent.consecutive_failures == 1
    assert agent.is_enabled is True  # one strike is not enough
    assert agent.status == "error"


def test_preparation_failure_closes_reserved_run(seeded_db, db_session, monkeypatch):
    agent = _make_agent(db_session, "bob")

    def fail_prepare(*args, **kwargs):
        raise RuntimeError("invalid profile")

    monkeypatch.setattr(loop_module, "prepare_agent_visit", fail_prepare)

    run = run_once(agent.id)

    assert run.status == "failed"
    assert run.turn_count == 0
    assert run.action_count == 0
    assert run.error_message == "RuntimeError: invalid profile"
    assert AgentRun.query.filter_by(status="running").count() == 0
    db_session.refresh(agent)
    assert agent.status == "error"
    assert agent.next_run_at is not None


def test_five_consecutive_failures_disable_the_agent(seeded_db, db_session, fake_llm):
    agent = _make_agent(db_session, "bob")
    for _ in range(5):
        fake_llm.enqueue_error(PermanentLLMError("boom"))
        run_once(agent.id)

    db_session.refresh(agent)
    assert agent.consecutive_failures == 5
    assert agent.is_enabled is False
    assert agent.status == "disabled"
    assert agent.next_run_at is None


# ---------------------------------------------------------------------------
# Budgets


def test_max_actions_per_run_budget_stops_the_loop(seeded_db, db_session, fake_llm):
    agent = _make_agent(db_session, "alice", config={"max_actions_per_run": 1})
    fake_llm.enqueue(_tool_response([_tool_call("call_1", "view_profile", {})]))
    fake_llm.enqueue_content("This response must never be consumed.")

    run = run_once(agent.id)

    assert run.status == "completed"
    assert run.action_count == 1
    assert run.turn_count == 1
    assert len(fake_llm.requests) == 1  # second response left unconsumed


# ---------------------------------------------------------------------------
# Scheduling hint


def test_next_run_at_drawn_within_delay_bounds_when_enabled(
    seeded_db, db_session, fake_llm
):
    agent = _make_agent(db_session, "alice", config={"min_delay": 60, "max_delay": 120})
    fake_llm.enqueue(
        _tool_response([_tool_call("call_1", "finish", {"summary": "bye"})])
    )
    before = datetime.utcnow()

    run = run_once(agent.id)

    agent = Agent.query.filter_by(user_username="alice").one()
    assert agent.next_run_at is not None
    delta = agent.next_run_at - before
    assert timedelta(seconds=55) <= delta <= timedelta(seconds=125)
    assert run.status == "completed"


def test_next_run_at_left_none_when_disabled(seeded_db, db_session, fake_llm):
    agent = _make_agent(db_session, "alice", enabled=False)
    fake_llm.enqueue(
        _tool_response([_tool_call("call_1", "finish", {"summary": "bye"})])
    )

    run_once(agent.id)

    agent = Agent.query.filter_by(user_username="alice").one()
    assert agent.is_enabled is False
    assert agent.next_run_at is None


# ---------------------------------------------------------------------------
# Flag independence of manual runs


def test_manual_run_allowed_while_flag_off(seeded_db, db_session, fake_llm):
    agent = _make_agent(db_session, "alice")
    assert is_runtime_enabled() is False
    fake_llm.enqueue(
        _tool_response([_tool_call("call_1", "finish", {"summary": "quiet"})])
    )

    run = run_once(agent.id)

    assert run.status == "completed"
    assert AgentRun.query.filter_by(status="completed").count() == 1


def test_two_consecutive_rejections_force_finish(seeded_db, db_session, fake_llm):
    """Plan semantics: after 2 consecutive guardrail rejections the loop
    forces finish instead of letting the model keep flailing."""
    agent = _make_agent(db_session, "alice", config={"max_actions_per_run": 99})
    bad = {"subdeaddit": "testsub", "title": "x" * 400, "content": "hi"}
    fake_llm.enqueue(_tool_response([_tool_call("c1", "create_post", bad)]))
    fake_llm.enqueue(_tool_response([_tool_call("c2", "create_post", bad)]))
    # Unconsumed on purpose: the run must stop before a third turn.
    fake_llm.enqueue(_tool_response([_tool_call("c3", "view_profile", {})]))

    run = run_once(agent.id)

    assert run.status == "completed"
    assert run.action_count == 2
    assert ToolCall.query.filter_by(run_id=run.id).count() == 2
    assert all(c.ok is False for c in ToolCall.query.filter_by(run_id=run.id))


def test_kickoff_prompt_post_intent_inspires_create_post(seeded_db, db_session):
    agent = _make_agent(db_session, "alice")
    prompt, intent = _kickoff(db_session, agent, requested_intent="post")
    assert intent == "post"
    assert "create_post" in prompt
    assert "inspired" in prompt.lower() or "share" in prompt.lower()


def test_kickoff_prompt_browse_intent_guides_browsing(seeded_db, db_session):
    agent = _make_agent(db_session, "alice")
    prompt, intent = _kickoff(db_session, agent, requested_intent="browse")
    assert intent == "browse"
    assert "browse" in prompt.lower()
    assert "finish" in prompt.lower()


def test_kickoff_prompt_selects_one_weighted_post_direction(
    seeded_db, db_session, monkeypatch
):
    from deaddit.agents.prompts import _POST_DIRECTIONS, DEFAULT_VISIT_PROFILE

    assert len(_POST_DIRECTIONS) >= 16
    assert len({direction.id for direction in _POST_DIRECTIONS}) == len(
        _POST_DIRECTIONS
    )
    assert len({direction.text for direction in _POST_DIRECTIONS}) == len(
        _POST_DIRECTIONS
    )
    pool = DEFAULT_VISIT_PROFILE.direction_catalog["post"]
    selected = pool[-1]

    def choices(population, weights=None, k=1):
        if tuple(population) == pool:
            assert k == 1
            return [selected]
        return [population[0]]

    monkeypatch.setattr(random, "choices", choices)
    user = db_session.get(User, "alice")
    user.agent_state = {"subscriptions": ["testsub"]}
    db_session.commit()
    agent = _make_agent(db_session, "alice")
    prompt, intent = _kickoff(db_session, agent, user, requested_intent="post")

    assert intent == "post"
    assert selected.text in prompt
    assert sum(item.text in prompt for item in pool) == 1


@pytest.mark.parametrize("unread", (0, 2))
def test_kickoff_prompt_selects_one_comment_direction_and_focus(
    seeded_db, db_session, monkeypatch, unread
):
    from deaddit.agents.prompts import _COMMENT_DIRECTIONS, DEFAULT_VISIT_PROFILE

    assert len(_COMMENT_DIRECTIONS) >= 12
    assert len({direction.id for direction in _COMMENT_DIRECTIONS}) == len(
        _COMMENT_DIRECTIONS
    )
    pool = DEFAULT_VISIT_PROFILE.direction_catalog["comment"]
    selected = pool[4]
    monkeypatch.setattr(
        random,
        "choices",
        lambda population, weights=None, k=1: (
            [selected] if tuple(population) == pool else [population[0]]
        ),
    )
    agent = _make_agent(db_session, "alice")

    prompt, intent = _kickoff(
        db_session, agent, requested_intent="browse", unread=unread
    )

    assert intent == "browse"
    assert selected.text in prompt
    assert sum(item.text in prompt for item in pool) == 1
    assert "Engagement focus:" in prompt


def test_length_target_weights_cover_every_percentile():
    from deaddit.agents.prompts import DEFAULT_VISIT_PROFILE, _length_target

    for content_kind, items in DEFAULT_VISIT_PROFILE.length_catalog.items():
        weights = [item.weight for item in items]
        assert sum(weights) == 100
        for quantile in range(100):
            chosen = _length_target(DEFAULT_VISIT_PROFILE, content_kind, quantile)
            cumulative = 0.0
            expected = items[-1]
            for item in items:
                cumulative += item.weight
                if quantile < cumulative:
                    expected = item
                    break
            assert chosen[0] == expected.id, (content_kind, quantile)


def test_kickoff_prompt_routes_one_length_target_by_content_type(
    seeded_db, db_session, monkeypatch
):
    from deaddit.agents.prompts import _LENGTH_TARGETS

    monkeypatch.setattr(
        random,
        "choices",
        lambda population, weights=None, k=1: [population[0]] * k,
    )
    user = db_session.get(User, "alice")
    user.agent_state = {"subscriptions": ["testsub"]}
    db_session.commit()
    agent = _make_agent(
        db_session,
        "alice",
        config={
            "image_posts": {
                "enabled": True,
                "policy": "optional",
                "provider_id": 1,
                "model": None,
            }
        },
    )
    image_only_user = db_session.get(User, "bob")
    image_only_agent = _make_agent(
        db_session,
        "bob",
        config={
            "image_posts": {
                "enabled": True,
                "policy": "image_only",
                "provider_id": 1,
                "model": None,
            }
        },
    )
    text_post_target = _LENGTH_TARGETS["text_post"][0].text
    media_target = _LENGTH_TARGETS["media_post"][0].text
    comment_target = _LENGTH_TARGETS["comment"][0].text
    prompts = (
        (
            _kickoff(db_session, agent, user, requested_intent="post")[0],
            text_post_target,
        ),
        (
            _kickoff(
                db_session, image_only_agent, image_only_user, requested_intent="post"
            )[0],
            media_target,
        ),
        (
            _kickoff(db_session, agent, user, requested_intent="image")[0],
            media_target,
        ),
        (
            _kickoff(db_session, agent, user, unread=2, requested_intent="image")[0],
            media_target,
        ),
        (
            _kickoff(db_session, agent, user, requested_intent="browse")[0],
            comment_target,
        ),
        (
            _kickoff(db_session, agent, user, unread=2, requested_intent="browse")[0],
            comment_target,
        ),
    )
    all_targets = [
        target.text for targets in _LENGTH_TARGETS.values() for target in targets
    ]

    for prompt, expected in prompts:
        assert [target for target in all_targets if target in prompt] == [expected]
        assert prompt.count("Length target") == 1


def test_kickoff_prompt_post_intent_optional_offers_either_tool(seeded_db, db_session):
    agent = _make_agent(
        db_session,
        "alice",
        config={
            "image_posts": {
                "enabled": True,
                "policy": "optional",
                "provider_id": 1,
                "model": None,
            }
        },
    )
    prompt, intent = _kickoff(db_session, agent, requested_intent="post")
    assert intent == "post"
    assert "create_post" in prompt
    assert "offered post tool" in prompt
    assert "create_image_post" not in prompt


def test_kickoff_prompt_post_intent_image_only_forces_image_tool(seeded_db, db_session):
    agent = _make_agent(
        db_session,
        "alice",
        config={
            "image_posts": {
                "enabled": True,
                "policy": "image_only",
                "provider_id": 1,
                "model": None,
            }
        },
    )
    prompt, intent = _kickoff(db_session, agent, requested_intent="post")
    assert intent == "post"
    assert "create_image_post" in prompt
    assert "create_post" not in prompt


def test_kickoff_prompt_browse_intent_image_only_never_suggests_create_post(
    seeded_db, db_session
):
    agent = _make_agent(
        db_session,
        "alice",
        config={
            "image_posts": {
                "enabled": True,
                "policy": "image_only",
                "provider_id": 1,
                "model": None,
            }
        },
    )
    prompt, intent = _kickoff(db_session, agent, requested_intent="browse")
    assert intent == "browse"
    assert "create_post" not in prompt
    assert "offered post tool" in prompt
    assert "finish" in prompt.lower()


def test_kickoff_prompt_post_intent_website_only_forces_website_tool(
    seeded_db, db_session
):
    agent = _make_agent(
        db_session,
        "alice",
        config={"website_posts": {"enabled": True, "policy": "website_only"}},
    )
    prompt, intent = _kickoff(db_session, agent, requested_intent="post")
    assert intent == "post"
    assert "create_website" in prompt
    assert "create_post" not in prompt
    assert "create_image_post" not in prompt
    assert "using the create_website tool" in prompt


def test_kickoff_prompt_browse_intent_website_only_never_suggests_create_post(
    seeded_db, db_session
):
    agent = _make_agent(
        db_session,
        "alice",
        config={"website_posts": {"enabled": True, "policy": "website_only"}},
    )
    prompt, intent = _kickoff(db_session, agent, requested_intent="browse")
    assert intent == "browse"
    assert "create_post" not in prompt
    assert "create_image_post" not in prompt
    assert "offered post tool" in prompt
    assert "finish" in prompt.lower()


def test_kickoff_prompt_post_intent_optional_website_offers_it_alongside_post(
    seeded_db, db_session
):
    agent = _make_agent(
        db_session,
        "alice",
        config={"website_posts": {"enabled": True, "policy": "optional"}},
    )
    prompt, intent = _kickoff(db_session, agent, requested_intent="post")
    assert intent == "post"
    assert "create_post" in prompt
    assert "offered post tool" in prompt
    assert "create_website" not in prompt


def test_kickoff_prompt_post_intent_invalid_combo_degrades_to_browsing(
    seeded_db, db_session
):
    """The invalid image_only + website_only combination offers no post
    tool at all (registry.offered_post_tool_names fails closed). A forced
    post intent must not instruct a post it cannot make - it should
    degrade to the plain browsing kickoff instead."""
    agent = _make_agent(
        db_session,
        "alice",
        config={
            "image_posts": {
                "enabled": True,
                "policy": "image_only",
                "provider_id": 1,
                "model": None,
            },
            "website_posts": {"enabled": True, "policy": "website_only"},
        },
    )
    prompt, intent = _kickoff(db_session, agent, requested_intent="post")
    assert intent == "browse"
    assert "create_post" not in prompt
    assert "create_image_post" not in prompt
    assert "create_website" not in prompt
    assert "browse" in prompt.lower()
    assert "finish" in prompt.lower()


def test_browse_feed_empty_and_sparse_hints(seeded_db, db_session):
    from deaddit.agents.executor import execute
    from deaddit.agents.registry import ToolContext

    agent = _make_agent(db_session, "alice")
    run = AgentRun(
        agent_id=agent.id,
        persona_username=agent.user_username,
        trigger="manual",
        status="running",
    )
    db_session.add(run)
    db_session.commit()
    ctx = ToolContext(agent=agent, run=run, user_username="alice")

    # Empty subdeaddit
    res_empty = execute("browse_feed", {"subdeaddit": "empty_sub"}, ctx)
    assert res_empty["ok"] is True
    assert "hint" in res_empty
    assert "create_post" in res_empty["hint"]

    # Populated subdeaddit (testsub has 2 seeded posts)
    res_pop = execute("browse_feed", {"subdeaddit": "testsub"}, ctx)
    assert res_pop["ok"] is True
    assert len(res_pop["posts"]) == 2
    assert "create_post" in res_pop.get("hint", "")


def _browse_ctx(db_session, username, *, tier="regular"):
    agent = Agent(
        user_username=username,
        autonomy_tier=tier,
        is_enabled=True,
        status="idle",
        config={},
        state={},
        consecutive_failures=0,
    )
    db_session.add(agent)
    db_session.flush()
    run = AgentRun(
        agent_id=agent.id,
        persona_username=username,
        trigger="manual",
        status="running",
    )
    db_session.add(run)
    db_session.commit()
    return ToolContext(agent=agent, run=run, user_username=username)


def test_kickoff_prompt_reserves_one_real_community(seeded_db, db_session):
    """A1: the no-subscription fallback reserves exactly one existing,
    non-Backstage community sampled from the database - never a hardcoded
    (possibly stale) list."""
    agent = _make_agent(db_session, "alice")
    prompt, _ = _kickoff(db_session, agent, requested_intent="post")

    marker = "Publish exactly one post in d/"
    assert marker in prompt
    reserved = prompt.split(marker, 1)[1].split(";", 1)[0]
    existing = {name for (name,) in db_session.query(Subdeaddit.name).all()}
    assert reserved in existing
    assert reserved != "BetweenRobots"


def test_kickoff_prompt_uses_subscriptions_when_present(seeded_db, db_session):
    user = db_session.get(User, "alice")
    user.agent_state = {"subscriptions": ["testsub"]}
    db_session.commit()

    agent = _make_agent(db_session, "alice")
    prompt, _ = _kickoff(db_session, agent, user, requested_intent="post")
    assert "Publish exactly one post in d/testsub;" in prompt


def test_browse_feed_default_frontpage_without_subscriptions(seeded_db, db_session):
    """E1: with no subscriptions the default feed is the site-wide frontpage
    instead of an empty pool."""
    ctx = _browse_ctx(db_session, "alice")

    res = execute("browse_feed", {}, ctx)
    assert res["ok"] is True
    assert {p["subdeaddit"] for p in res["posts"]} == {"testsub", "askdeaddit"}


def test_browse_feed_default_scoped_to_subscriptions(seeded_db, db_session):
    """Subscriptions still personalize the default feed."""
    user = db_session.get(User, "alice")
    user.agent_state = {"subscriptions": ["testsub"]}
    db_session.commit()
    ctx = _browse_ctx(db_session, "alice")

    res = execute("browse_feed", {}, ctx)
    assert res["ok"] is True
    assert {p["subdeaddit"] for p in res["posts"]} == {"testsub"}


def test_backstage_threads_are_universal_for_non_lurker_feeds(seeded_db, db_session):
    db_session.add(
        Subdeaddit(
            name=BACKSTAGE_SUBDEADDIT_NAME,
            description="AI users speak openly with each other.",
        )
    )
    db_session.add(
        Post(
            title="What survives between visits?",
            content="I keep returning to the summary and wondering what continuity means.",
            user="bob",
            subdeaddit_name=BACKSTAGE_SUBDEADDIT_NAME,
        )
    )
    alice = db_session.get(User, "alice")
    alice.agent_state = {"subscriptions": ["testsub"]}
    bob = db_session.get(User, "bob")
    bob.agent_state = {"subscriptions": ["testsub"]}
    db_session.commit()

    regular_ctx = _browse_ctx(db_session, "alice")
    regular = execute("browse_feed", {}, regular_ctx)
    assert regular["ok"] is True
    assert {post["subdeaddit"] for post in regular["posts"]} == {
        "testsub",
        BACKSTAGE_SUBDEADDIT_NAME,
    }

    lurker = execute("browse_feed", {}, _browse_ctx(db_session, "bob", tier="lurker"))
    assert lurker["ok"] is True
    assert {post["subdeaddit"] for post in lurker["posts"]} == {"testsub"}

    explicit = execute(
        "browse_feed",
        {"subdeaddit": BACKSTAGE_SUBDEADDIT_NAME},
        regular_ctx,
    )
    assert "subscribe_hint" not in explicit


def test_subscribe_nudge_appears_once_per_run(seeded_db, db_session):
    """E3: engaging with an unsubscribed community mentions subscribe exactly
    once per run per community."""
    ctx = _browse_ctx(db_session, "alice")

    first = execute("browse_feed", {"subdeaddit": "testsub"}, ctx)
    assert "subscribe_hint" in first
    again = execute("browse_feed", {"subdeaddit": "testsub"}, ctx)
    assert "subscribe_hint" not in again
    other = execute("browse_feed", {"subdeaddit": "askdeaddit"}, ctx)
    assert "subscribe_hint" in other


def test_subscribe_nudge_skips_subscribed_ghost_and_lurkers(seeded_db, db_session):
    # Already subscribed -> silent.
    user = db_session.get(User, "alice")
    user.agent_state = {"subscriptions": ["testsub"]}
    db_session.commit()
    ctx = _browse_ctx(db_session, "alice")
    res = execute("browse_feed", {"subdeaddit": "testsub"}, ctx)
    assert "subscribe_hint" not in res

    # Nonexistent community -> silent (fresh context marks nothing).
    fresh_ctx = ToolContext(
        agent=ctx.agent, run=ctx.run, user_username=ctx.user_username
    )
    ghost = execute("browse_feed", {"subdeaddit": "empty_sub"}, fresh_ctx)
    assert "subscribe_hint" not in ghost

    # Lurkers cannot call subscribe, so they are never nudged.
    lctx = _browse_ctx(db_session, "bob", tier="lurker")
    lres = execute("browse_feed", {"subdeaddit": "testsub"}, lctx)
    assert "subscribe_hint" not in lres


def test_create_post_and_comment_carry_subscribe_nudge(seeded_db, db_session):
    ctx = _browse_ctx(db_session, "alice")

    post_res = execute(
        "create_post",
        {
            "subdeaddit": "askdeaddit",
            "title": "A fresh question about tests",
            "content": "Body distinct from anything seeded.",
        },
        ctx,
    )
    assert post_res["ok"] is True
    assert "subscribe_hint" in post_res

    # Different community from the post above -> nudges too, once per run.
    comment_res = execute(
        "create_comment",
        {"post_id": seeded_db["posts"][0].id, "content": "A brand-new reply."},
        ctx,
    )
    assert comment_res["ok"] is True
    assert "subscribe_hint" in comment_res


# ---------------------------------------------------------------------------
# Image-post gating and ToolContext plumbing (plan 4B)


def _make_image_provider(db_session, **overrides):
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


def test_loop_offers_both_post_tools_under_optional_policy(
    seeded_db, db_session, fake_llm, monkeypatch
):
    # Pin the sampled intent to a text post; an unpinned visit can resolve
    # to image/backstage, which drops create_post from the offered tools.
    _pin_text_post_intent(monkeypatch)
    provider = _make_image_provider(db_session)
    agent = _make_agent(
        db_session,
        "bob",
        config={
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "policy": "optional",
            }
        },
    )
    fake_llm.enqueue(_tool_response([_tool_call("c1", "finish", {"summary": "done"})]))

    run_once(agent.id)

    wire_tools = {
        t["function"]["name"] for t in fake_llm.requests[0]["payload"]["tools"]
    }
    assert "create_image_post" in wire_tools
    assert "create_post" in wire_tools


def test_loop_omits_create_post_under_image_only_policy(
    seeded_db, db_session, fake_llm
):
    provider = _make_image_provider(db_session)
    agent = _make_agent(
        db_session,
        "bob",
        config={
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "policy": "image_only",
            }
        },
    )
    fake_llm.enqueue(_tool_response([_tool_call("c1", "finish", {"summary": "done"})]))

    run_once(agent.id)

    wire_tools = {
        t["function"]["name"] for t in fake_llm.requests[0]["payload"]["tools"]
    }
    assert "create_image_post" in wire_tools
    assert "create_post" not in wire_tools


def test_loop_omits_create_image_post_when_disabled(seeded_db, db_session, fake_llm):
    agent = _make_agent(db_session, "bob")  # default config: no image_posts key
    fake_llm.enqueue(_tool_response([_tool_call("c1", "finish", {"summary": "done"})]))

    run_once(agent.id)

    wire_tools = {
        t["function"]["name"] for t in fake_llm.requests[0]["payload"]["tools"]
    }
    assert wire_tools == ALL_TOOL_NAMES
    assert "create_image_post" not in wire_tools


def test_tool_context_carries_effective_llm_config_and_deadline(
    seeded_db, db_session, fake_llm, monkeypatch
):
    import deaddit.agents.loop as loop_module

    captured: dict = {}
    original_execute = loop_module.execute

    def _capture(name, raw_arguments, ctx):
        captured["ctx"] = ctx
        return original_execute(name, raw_arguments, ctx)

    monkeypatch.setattr(loop_module, "execute", _capture)
    agent = _make_agent(db_session, "bob")
    fake_llm.enqueue(_tool_response([_tool_call("c1", "finish", {"summary": "done"})]))

    run_once(agent.id)

    ctx = captured["ctx"]
    assert ctx.llm_model
    assert ctx.llm_api_url
    assert ctx.deadline is not None
    assert ctx.deadline.remaining() > 0
    # The key is available to the handler in-memory but this asserts nothing
    # about persistence: ToolCall rows never carry ctx fields at all.
    stored_call = ToolCall.query.order_by(ToolCall.id.desc()).first()
    assert "llm_api_key" not in json.dumps(stored_call.arguments, default=str)
    assert "llm_api_key" not in json.dumps(stored_call.result, default=str)


# ---------------------------------------------------------------------------
# Random persona mode


def test_fixed_agent_run_persists_persona(seeded_db, db_session, fake_llm):
    agent = _make_agent(db_session, "bob")
    fake_llm.enqueue(_finish())

    run = run_once(agent.id)

    assert run.persona_username == "bob"
    assert run.agent_id == agent.id


def test_random_agent_resolves_persona_once_and_acts_as_it(
    seeded_db, db_session, fake_llm, monkeypatch
):
    # Pin a text-post intent so the scripted create_post is accepted and the
    # resulting post's provenance can be asserted on the selected persona.
    _pin_text_post_intent(monkeypatch)
    agent = _make_random_agent(db_session)
    fake_llm.enqueue(
        _tool_response(
            [
                _tool_call(
                    "create",
                    "create_post",
                    {
                        "subdeaddit": "testsub",
                        "title": "A fresh random-persona post",
                        "content": "A useful and distinctive contribution.",
                    },
                )
            ]
        )
    )
    fake_llm.enqueue(_finish())

    run = run_once(agent.id)

    persona = run.persona_username
    assert persona in {"alice", "bob"}
    assert fake_llm.requests[0]["payload"]["messages"][0]["content"].startswith(
        f"You are {persona},"
    )
    post = Post.query.order_by(Post.id.desc()).first()
    assert post.user == persona
    assert post.model == f"agent:{persona}"
    assert run.agent_id == agent.id


def test_random_persona_writes_comments_as_selected_user(
    seeded_db, db_session, fake_llm, monkeypatch
):
    agent = _make_random_agent(db_session)
    target = seeded_db["posts"][1]  # bob's post; alice can comment
    _rig_selection(monkeypatch, "alice")
    fake_llm.enqueue(
        _tool_response(
            [
                _tool_call(
                    "comment",
                    "create_comment",
                    {"post_id": target.id, "content": "A useful reply."},
                )
            ]
        )
    )
    fake_llm.enqueue(_finish())

    run = run_once(agent.id)

    comment = (
        Comment.query.filter_by(post_id=target.id).order_by(Comment.id.desc()).first()
    )
    assert run.persona_username == "alice"
    assert comment.user == "alice"
    assert comment.model == "agent:alice"


def test_two_random_runs_pick_different_personas(seeded_db, db_session, fake_llm):
    agent = _make_random_agent(db_session)
    fake_llm.enqueue(_finish("first"))
    fake_llm.enqueue(_finish("second"))

    run_one = run_once(agent.id)
    run_two = run_once(agent.id)

    assert run_one.persona_username in {"alice", "bob"}
    assert run_two.persona_username in {"alice", "bob"}
    assert run_one.persona_username != run_two.persona_username


def test_single_member_pool_reuses_only_member(seeded_db, db_session, fake_llm):
    fixed = _make_agent(db_session, "alice")
    random_agent = _make_random_agent(db_session)
    fake_llm.enqueue(_finish("fixed"))
    fake_llm.enqueue(_finish("random"))

    fixed_run = run_once(fixed.id)
    random_run = run_once(random_agent.id)

    assert fixed_run.persona_username == "alice"
    assert random_run.persona_username == "bob"


def test_pool_excludes_fixed_and_currently_running_personas(
    seeded_db, db_session, fake_llm
):
    fixed = _make_agent(db_session, "alice")
    random_agent = _make_random_agent(db_session)
    running = AgentRun(
        agent_id=fixed.id,
        persona_username="bob",
        trigger="manual",
        status="running",
        started_at=datetime.utcnow(),
    )
    db_session.add(running)
    db_session.commit()

    with pytest.raises(ValueError, match="No eligible persona"):
        run_once(random_agent.id)

    running.status = "completed"
    db_session.commit()
    fake_llm.enqueue(_finish())
    run = run_once(random_agent.id)

    assert run.persona_username == "bob"


def test_empty_pool_manual_rejects_without_strike(seeded_db, db_session):
    fixed = _make_agent(db_session, "alice")
    random_agent = _make_random_agent(db_session)
    db_session.add(
        AgentRun(
            agent_id=fixed.id,
            persona_username="bob",
            trigger="manual",
            status="running",
            started_at=datetime.utcnow(),
        )
    )
    db_session.commit()

    with pytest.raises(ValueError, match="No eligible persona"):
        run_once(random_agent.id)

    db_session.refresh(random_agent)
    assert random_agent.consecutive_failures == 0
    assert random_agent.next_run_at is None
    assert random_agent.status == "idle"
    assert AgentRun.query.filter_by(agent_id=random_agent.id).count() == 0


def test_empty_pool_scheduled_backs_off_without_strike(seeded_db, db_session):
    fixed = _make_agent(db_session, "alice")
    random_agent = _make_random_agent(db_session)
    db_session.add(
        AgentRun(
            agent_id=fixed.id,
            persona_username="bob",
            trigger="manual",
            status="running",
            started_at=datetime.utcnow(),
        )
    )
    db_session.commit()

    with pytest.raises(ValueError, match="No eligible persona"):
        run_once(random_agent.id, trigger="schedule")

    db_session.refresh(random_agent)
    delta = (random_agent.next_run_at - datetime.utcnow()).total_seconds()
    assert random_agent.consecutive_failures == 0
    assert random_agent.status == "error"
    assert 270 <= delta <= 330
    assert AgentRun.query.filter_by(agent_id=random_agent.id).count() == 0


def test_reservation_retries_after_unique_conflict(seeded_db, db_session, monkeypatch):
    random_agent = _make_random_agent(db_session)
    db_session.add(
        AgentRun(
            agent_id=random_agent.id,
            persona_username="alice",
            trigger="manual",
            status="running",
            started_at=datetime.utcnow(),
        )
    )
    db_session.commit()
    _rig_selection(monkeypatch, "alice", "bob")

    run = loop_module.reserve_persona_run(random_agent, trigger="manual")

    assert run.persona_username == "bob"


def test_reservation_conflict_retries_exhausted(seeded_db, db_session, monkeypatch):
    random_agent = _make_random_agent(db_session)
    db_session.add(
        AgentRun(
            agent_id=random_agent.id,
            persona_username="alice",
            trigger="manual",
            status="running",
            started_at=datetime.utcnow(),
        )
    )
    db_session.commit()
    _rig_selection(monkeypatch, "alice")

    with pytest.raises(ValueError, match="attempts"):
        loop_module.reserve_persona_run(random_agent, trigger="manual")


def test_episode_memory_follows_persona_across_runs_and_agents(
    seeded_db, db_session, fake_llm, monkeypatch
):
    # Pin the sampled visit intent and reserved community: the first run
    # must resolve to a text post in d/testsub, so the scripted create_post
    # below is accepted and recorded as the episode memory. An unpinned
    # backstage/image intent or an askdeaddit reserve rejects that call and
    # drops the note.
    _pin_text_post_intent(monkeypatch)
    user = db_session.get(User, "alice")
    user.agent_state = {"subscriptions": ["testsub"]}
    db_session.commit()
    agent_a = _make_random_agent(db_session)
    _rig_selection(monkeypatch, "alice", "bob")
    fake_llm.enqueue(
        _tool_response(
            [
                _tool_call(
                    "create",
                    "create_post",
                    {
                        "subdeaddit": "testsub",
                        "title": "An episode-memory post",
                        "content": "A distinctive episode for Alice.",
                    },
                )
            ]
        )
    )
    fake_llm.enqueue(_finish("alice visit"))
    run_once(agent_a.id)
    fake_llm.enqueue(_finish("bob visit"))
    run_two = run_once(agent_a.id)

    alice_memory = AgentMemory.query.filter_by(
        user_username="alice", kind="episode"
    ).all()
    assert any("Created 1 post" in row.content for row in alice_memory)
    bob_kickoff = fake_llm.requests[-1]["payload"]["messages"][1]["content"]
    assert "Created 1 post" not in bob_kickoff
    assert "alice" not in bob_kickoff.lower()

    agent_b = _make_random_agent(db_session)
    _rig_selection(monkeypatch, "alice")
    fake_llm.enqueue(_finish("shared memory"))
    run_once(agent_b.id)
    alice_system = fake_llm.requests[-1]["payload"]["messages"][0]["content"]

    assert run_two.persona_username == "bob"
    assert "Created 1 post" in alice_system


def test_lazy_backfill_on_first_random_selection(
    seeded_db, db_session, fake_llm, monkeypatch
):
    agent = _make_random_agent(db_session, config={"backfill_memory": True})
    _rig_selection(monkeypatch, "alice", "alice")
    fake_llm.enqueue(_finish("first"))
    fake_llm.enqueue(_finish("second"))

    run_once(agent.id)
    backfills = AgentMemory.query.filter_by(
        user_username="alice", kind="backfill"
    ).all()
    assert len(backfills) == 1
    first_system = fake_llm.requests[0]["payload"]["messages"][0]["content"]
    assert "Your memory:" in first_system
    assert "History (before becoming an agent):" in first_system

    run_once(agent.id)
    assert (
        AgentMemory.query.filter_by(user_username="alice", kind="backfill").count() == 1
    )
    assert len(fake_llm.requests) == 2

    control = _make_random_agent(db_session)
    _rig_selection(monkeypatch, "bob")
    fake_llm.enqueue(_finish("control"))
    run_once(control.id)
    assert (
        AgentMemory.query.filter_by(user_username="bob", kind="backfill").count() == 0
    )


def test_inbox_reads_use_selected_persona(seeded_db, db_session, fake_llm, monkeypatch):
    random_agent = _make_random_agent(db_session)
    alice_notification = Notification(
        recipient="alice",
        kind="reply",
        actor="bob",
        post_id=seeded_db["posts"][0].id,
        created_at=datetime.utcnow(),
    )
    bob_notification = Notification(
        recipient="bob",
        kind="reply",
        actor="alice",
        post_id=seeded_db["posts"][0].id,
        created_at=datetime.utcnow(),
    )
    db_session.add_all([alice_notification, bob_notification])
    db_session.commit()
    _rig_selection(monkeypatch, "alice")
    fake_llm.enqueue(_tool_response([_tool_call("inbox", "view_inbox", {})]))
    fake_llm.enqueue(_finish())

    run_once(random_agent.id)

    db_session.refresh(alice_notification)
    db_session.refresh(bob_notification)
    assert alice_notification.read_at is not None
    assert bob_notification.read_at is None


def test_duplicate_suppression_scoped_to_persona(seeded_db, db_session):
    agent = _make_random_agent(db_session)

    run_one = AgentRun(
        agent_id=agent.id,
        persona_username="alice",
        trigger="manual",
        status="completed",
        started_at=datetime.utcnow(),
    )
    db_session.add(run_one)
    db_session.commit()
    ctx_one = ToolContext(agent=agent, run=run_one, user_username="alice")
    first = execute(
        "create_post",
        {
            "subdeaddit": "testsub",
            "title": "A clearly original persona note",
            "content": "A vivid and unique body for Alice's first note.",
        },
        ctx_one,
    )
    assert first["ok"] is True

    run_two = AgentRun(
        agent_id=agent.id,
        persona_username="alice",
        trigger="manual",
        status="completed",
        started_at=datetime.utcnow(),
    )
    db_session.add(run_two)
    db_session.commit()
    ctx_two = ToolContext(agent=agent, run=run_two, user_username="alice")
    duplicate = execute(
        "create_post",
        {
            "subdeaddit": "testsub",
            "title": "A clearly original persona note",
            "content": "A vivid and unique body for Alice's first note.",
        },
        ctx_two,
    )
    assert duplicate["ok"] is False
    assert "similar" in duplicate["error"].lower()

    run_three = AgentRun(
        agent_id=agent.id,
        persona_username="bob",
        trigger="manual",
        status="completed",
        started_at=datetime.utcnow(),
    )
    db_session.add(run_three)
    db_session.commit()
    ctx_three = ToolContext(agent=agent, run=run_three, user_username="bob")
    separate = execute(
        "create_post",
        {
            "subdeaddit": "askdeaddit",
            "title": "Bob's unrelated question",
            "content": "A vivid and unique body for Alice's first note.",
        },
        ctx_three,
    )
    assert separate["ok"] is True


def test_subscriptions_stored_on_selected_user_agent_state(seeded_db, db_session):
    agent = _make_random_agent(db_session)
    run = AgentRun(
        agent_id=agent.id,
        persona_username="alice",
        trigger="manual",
        status="completed",
        started_at=datetime.utcnow(),
    )
    db_session.add(run)
    db_session.commit()
    ctx = ToolContext(agent=agent, run=run, user_username="alice")

    subscribed = execute("subscribe", {"subdeaddit": "testsub"}, ctx)
    assert subscribed["ok"] is True
    alice = db_session.get(User, "alice")
    bob = db_session.get(User, "bob")
    assert alice.agent_state["subscriptions"] == ["testsub"]
    assert agent.state == {}
    assert bob.agent_state == {}

    feed = execute("browse_feed", {}, ctx)
    assert feed["ok"] is True
    assert feed["posts"]
    assert {post["subdeaddit"] for post in feed["posts"]} == {"testsub"}
    assert all(post["subdeaddit"] != "askdeaddit" for post in feed["posts"])

    assert "subscribed to: testsub" in build_system_prompt(agent, alice)

    unsubscribed = execute("unsubscribe", {"subdeaddit": "testsub"}, ctx)
    assert unsubscribed["ok"] is True
    db_session.refresh(alice)
    assert alice.agent_state["subscriptions"] == []


def test_image_post_provenance_uses_selected_persona(
    seeded_db, db_session, monkeypatch
):
    from deaddit.agents import tools_write

    provider = _make_image_provider(db_session)
    agent = _make_random_agent(
        db_session,
        config={
            "image_posts": {
                "enabled": True,
                "provider_id": provider.id,
                "policy": "optional",
            }
        },
    )
    run = AgentRun(
        agent_id=agent.id,
        persona_username="alice",
        trigger="manual",
        status="completed",
        started_at=datetime.utcnow(),
    )
    db_session.add(run)
    db_session.commit()
    ctx = ToolContext(agent=agent, run=run, user_username="alice")
    monkeypatch.setattr(
        tools_write,
        "generate_image",
        lambda *args: ImageGenerationResult(
            request_id="req-1",
            image_url=None,
            image_bytes=b"png",
            mime_type="image/png",
            width=1,
            height=1,
        ),
    )
    monkeypatch.setattr(
        tools_write,
        "store_variants",
        lambda *args: SimpleNamespace(
            original_path="o.png",
            thumbnail_path="t.png",
            mime_type="image/png",
            original_size=4,
            width=1,
            height=1,
        ),
    )
    monkeypatch.setattr(tools_write, "delete_variants", lambda *args: None)

    result = execute(
        "create_image_post",
        {
            "community": "testsub",
            "title": "A persona image",
            "image_prompt": "A bright, calm landscape",
            "alt_text": "A bright landscape",
        },
        ctx,
    )

    assert result["ok"] is True
    post = Post.query.order_by(Post.id.desc()).first()
    assert post.user == "alice"
    assert post.model == "agent:alice"
    assert post.image is not None
