"""Coverage for the `deaddit agent` CLI group and flag-off inertness."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from deaddit.agents.loop import is_runtime_enabled
from deaddit.cli import cli
from deaddit.models import Agent, AgentRun, User


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def agents_cli():
    # Import the module object directly: deaddit.agents uses a lazy
    # __getattr__ that breaks dotted-string monkeypatch targets.
    from deaddit.agents import cli as agents_cli_module

    return agents_cli_module


@pytest.fixture()
def patch_cli_app(monkeypatch, app, agents_cli):
    """Point the CLI's create_app at the test app (no instance DB)."""
    monkeypatch.setattr(agents_cli, "create_app", lambda *a, **k: app)


@pytest.fixture()
def probe_recorder(monkeypatch, agents_cli):
    """Replace ensure_tools_allowed with a recorder; returns call list."""
    calls = []
    monkeypatch.setattr(
        agents_cli,
        "ensure_tools_allowed",
        lambda api_url, model, *, auto_probe=False: calls.append(
            (api_url, model, auto_probe)
        ),
    )
    return calls


# ---------------------------------------------------------------------------
# Help surface


def test_agent_help_lists_commands(runner):
    result = runner.invoke(cli, ["agent", "--help"])

    assert result.exit_code == 0
    for command in ("create", "list", "run-once"):
        assert command in result.output


# ---------------------------------------------------------------------------
# create


@pytest.mark.parametrize(
    "persona_args",
    [[], ["--username", "alice", "--random-persona"]],
    ids=["neither", "both"],
)
def test_create_requires_exactly_one_persona_choice(runner, persona_args):
    result = runner.invoke(cli, ["agent", "create", *persona_args])

    assert result.exit_code != 0
    assert "Exactly one of --username or --random-persona is required." in result.output


def test_create_rejects_unknown_persona(runner, patch_cli_app):
    result = runner.invoke(cli, ["agent", "create", "--username", "ghost"])

    assert result.exit_code != 0
    assert "does not exist" in result.output
    assert Agent.query.count() == 0


def test_create_random_personas_inserts_rows_and_configures_backfill(
    runner, patch_cli_app, probe_recorder
):
    args = ["agent", "create", "--random-persona", "--backfill-memory"]
    first = runner.invoke(cli, args)
    second = runner.invoke(cli, args)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    rows = Agent.query.order_by(Agent.id).all()
    assert len(rows) == 2
    assert all(row.persona_mode == "random" for row in rows)
    assert all(row.user_username is None for row in rows)
    assert all(row.config["backfill_memory"] is True for row in rows)
    assert len(probe_recorder) == 2


def test_create_fixed_persona_upserts_and_sets_fixed_mode(
    runner, patch_cli_app, probe_recorder, seeded_db
):
    first = runner.invoke(cli, ["agent", "create", "--username", "alice"])
    second = runner.invoke(
        cli, ["agent", "create", "--username", "alice", "--tier", "lurker"]
    )

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    rows = Agent.query.filter_by(user_username="alice").all()
    assert len(rows) == 1
    assert rows[0].persona_mode == "fixed"
    assert rows[0].autonomy_tier == "lurker"
    assert len(probe_recorder) == 2


def test_create_persists_agent_with_config(
    runner, patch_cli_app, probe_recorder, seeded_db
):
    result = runner.invoke(
        cli,
        ["agent", "create", "--username", "alice", "--enable"],
    )

    assert result.exit_code == 0, result.output
    agent = Agent.query.filter_by(user_username="alice").one()
    assert agent.autonomy_tier == "regular"
    assert agent.persona_mode == "fixed"
    assert agent.is_enabled is True
    config = agent.config
    assert set(config) == {
        "api_url",
        "model",
        "min_delay",
        "max_delay",
        "max_actions_per_run",
        "max_run_seconds",
    }
    assert config["min_delay"] == 60
    assert config["max_delay"] == 900

    # The happy path probes the endpoint once with auto_probe before saving.
    assert len(probe_recorder) == 1
    api_url, model, auto_probe = probe_recorder[0]
    assert auto_probe is True
    assert api_url == config["api_url"]
    assert model == config["model"]


# ---------------------------------------------------------------------------
# list


def test_list_reports_registered_agents(
    runner, patch_cli_app, probe_recorder, seeded_db
):
    assert runner.invoke(cli, ["agent", "create", "--username", "alice"]).exit_code == 0
    assert (
        runner.invoke(cli, ["agent", "create", "--random-persona"]).exit_code == 0
    )
    fixed = Agent.query.filter_by(persona_mode="fixed").one()
    random = Agent.query.filter_by(persona_mode="random").one()

    result = runner.invoke(cli, ["agent", "list"])

    assert result.exit_code == 0
    assert "id" in result.output
    assert "mode" in result.output
    assert f"{fixed.id} fixed" in result.output
    assert f"{random.id} random" in result.output
    assert f"Random #{random.id}" in result.output
    assert "alice" in result.output
    assert "No agents registered." not in result.output


def test_list_empty_outputs_placeholder(runner, patch_cli_app):
    result = runner.invoke(cli, ["agent", "list"])

    assert result.exit_code == 0
    assert "No agents registered." in result.output


# ---------------------------------------------------------------------------
# run-once


def test_run_once_uses_numeric_agent_id_and_reports_selected_persona(
    runner, patch_cli_app, agents_cli, db_session, monkeypatch
):
    user = User(username="selected_persona")
    db_session.add(user)
    db_session.flush()
    agent = Agent(
        persona_mode="random",
        user_username=None,
        autonomy_tier="regular",
        config={},
        state={},
    )
    db_session.add(agent)
    db_session.commit()
    calls = []

    def fake_run_once(agent_id, *, trigger, force_intent):
        calls.append((agent_id, trigger, force_intent))
        run = AgentRun(
            agent_id=agent_id,
            persona_username=user.username,
            trigger=trigger,
            status="completed",
            turn_count=1,
            action_count=0,
            token_usage={"total_tokens": 3},
        )
        db_session.add(run)
        db_session.commit()
        return run

    monkeypatch.setattr(agents_cli, "run_once", fake_run_once)
    result = runner.invoke(cli, ["agent", "run-once", str(agent.id)])

    assert result.exit_code == 0, result.output
    assert calls == [(agent.id, "manual", None)]
    assert f"run {AgentRun.query.one().id}: agent_id={agent.id}" in result.output
    assert "persona=selected_persona status=completed trigger=manual" in result.output


# ---------------------------------------------------------------------------
# Flag-off proof of zero background activity


def test_fresh_boot_starts_nothing(app, db_session):
    """Booting the app with the flag off must not create runs or schedule."""
    from deaddit import Config

    assert Config.DEFAULTS["AGENT_RUNTIME_ENABLED"] == "false"
    assert is_runtime_enabled() is False
    # No scheduler has been started: no run rows exist after plain boot.
    assert AgentRun.query.count() == 0
