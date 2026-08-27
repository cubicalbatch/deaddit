"""Coverage for the `deaddit agent` CLI group and flag-off inertness."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from deaddit.agents.loop import is_runtime_enabled
from deaddit.cli import cli
from deaddit.models import Agent, AgentRun


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


def test_create_rejects_unknown_persona(runner, patch_cli_app):
    result = runner.invoke(cli, ["agent", "create", "--username", "ghost"])

    assert result.exit_code != 0
    assert "does not exist" in result.output
    assert Agent.query.count() == 0


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

    result = runner.invoke(cli, ["agent", "list"])

    assert result.exit_code == 0
    assert "alice" in result.output
    assert "No agents registered." not in result.output


def test_list_empty_outputs_placeholder(runner, patch_cli_app):
    result = runner.invoke(cli, ["agent", "list"])

    assert result.exit_code == 0
    assert "No agents registered." in result.output


# ---------------------------------------------------------------------------
# Flag-off proof of zero background activity


def test_fresh_boot_starts_nothing(app, db_session):
    """Booting the app with the flag off must not create runs or schedule."""
    from deaddit import Config

    assert Config.DEFAULTS["AGENT_RUNTIME_ENABLED"] == "false"
    assert is_runtime_enabled() is False
    # No scheduler has been started: no run rows exist after plain boot.
    assert AgentRun.query.count() == 0
