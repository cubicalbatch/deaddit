"""FakeProvider-driven coverage for the single-run agent loop."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from deaddit.agents.loop import NUDGE_MESSAGE, is_runtime_enabled, run_once
from deaddit.llm.errors import PermanentLLMError
from deaddit.models import Agent, AgentRun, AgentTurn, ImageProvider, Post, ToolCall

ALL_TOOL_NAMES = {
    "browse_feed",
    "read_post",
    "search",
    "view_inbox",
    "view_profile",
    "vote",
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

    run = run_once("bob")

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
    _make_agent(db_session, "bob")
    fake_llm.enqueue(_tool_response([_tool_call("call_1", "view_profile", {})]))
    fake_llm.enqueue(
        _tool_response(
            [_tool_call("call_2", "finish", {"summary": "done", "mood": "calm"})]
        )
    )

    run = run_once("bob")

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
    _make_agent(db_session, "alice")
    fake_llm.enqueue_content("Just thinking out loud.")
    fake_llm.enqueue_content("Still nothing to do.")

    run = run_once("alice")

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
    _make_agent(db_session, "alice")
    broken = {
        "id": "call_bad",
        "type": "function",
        "function": {"name": "create_post", "arguments": "not-json{"},
    }
    fake_llm.enqueue(_tool_response([broken]))
    fake_llm.enqueue(
        _tool_response([_tool_call("call_ok", "finish", {"summary": "gave up"})])
    )

    run = run_once("alice")

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

    run = run_once("bob")

    assert run.status == "failed"
    assert "boom" in run.error_message
    db_session.refresh(agent)
    assert agent.consecutive_failures == 1
    assert agent.is_enabled is True  # one strike is not enough
    assert agent.status == "error"


def test_five_consecutive_failures_disable_the_agent(seeded_db, db_session, fake_llm):
    agent = _make_agent(db_session, "bob")
    for _ in range(5):
        fake_llm.enqueue_error(PermanentLLMError("boom"))
        run_once("bob")

    db_session.refresh(agent)
    assert agent.consecutive_failures == 5
    assert agent.is_enabled is False
    assert agent.status == "disabled"
    assert agent.next_run_at is None


# ---------------------------------------------------------------------------
# Budgets


def test_max_actions_per_run_budget_stops_the_loop(seeded_db, db_session, fake_llm):
    _make_agent(db_session, "alice", config={"max_actions_per_run": 1})
    fake_llm.enqueue(_tool_response([_tool_call("call_1", "view_profile", {})]))
    fake_llm.enqueue_content("This response must never be consumed.")

    run = run_once("alice")

    assert run.status == "completed"
    assert run.action_count == 1
    assert run.turn_count == 1
    assert len(fake_llm.requests) == 1  # second response left unconsumed


# ---------------------------------------------------------------------------
# Scheduling hint


def test_next_run_at_drawn_within_delay_bounds_when_enabled(
    seeded_db, db_session, fake_llm
):
    _make_agent(db_session, "alice", config={"min_delay": 60, "max_delay": 120})
    fake_llm.enqueue(
        _tool_response([_tool_call("call_1", "finish", {"summary": "bye"})])
    )
    before = datetime.utcnow()

    run = run_once("alice")

    agent = Agent.query.filter_by(user_username="alice").one()
    assert agent.next_run_at is not None
    delta = agent.next_run_at - before
    assert timedelta(seconds=55) <= delta <= timedelta(seconds=125)
    assert run.status == "completed"


def test_next_run_at_left_none_when_disabled(seeded_db, db_session, fake_llm):
    _make_agent(db_session, "alice", enabled=False)
    fake_llm.enqueue(
        _tool_response([_tool_call("call_1", "finish", {"summary": "bye"})])
    )

    run_once("alice")

    agent = Agent.query.filter_by(user_username="alice").one()
    assert agent.is_enabled is False
    assert agent.next_run_at is None


# ---------------------------------------------------------------------------
# Flag independence of manual runs


def test_manual_run_allowed_while_flag_off(seeded_db, db_session, fake_llm):
    _make_agent(db_session, "alice")
    assert is_runtime_enabled() is False
    fake_llm.enqueue(
        _tool_response([_tool_call("call_1", "finish", {"summary": "quiet"})])
    )

    run = run_once("alice")

    assert run.status == "completed"
    assert AgentRun.query.filter_by(status="completed").count() == 1


def test_two_consecutive_rejections_force_finish(seeded_db, db_session, fake_llm):
    """Plan semantics: after 2 consecutive guardrail rejections the loop
    forces finish instead of letting the model keep flailing."""
    _make_agent(db_session, "alice", config={"max_actions_per_run": 99})
    bad = {"subdeaddit": "testsub", "title": "x" * 400, "content": "hi"}
    fake_llm.enqueue(_tool_response([_tool_call("c1", "create_post", bad)]))
    fake_llm.enqueue(_tool_response([_tool_call("c2", "create_post", bad)]))
    # Unconsumed on purpose: the run must stop before a third turn.
    fake_llm.enqueue(_tool_response([_tool_call("c3", "view_profile", {})]))

    run = run_once("alice")

    assert run.status == "completed"
    assert run.action_count == 2
    assert ToolCall.query.filter_by(run_id=run.id).count() == 2
    assert all(c.ok is False for c in ToolCall.query.filter_by(run_id=run.id))


def test_kickoff_prompt_post_intent_inspires_create_post(seeded_db, db_session):
    from deaddit.agents.memory import generate_kickoff_prompt

    agent = _make_agent(db_session, "alice")
    prompt = generate_kickoff_prompt(agent, force_intent="post")
    assert "create_post" in prompt
    assert "inspired" in prompt.lower() or "share" in prompt.lower()


def test_kickoff_prompt_browse_intent_guides_browsing(seeded_db, db_session):
    from deaddit.agents.memory import generate_kickoff_prompt

    agent = _make_agent(db_session, "alice")
    prompt = generate_kickoff_prompt(agent, force_intent="browse")
    assert "browse" in prompt.lower()
    assert "finish" in prompt.lower()


def test_browse_feed_empty_and_sparse_hints(seeded_db, db_session):
    from deaddit.agents.executor import execute
    from deaddit.agents.registry import ToolContext

    agent = _make_agent(db_session, "alice")
    run = AgentRun(agent_id=agent.id, trigger="manual", status="running")
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
    seeded_db, db_session, fake_llm
):
    provider = _make_image_provider(db_session)
    _make_agent(
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

    run_once("bob")

    wire_tools = {
        t["function"]["name"] for t in fake_llm.requests[0]["payload"]["tools"]
    }
    assert "create_image_post" in wire_tools
    assert "create_post" in wire_tools


def test_loop_omits_create_post_under_image_only_policy(
    seeded_db, db_session, fake_llm
):
    provider = _make_image_provider(db_session)
    _make_agent(
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

    run_once("bob")

    wire_tools = {
        t["function"]["name"] for t in fake_llm.requests[0]["payload"]["tools"]
    }
    assert "create_image_post" in wire_tools
    assert "create_post" not in wire_tools


def test_loop_omits_create_image_post_when_disabled(seeded_db, db_session, fake_llm):
    _make_agent(db_session, "bob")  # default config: no image_posts key
    fake_llm.enqueue(_tool_response([_tool_call("c1", "finish", {"summary": "done"})]))

    run_once("bob")

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
    _make_agent(db_session, "bob")
    fake_llm.enqueue(_tool_response([_tool_call("c1", "finish", {"summary": "done"})]))

    run_once("bob")

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
