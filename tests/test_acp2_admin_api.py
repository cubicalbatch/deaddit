"""AgenticCore Phase 2: agent-admin JSON API and memory subsystem.

Covers the ``# --- AgenticCore: agent administration ---`` section of
deaddit/admin.py (list/create/toggle/force-run/drill-down/bulk endpoints
and pages) plus deaddit/agents/memory.py (episode summaries, persona
backfill, kickoff memory injection).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import deaddit.llm.capabilities as capabilities
from deaddit.agents.memory import (
    BACKFILL_PREFIX,
    backfill_persona_history,
    build_initial_messages,
    summarize_run,
)
from deaddit.extensions import db
from deaddit.llm.errors import CapabilityError
from deaddit.models import (
    Agent,
    AgentMemory,
    AgentRun,
    AgentTurn,
    Comment,
    ToolCall,
    User,
)


@pytest.fixture()
def admin_client(client):
    """Client that passes the admin_required gate even if API_TOKEN is set."""
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


def _make_agent(db_session, username, *, enabled=False, config=None):
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


def _make_random_agent(db_session, *, enabled=False, config=None):
    agent = Agent(
        persona_mode="random",
        user_username=None,
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


def _noop_tools_allowed(api_url, model_name, **kwargs):
    return None


# ---------------------------------------------------------------------------
# GET /admin/api/agents + presets


def test_agent_list_serializes_config_and_run_tallies(
    seeded_db, admin_client, db_session
):
    agent = _make_agent(
        db_session, "alice", config={"min_delay": 60, "max_run_seconds": 120}
    )
    now = datetime.utcnow()
    for status in ("completed", "completed", "failed", "interrupted"):
        db.session.add(
            AgentRun(
                agent_id=agent.id,
                persona_username=agent.user_username,
                trigger="schedule",
                status=status,
                started_at=now,
                finished_at=now,
            )
        )
    agent.consecutive_failures = 2
    db.session.commit()

    resp = admin_client.get("/admin/api/agents")
    assert resp.status_code == 200
    body = resp.get_json()

    rows = {row["user_username"]: row for row in body["agents"]}
    row = rows["alice"]
    assert row["is_enabled"] is False
    assert row["status"] == "idle"
    assert row["config"]["max_run_seconds"] == 120
    assert row["consecutive_failures"] == 2
    assert row["runs_completed"] == 2
    assert row["runs_failed"] == 1
    assert row["runs_interrupted"] == 1
    assert row["runs_total"] == 4


def test_presets_endpoint_exposes_form_defaults(seeded_db, admin_client):
    body = admin_client.get("/admin/api/agents/presets").get_json()
    assert body["tiers"] == ["lurker", "regular", "power_user"]
    assert set(body["cadence"]) == {"slow", "normal", "active"}
    for lo, hi in body["cadence"].values():
        assert 0 <= lo <= hi
    assert set(body["daily_request_ceiling"]) == {"light", "standard", "heavy"}
    assert set(body["cohort_size"]) == {"small", "medium", "large"}


# ---------------------------------------------------------------------------
# POST /admin/api/agents — creation gating


def test_create_agent_is_disabled_by_default_with_budget_config(
    seeded_db, admin_client, db_session, monkeypatch
):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)

    resp = admin_client.post(
        "/admin/api/agents", json={"username": "alice", "backfill_memory": False}
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["agent"]["persona_mode"] == "fixed"
    assert body["agent"]["user_username"] == "alice"
    assert body["agent"]["display_label"] == "alice"
    assert body["agent"]["is_enabled"] is False
    assert body["agent"]["next_run_at"] is None
    config = body["agent"]["config"]
    assert {"api_url", "model", "min_delay", "max_delay"} <= set(config)
    assert {"max_actions_per_run", "max_run_seconds"} <= set(config)
    assert "daily_request_ceiling" not in config


def test_create_random_agent_defaults_and_opt_out(
    seeded_db, admin_client, db_session, monkeypatch
):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)

    default = admin_client.post("/admin/api/agents", json={"persona_mode": "random"})
    assert default.status_code == 201
    default_data = default.get_json()["agent"]
    assert default_data["persona_mode"] == "random"
    assert default_data["user_username"] is None
    assert default_data["display_label"] == f"Random #{default_data['id']}"
    assert default_data["is_enabled"] is False
    assert default_data["config"]["backfill_memory"] is True

    opted_out = admin_client.post(
        "/admin/api/agents",
        json={"persona_mode": "random", "backfill_memory": False},
    )
    assert opted_out.status_code == 201
    opted_out_data = opted_out.get_json()["agent"]
    assert opted_out_data["persona_mode"] == "random"
    assert opted_out_data["user_username"] is None
    assert "backfill_memory" not in opted_out_data["config"]
    assert Agent.query.filter_by(persona_mode="random").count() == 2


def test_create_agent_persona_mode_validation(seeded_db, admin_client, monkeypatch):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)
    for payload in (
        {"persona_mode": "random", "username": "alice"},
        {"persona_mode": "fixed"},
        {"persona_mode": "mystery", "username": "alice"},
    ):
        response = admin_client.post("/admin/api/agents", json=payload)
        assert response.status_code == 400


def test_random_agents_have_distinct_ids_and_deterministic_list_order(
    seeded_db, admin_client, db_session, monkeypatch
):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)
    _make_agent(db_session, "alice")
    _make_agent(db_session, "bob")
    first = _make_random_agent(db_session)
    second = _make_random_agent(db_session)

    response = admin_client.get("/admin/api/agents")
    assert response.status_code == 200
    rows = response.get_json()["agents"]
    assert [row["display_label"] for row in rows] == [
        "alice",
        "bob",
        f"Random #{first.id}",
        f"Random #{second.id}",
    ]
    assert [row["persona_mode"] for row in rows[:2]] == ["fixed", "fixed"]
    assert [row["user_username"] for row in rows[:2]] == ["alice", "bob"]
    assert rows[2]["persona_mode"] == rows[3]["persona_mode"] == "random"
    assert rows[2]["user_username"] is rows[3]["user_username"] is None
    assert first.id != second.id

    detail = admin_client.get(f"/admin/api/agents/{second.id}")
    assert detail.status_code == 200
    assert detail.get_json()["id"] == second.id
    assert detail.get_json()["display_label"] == f"Random #{second.id}"
    assert admin_client.get("/admin/api/agents/999999").status_code == 404


def test_candidates_exclude_fixed_but_not_random_agents(
    seeded_db, admin_client, db_session
):
    _make_random_agent(db_session)
    candidates = admin_client.get("/admin/api/personas/candidates")
    assert candidates.status_code == 200
    usernames = {row["username"] for row in candidates.get_json()["candidates"]}
    assert {"alice", "bob"} <= usernames

    _make_agent(db_session, "alice")
    candidates = admin_client.get("/admin/api/personas/candidates")
    usernames = {row["username"] for row in candidates.get_json()["candidates"]}
    assert "alice" not in usernames
    assert "bob" in usernames


def test_create_agent_validates_input(seeded_db, admin_client, db_session, monkeypatch):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)
    _make_agent(db_session, "alice")

    missing = admin_client.post("/admin/api/agents", json={})
    assert missing.status_code == 400
    unknown_user = admin_client.post("/admin/api/agents", json={"username": "ghost"})
    assert unknown_user.status_code == 400
    duplicate = admin_client.post("/admin/api/agents", json={"username": "alice"})
    assert duplicate.status_code == 409
    bad_tier = admin_client.post(
        "/admin/api/agents", json={"username": "bob", "autonomy_tier": "wizard"}
    )
    assert bad_tier.status_code == 400
    bad_delays = admin_client.post(
        "/admin/api/agents",
        json={"username": "bob", "min_delay": 100, "max_delay": 50},
    )
    assert bad_delays.status_code == 400


def test_create_agent_gated_on_capability_error(
    seeded_db, admin_client, db_session, monkeypatch
):
    def deny(api_url, model_name, **kwargs):
        raise CapabilityError(f"Model '{model_name}' cannot do tools")

    monkeypatch.setattr(capabilities, "ensure_tools_allowed", deny)

    resp = admin_client.post("/admin/api/agents", json={"username": "alice"})
    assert resp.status_code == 400
    assert "cannot do tools" in resp.get_json()["error"]
    # Nothing was created: the cohort gate fires before any agent exists.
    assert Agent.query.count() == 0


def test_create_agent_backfills_persona_memory(
    seeded_db, admin_client, fake_llm, monkeypatch
):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)
    fake_llm.enqueue_content("Alice used to post about testing.")

    resp = admin_client.post("/admin/api/agents", json={"username": "alice"})
    assert resp.status_code == 201
    assert resp.get_json()["episodes"] == 1

    row = AgentMemory.query.one()
    assert row.kind == "backfill"
    assert row.content.startswith(BACKFILL_PREFIX)
    assert "[1/1]" in row.content
    assert "Alice used to post about testing." in row.content


# ---------------------------------------------------------------------------
# Toggle semantics


def test_toggle_enable_resets_failure_strikes_and_arms(
    seeded_db, admin_client, db_session
):
    agent = _make_agent(db_session, "alice")
    agent.consecutive_failures = 4
    agent.next_run_at = None
    db.session.commit()

    on = admin_client.post(f"/admin/api/agents/{agent.id}/toggle")
    assert on.status_code == 200
    body = on.get_json()["agent"]
    assert body["is_enabled"] is True
    assert body["consecutive_failures"] == 0
    armed = datetime.fromisoformat(body["next_run_at"])
    assert abs((datetime.utcnow() - armed).total_seconds()) < 120

    off = admin_client.post(f"/admin/api/agents/{agent.id}/toggle").get_json()["agent"]
    assert off["is_enabled"] is False
    assert off["next_run_at"] is None
    assert off["status"] == "idle"
    db.session.refresh(agent)
    assert agent.consecutive_failures == 0  # strikes stay cleared after disable


def test_toggle_unknown_agent_404(seeded_db, admin_client):
    assert admin_client.post("/admin/api/agents/9999/toggle").status_code == 404


def test_force_run_conflict_while_already_running(seeded_db, admin_client, db_session):
    agent = _make_agent(db_session, "alice")
    db.session.add(
        AgentRun(
            agent_id=agent.id,
            persona_username=agent.user_username,
            trigger="schedule",
            status="running",
            started_at=datetime.utcnow(),
        )
    )
    db.session.commit()

    resp = admin_client.post(f"/admin/api/agents/{agent.id}/force-run")
    assert resp.status_code == 409
    assert "already has a run in progress" in resp.get_json()["error"]


def test_force_run_unknown_agent_404(seeded_db, admin_client):
    assert admin_client.post("/admin/api/agents/9999/force-run").status_code == 404


def test_force_run_executes_a_full_visit(seeded_db, admin_client, db_session, fake_llm):
    agent = _make_agent(db_session, "alice", config={"min_delay": 0, "max_delay": 0})
    # Plain content twice: first answer triggers a nudge, second force-finishes.
    fake_llm.enqueue_content("Just looking around.")
    fake_llm.enqueue_content("Done, finishing.")

    resp = admin_client.post(f"/admin/api/agents/{agent.id}/force-run")
    assert resp.status_code == 200
    run = resp.get_json()["run"]
    assert run["agent_id"] == agent.id
    assert run["persona_username"] == "alice"
    assert run["trigger"] == "manual"
    assert run["status"] == "completed"
    assert run["turn_count"] == 2
    persisted = AgentRun.query.filter_by(agent_id=agent.id).one()
    assert persisted.persona_username == run["persona_username"]

    db.session.refresh(agent)
    assert agent.status == "idle"
    assert agent.last_run_at is not None
    episode = AgentMemory.query.filter_by(kind="episode").one()
    assert "without taking any tool actions" in episode.content


def test_random_force_run_snapshots_selected_persona(
    seeded_db, admin_client, db_session, fake_llm, monkeypatch
):
    monkeypatch.setattr(capabilities, "ensure_tools_allowed", _noop_tools_allowed)
    agent = _make_random_agent(
        db_session, config={"min_delay": 0, "max_delay": 0, "backfill_memory": False}
    )
    fake_llm.enqueue_content("Just looking around.")
    fake_llm.enqueue_content("Done, finishing.")

    response = admin_client.post(f"/admin/api/agents/{agent.id}/force-run")
    assert response.status_code == 200
    run = response.get_json()["run"]
    assert run["agent_id"] == agent.id
    assert run["persona_username"] in {"alice", "bob"}
    persisted = AgentRun.query.filter_by(agent_id=agent.id).one()
    assert persisted.persona_username == run["persona_username"]


def test_persona_identity_is_immutable_but_current_mode_is_idempotent(
    seeded_db, admin_client, db_session
):
    fixed = _make_agent(db_session, "alice")
    unchanged = admin_client.put(
        f"/admin/api/agents/{fixed.id}", json={"persona_mode": "fixed"}
    )
    assert unchanged.status_code == 200
    assert unchanged.get_json()["agent"]["persona_mode"] == "fixed"

    conversion = admin_client.put(
        f"/admin/api/agents/{fixed.id}", json={"persona_mode": "random"}
    )
    assert conversion.status_code == 400
    assert "immutable" in conversion.get_json()["error"]
    username_change = admin_client.put(
        f"/admin/api/agents/{fixed.id}", json={"username": "bob"}
    )
    assert username_change.status_code == 400
    assert "immutable" in username_change.get_json()["error"]

    random_agent = _make_random_agent(db_session)
    fixed_assignment = admin_client.put(
        f"/admin/api/agents/{random_agent.id}", json={"username": "bob"}
    )
    assert fixed_assignment.status_code == 400
    assert "immutable" in fixed_assignment.get_json()["error"]


# ---------------------------------------------------------------------------
# Drill-down endpoints: runs / turns / tool calls


def _seed_drilldown(db_session, agent):
    now = datetime.utcnow()
    older = AgentRun(
        agent_id=agent.id,
        persona_username=agent.user_username,
        trigger="schedule",
        status="completed",
        started_at=now - timedelta(hours=2),
        finished_at=now,
    )
    newer = AgentRun(
        agent_id=agent.id,
        persona_username=agent.user_username,
        trigger="manual",
        status="failed",
        started_at=now - timedelta(minutes=5),
        error_message="boom",
    )
    db.session.add_all([older, newer])
    db.session.flush()
    turn_one = AgentTurn(
        run_id=newer.id,
        seq=1,
        request_messages=[],
        response_message={},
        model="llama3",
        latency_ms=42,
    )
    turn_two = AgentTurn(
        run_id=newer.id,
        seq=2,
        request_messages=[],
        response_message={},
        model="llama3",
        latency_ms=7,
    )
    db.session.add_all([turn_one, turn_two])
    db.session.flush()
    call = ToolCall(
        run_id=newer.id,
        turn_id=turn_one.id,
        name="view_inbox",
        arguments={},
        result={},
        ok=False,
        error="HTTP 503: sad",
        duration_ms=12,
        created_at=now,
    )
    db.session.add(call)
    db.session.commit()
    return older, newer, turn_one, turn_two, call


def test_runs_endpoint_lists_newest_first_and_respects_limit(
    seeded_db, admin_client, db_session
):
    agent = _make_agent(db_session, "alice")
    older, newer, *_ = _seed_drilldown(db_session, agent)

    body = admin_client.get(f"/admin/api/agents/{agent.id}/runs").get_json()
    ids = [run["id"] for run in body["runs"]]
    assert ids == [newer.id, older.id]
    assert body["runs"][0]["error_message"] == "boom"

    limited = admin_client.get(f"/admin/api/agents/{agent.id}/runs?limit=1")
    assert [run["id"] for run in limited.get_json()["runs"]] == [newer.id]


def test_runs_endpoint_unknown_agent_404(seeded_db, admin_client):
    assert admin_client.get("/admin/api/agents/9999/runs").status_code == 404


def test_turns_endpoint_returns_seq_ordered_chain(seeded_db, admin_client, db_session):
    agent = _make_agent(db_session, "alice")
    _, newer, turn_one, turn_two, _ = _seed_drilldown(db_session, agent)

    body = admin_client.get(f"/admin/api/runs/{newer.id}/turns").get_json()
    assert [turn["seq"] for turn in body["turns"]] == [1, 2]
    assert body["turns"][0]["model"] == "llama3"
    assert body["turns"][0]["latency_ms"] == 42


def test_turns_endpoint_unknown_run_404(seeded_db, admin_client):
    assert admin_client.get("/admin/api/runs/9999/turns").status_code == 404


def test_tool_calls_endpoint_serializes_invocations(
    seeded_db, admin_client, db_session
):
    agent = _make_agent(db_session, "alice")
    _, _, turn_one, _, _call = _seed_drilldown(db_session, agent)

    body = admin_client.get(f"/admin/api/turns/{turn_one.id}/tool_calls").get_json()
    assert len(body["tool_calls"]) == 1
    entry = body["tool_calls"][0]
    assert entry["name"] == "view_inbox"
    assert entry["ok"] is False
    assert entry["error"] == "HTTP 503: sad"
    assert entry["created_at"] is not None


def test_tool_calls_endpoint_unknown_turn_404(seeded_db, admin_client):
    assert admin_client.get("/admin/api/turns/9999/tool_calls").status_code == 404


# ---------------------------------------------------------------------------
# Bulk start/pause


def test_start_all_resets_strikes_and_arms_only_disabled(
    seeded_db, admin_client, db_session
):
    stopped = _make_agent(db_session, "alice")
    stopped.consecutive_failures = 3
    running = _make_agent(db_session, "bob", enabled=True)
    running.next_run_at = datetime.utcnow() + timedelta(hours=1)
    db.session.commit()

    body = admin_client.post("/admin/api/agents/start-all").get_json()
    assert body == {"started": 1}
    db.session.refresh(stopped)
    assert stopped.is_enabled is True
    assert stopped.consecutive_failures == 0
    assert stopped.next_run_at is not None
    db.session.refresh(running)
    assert running.next_run_at > datetime.utcnow()


def test_pause_all_clears_wakes_and_sets_idle(seeded_db, admin_client, db_session):
    one = _make_agent(db_session, "alice", enabled=True)
    two = _make_agent(db_session, "bob", enabled=True)
    one.next_run_at = datetime.utcnow() + timedelta(hours=1)
    two.status = "running"
    db.session.commit()

    body = admin_client.post("/admin/api/agents/pause-all").get_json()
    assert body == {"paused": 2}
    for agent in (one, two):
        db.session.refresh(agent)
        assert agent.is_enabled is False
        assert agent.next_run_at is None
        assert agent.status == "idle"


# ---------------------------------------------------------------------------
# Pages and auth gating


def test_dashboard_pages_render_and_register_endpoints(
    app, seeded_db, admin_client, db_session
):
    endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
    assert "admin.agents_dashboard" in endpoints
    assert "admin.agent_detail" in endpoints

    agent = _make_agent(db_session, "alice")
    assert admin_client.get("/admin/agents").status_code == 200
    assert admin_client.get(f"/admin/agents/{agent.id}").status_code == 200


def test_admin_gate_redirects_anonymous_visitors(app, client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "sekrit-token")

    resp = client.get("/admin/api/agents")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]

    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    assert client.get("/admin/api/agents").status_code == 200


# ---------------------------------------------------------------------------
# Memory: episode summaries at run end


def test_summarize_run_counts_actions_errors_and_creations(seeded_db, db_session):
    agent = _make_agent(db_session, "alice")
    run = AgentRun(
        agent_id=agent.id,
        persona_username=agent.user_username,
        trigger="schedule",
        status="completed",
        started_at=datetime.utcnow(),
        finished_at=datetime.utcnow(),
    )
    db.session.add(run)
    db.session.flush()
    db.session.add_all(
        [
            ToolCall(
                run_id=run.id,
                name="create_post",
                arguments={},
                result={},
                ok=True,
                duration_ms=5,
            ),
            ToolCall(
                run_id=run.id,
                name="create_post",
                arguments={},
                result={},
                ok=True,
                duration_ms=6,
            ),
            ToolCall(
                run_id=run.id,
                name="view_inbox",
                arguments={},
                result={},
                ok=False,
                error="HTTP 500: upstream exploded",
                duration_ms=99,
            ),
        ]
    )
    db.session.commit()

    summarize_run(agent, run)

    row = AgentMemory.query.filter_by(
        user_username=agent.user_username, kind="episode"
    ).one()
    content = row.content
    assert content.startswith("Last visit: 3 tool action(s), 2 ok / 1 error:")
    assert "create_post x2" in content
    assert 'errored e.g. "HTTP 500' in content
    assert "Created 2 posts." in content


def test_summarize_run_without_actions_writes_idle_episode(seeded_db, db_session):
    agent = _make_agent(db_session, "alice")
    run = AgentRun(
        agent_id=agent.id,
        persona_username=agent.user_username,
        trigger="manual",
        status="completed",
        started_at=datetime.utcnow(),
        finished_at=datetime.utcnow(),
    )
    db.session.add(run)
    db.session.commit()

    summarize_run(agent, run)

    row = AgentMemory.query.filter_by(kind="episode").one()
    assert row.content == (
        "Woke up, looked around, and finished without taking any tool actions."
    )


# ---------------------------------------------------------------------------
# Memory: persona-history backfill


def test_backfill_is_idempotent_and_extractive_without_llm(seeded_db, db_session):
    _make_agent(db_session, "alice")  # backfill requires a registered agent
    inserted = backfill_persona_history("alice")  # no api_url/model -> extractive
    assert inserted == 1
    row = AgentMemory.query.filter_by(kind="backfill").one()
    assert row.content.startswith(BACKFILL_PREFIX)
    assert "Extracted summary:" in row.content
    assert "wrote 2 post(s) and 1 comment(s)" in row.content

    # Second call sees existing kind='backfill' rows and inserts nothing.
    assert backfill_persona_history("alice") == 0
    assert AgentMemory.query.filter_by(kind="backfill").count() == 1


def test_backfill_chunks_history_into_labelled_episodes(seeded_db, db_session):
    db.session.add(User(username="carol", bio="chatty carol"))
    db.session.commit()
    _make_agent(db_session, "carol")

    post = seeded_db["posts"][0]
    for index in range(16):
        db.session.add(
            Comment(
                post_id=post.id,
                user="carol",
                model="test-model",
                content=f"Opinion number {index} about forums.",
            )
        )
    db.session.commit()

    inserted = backfill_persona_history("carol")
    assert inserted == 2  # ceil(16 / 15)

    contents = [
        row.content for row in AgentMemory.query.filter_by(kind="backfill").all()
    ]
    assert any("[1/2]" in content for content in contents)
    assert any("[2/2]" in content for content in contents)


def test_backfill_falls_back_to_extractive_when_provider_raises(
    seeded_db, db_session, fake_llm
):
    _make_agent(db_session, "alice")  # backfill requires a registered agent
    fake_llm.enqueue_error(RuntimeError("provider kaboom"))

    inserted = backfill_persona_history(
        "alice", api_url="http://localhost:9999/v1", model="llama3"
    )

    assert inserted == 1
    row = AgentMemory.query.filter_by(kind="backfill").one()
    assert row.content.startswith(BACKFILL_PREFIX)
    assert "Extracted summary:" in row.content


# ---------------------------------------------------------------------------
# Memory: kickoff injection


def test_initial_messages_inject_backfills_and_recent_episodes(seeded_db, db_session):
    agent = _make_agent(db_session, "alice")
    db.session.add_all(
        [
            AgentMemory(
                user_username=agent.user_username,
                kind="backfill",
                content=f"{BACKFILL_PREFIX} she posted often",
            ),
            AgentMemory(
                user_username=agent.user_username, kind="episode", content="quiet visit"
            ),
            AgentMemory(
                user_username=agent.user_username, kind="episode", content="busy visit"
            ),
        ]
    )
    db.session.commit()

    messages = build_initial_messages(agent, db.session.get(User, "alice"))
    assert messages[0]["role"] == "system"
    kickoff = messages[-1]["content"]
    assert "Your memory:" in kickoff
    assert f"- {BACKFILL_PREFIX} she posted often" in kickoff
    assert "Recent visits:" in kickoff
    assert "- busy visit" in kickoff  # newest episode listed first
    assert "- quiet visit" in kickoff


def test_initial_messages_have_no_memory_block_when_empty(seeded_db, db_session):
    agent = _make_agent(db_session, "alice")

    messages = build_initial_messages(agent, db.session.get(User, "alice"))

    assert "Your memory:" not in messages[-1]["content"]
