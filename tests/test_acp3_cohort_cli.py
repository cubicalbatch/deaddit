"""Coverage for the AC-P3 cohort spec and `deaddit agent create-cohort`.

All LLM-touching paths go through tests.fakes.FakeProvider; no network.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from deaddit.agents.cohort import (
    COHORT_SPEC_VERSION,
    CohortSpecError,
    load_spec,
    validate_spec,
)
from deaddit.agents.loop import DEFAULT_CONFIG
from deaddit.cli import cli
from deaddit.models import Agent, User

ENDPOINT = {"api_url": "http://llm.test/v1", "model": "test-model"}

# Staggered cadence bounds inside the 240..3600 window, min < max.
_CADENCES = [
    (300, 1500),
    (360, 1600),
    (420, 1800),
    (480, 1900),
    (540, 2000),
    (600, 2400),
    (720, 2600),
    (900, 2800),
    (1200, 3200),
    (1500, 3600),
]
_TIERS = [
    "power_user",
    "power_user",
    "regular",
    "regular",
    "regular",
    "regular",
    "regular",
    "regular",
    "lurker",
    "regular",
]


def _default_agents():
    return [
        {
            "username": f"user_{i}",
            "tier": _TIERS[i],
            "min_delay": lo,
            "max_delay": hi,
        }
        for i, (lo, hi) in enumerate(_CADENCES)
    ]


def _spec(agents=None):
    return {
        "version": COHORT_SPEC_VERSION,
        "endpoint": dict(ENDPOINT),
        "agents": _default_agents() if agents is None else agents,
    }


def _write_spec(tmp_path, spec) -> str:
    path = tmp_path / "cohort.json"
    path.write_text(json.dumps(spec))
    return str(path)


def _enqueue_ok_probe(fake_llm) -> None:
    """One schema-valid echo tool call: the probe-success verdict."""
    fake_llm.enqueue_tool_calls(
        [
            {
                "id": "call_probe",
                "type": "function",
                "function": {
                    "name": "echo_probe",
                    "arguments": '{"message": "ping"}',
                },
            }
        ]
    )


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def agents_cli():
    # Module object directly: deaddit.agents lazy __getattr__ breaks
    # dotted-string monkeypatch targets.
    from deaddit.agents import cli as agents_cli_module

    return agents_cli_module


@pytest.fixture()
def patch_cli_app(monkeypatch, app, agents_cli):
    monkeypatch.setattr(agents_cli, "create_app", lambda *a, **k: app)


@pytest.fixture()
def seeded_users(app, db_session):
    names = [f"user_{i}" for i in range(10)]
    db_session.add_all([User(username=name, bio=f"bio {name}") for name in names])
    db_session.commit()
    return names


# ---------------------------------------------------------------------------
# Spec validation


def test_shipped_cohort_spec_is_valid():
    with open("deaddit/agents/parity_cohort.json") as fh:
        spec = json.load(fh)
    assert validate_spec(spec) == []


def test_valid_spec_passes():
    assert validate_spec(_spec()) == []


@pytest.mark.parametrize(
    ("mutation", "expected_fragment"),
    [
        pytest.param(
            lambda s: s.update(agents=s["agents"][:7]),
            "between 8 and 15",
            id="too-small",
        ),
        pytest.param(
            lambda s: s.update(
                agents=s["agents"]
                + [dict(s["agents"][0], username=f"extra_{i}") for i in range(6)]
            ),
            "between 8 and 15",
            id="too-large",
        ),
        pytest.param(
            lambda s: s["agents"][1].update(username=s["agents"][0]["username"]),
            "duplicate username 'user_0'",
            id="duplicate-username",
        ),
        pytest.param(
            lambda s: s["agents"][0].update(tier="admin"),
            "must be one of",
            id="bad-tier",
        ),
        pytest.param(
            lambda s: s["agents"][0].update(min_delay=2000, max_delay=300),
            "min_delay (2000) must be <= max_delay (300)",
            id="min-above-max",
        ),
        pytest.param(
            lambda s: s["agents"][0].update(max_actions_per_run=31),
            "max_actions_per_run: guardrail cap must be 30",
            id="wrong-action-cap",
        ),
        pytest.param(
            lambda s: s["agents"][0].update(max_run_seconds=400),
            "max_run_seconds: guardrail cap must be 300",
            id="wrong-run-cap",
        ),
        pytest.param(
            lambda s: s["agents"][0].update(horse_shoes=4),
            "unknown key(s): horse_shoes",
            id="unknown-key",
        ),
        pytest.param(
            lambda s: s.update(endpoint={"model": "m"}),
            "endpoint.api_url: required non-empty string",
            id="missing-api-url",
        ),
        pytest.param(
            lambda s: s.update(endpoint={"api_url": "  ", "model": "m"}),
            "endpoint.api_url: required non-empty string",
            id="blank-api-url",
        ),
        pytest.param(
            lambda s: s["agents"][0].update(min_delay=0),
            "min_delay: must be a positive integer",
            id="zero-min-delay",
        ),
        pytest.param(
            lambda s: s["agents"][0].update(daily_request_ceiling=-5),
            "daily_request_ceiling: must be a positive integer",
            id="bad-ceiling",
        ),
    ],
)
def test_each_rule_violation_is_reported(mutation, expected_fragment):
    spec = _spec()
    mutation(spec)
    problems = validate_spec(spec)
    assert any(expected_fragment in problem for problem in problems), problems


def test_guardrail_caps_at_exact_defaults_are_accepted():
    spec = _spec()
    spec["agents"][0]["max_actions_per_run"] = DEFAULT_CONFIG["max_actions_per_run"]
    spec["agents"][0]["max_run_seconds"] = DEFAULT_CONFIG["max_run_seconds"]
    assert validate_spec(spec) == []


def test_load_spec_lists_all_problems_at_once(tmp_path):
    spec = _spec(agents=_default_agents()[:3])
    del spec["endpoint"]["model"]
    spec["agents"][0]["tier"] = "wizard"
    path = _write_spec(tmp_path, spec)
    with pytest.raises(CohortSpecError) as excinfo:
        load_spec(path)
    assert isinstance(excinfo.value, ValueError)
    message = str(excinfo.value)
    assert "endpoint.model: required non-empty string" in message
    assert "between 8 and 15" in message
    assert "agents[0].tier" in message


def test_load_spec_rejects_missing_file(tmp_path):
    with pytest.raises(CohortSpecError, match="not found"):
        load_spec(str(tmp_path / "nope.json"))


def test_load_spec_rejects_invalid_json(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json")
    with pytest.raises(CohortSpecError, match="invalid JSON"):
        load_spec(str(path))


# ---------------------------------------------------------------------------
# create-cohort against the test app


def _invoke_cohort(runner, spec_path, *extra):
    return runner.invoke(cli, ["agent", "create-cohort", "--spec", spec_path, *extra])


def test_create_cohort_creates_disabled_rows_inside_guardrails(
    runner, patch_cli_app, seeded_users, fake_llm, tmp_path
):
    _enqueue_ok_probe(fake_llm)
    result = _invoke_cohort(runner, _write_spec(tmp_path, _spec()))
    assert result.exit_code == 0, result.output

    rows = Agent.query.order_by(Agent.user_username).all()
    assert len(rows) == 10
    entries = {entry["username"]: entry for entry in _spec()["agents"]}
    for row in rows:
        entry = entries[row.user_username]
        config = row.config
        assert row.is_enabled is False  # decision 1: nothing runs by default
        assert row.status == "idle"
        assert row.autonomy_tier == entry["tier"]
        assert config["api_url"] == ENDPOINT["api_url"]
        assert config["model"] == ENDPOINT["model"]
        assert config["min_delay"] == entry["min_delay"]
        assert config["max_delay"] == entry["max_delay"]
        assert (
            config["max_actions_per_run"] == DEFAULT_CONFIG["max_actions_per_run"] == 30
        )
        assert config["max_run_seconds"] == DEFAULT_CONFIG["max_run_seconds"] == 300
        assert "daily_request_ceiling" not in config
    assert f"Cohort v{COHORT_SPEC_VERSION}: 10 agents" in result.output
    assert "probe evidence:" in result.output


def test_create_cohort_persists_daily_request_ceilings(
    runner, patch_cli_app, seeded_users, fake_llm, tmp_path
):
    _enqueue_ok_probe(fake_llm)
    spec = _spec()
    for index, entry in enumerate(spec["agents"]):
        entry["daily_request_ceiling"] = 240 - index * 10
    result = _invoke_cohort(runner, _write_spec(tmp_path, spec))
    assert result.exit_code == 0, result.output
    for row in Agent.query.all():
        index = int(row.user_username.split("_")[1])
        expected = 240 - index * 10
        assert row.config["daily_request_ceiling"] == expected
        assert f"ceiling={expected}" in result.output


def test_create_cohort_is_idempotent(
    runner, patch_cli_app, seeded_users, fake_llm, tmp_path
):
    _enqueue_ok_probe(fake_llm)
    spec_path = _write_spec(tmp_path, _spec())
    first = _invoke_cohort(runner, spec_path)
    assert first.exit_code == 0, first.output
    # Second run: capability verdict is cached -> no second probe needed.
    second = _invoke_cohort(runner, spec_path, "--no-backfill-memory")
    assert second.exit_code == 0, second.output
    assert Agent.query.count() == 10  # updates, never duplicates


def test_enable_flag_enables_rows(
    runner, patch_cli_app, seeded_users, fake_llm, tmp_path
):
    _enqueue_ok_probe(fake_llm)
    result = _invoke_cohort(runner, _write_spec(tmp_path, _spec()), "--enable")
    assert result.exit_code == 0, result.output
    assert all(row.is_enabled for row in Agent.query.all())


def test_probe_gate_blocks_every_write_on_non_tool_response(
    runner, patch_cli_app, seeded_users, fake_llm, tmp_path
):
    fake_llm.enqueue_content("Sorry, I cannot call tools.")
    result = _invoke_cohort(runner, _write_spec(tmp_path, _spec()))
    assert result.exit_code != 0
    assert "does not support tools" in result.output
    assert Agent.query.count() == 0  # gate fires before any write
    assert len(fake_llm.requests) == 1  # exactly one probe for the cohort


def test_backfill_flag_off_skips_memory_writes(
    runner, patch_cli_app, seeded_users, fake_llm, monkeypatch, agents_cli, tmp_path
):
    _enqueue_ok_probe(fake_llm)

    def _boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("backfill_persona_history must not be called")

    monkeypatch.setattr(agents_cli, "backfill_persona_history", _boom)
    result = _invoke_cohort(
        runner, _write_spec(tmp_path, _spec()), "--no-backfill-memory"
    )
    assert result.exit_code == 0, result.output
    assert "episodes=0" in result.output


def test_backfill_default_on_reports_episode_counts(
    runner, patch_cli_app, seeded_users, fake_llm, monkeypatch, agents_cli, tmp_path
):
    _enqueue_ok_probe(fake_llm)
    calls = []

    def _fake_backfill(user_username, *, api_url=None, model=None):
        calls.append(user_username)
        assert api_url == ENDPOINT["api_url"]
        assert model == ENDPOINT["model"]
        return 7

    monkeypatch.setattr(agents_cli, "backfill_persona_history", _fake_backfill)
    result = _invoke_cohort(runner, _write_spec(tmp_path, _spec()))
    assert result.exit_code == 0, result.output
    assert len(calls) == 10
    assert result.output.count("episodes=7") == 10


def test_backfill_failure_becomes_warning_not_abort(
    runner, patch_cli_app, seeded_users, fake_llm, monkeypatch, agents_cli, tmp_path
):
    _enqueue_ok_probe(fake_llm)

    def _fail(user_username, **kwargs):
        raise RuntimeError("memory store offline")

    monkeypatch.setattr(agents_cli, "backfill_persona_history", _fail)
    result = _invoke_cohort(runner, _write_spec(tmp_path, _spec()))
    assert result.exit_code == 0, result.output
    assert Agent.query.count() == 10
    assert "backfill failed: memory store offline" in result.output


# ---------------------------------------------------------------------------
# --db override routing


def _alt_app(db_uri):
    from deaddit import create_app

    return create_app({"SQLALCHEMY_DATABASE_URI": db_uri})


def _seed_alt_db(app, names):
    from deaddit.extensions import db as _db

    with app.app_context():
        _db.create_all()
        _db.session.add_all([User(username=n, bio="b") for n in names])
        _db.session.commit()


def test_db_flag_routes_rows_to_alternate_file(runner, tmp_path, fake_llm):
    """The --db group option lands Agent rows in the named file DB."""
    _enqueue_ok_probe(fake_llm)
    db_uri = f"sqlite:///{tmp_path / 'cohort.db'}"
    names = [f"solo_{i}" for i in range(8)]
    _seed_alt_db(_alt_app(db_uri), names)
    spec_path = _write_spec(
        tmp_path,
        {
            "version": COHORT_SPEC_VERSION,
            "endpoint": dict(ENDPOINT),
            "agents": [
                {"username": n, "tier": "regular", "min_delay": 300, "max_delay": 1500}
                for n in names
            ],
        },
    )

    result = runner.invoke(
        cli,
        [
            "agent",
            "--db",
            db_uri,
            "create-cohort",
            "--spec",
            spec_path,
            "--no-backfill-memory",
        ],
    )
    assert result.exit_code == 0, result.output

    app = _alt_app(db_uri)
    with app.app_context():
        rows = Agent.query.all()
        assert len(rows) == 8
        assert all(row.is_enabled is False for row in rows)
        assert rows[0].config["api_url"] == ENDPOINT["api_url"]
    assert (tmp_path / "cohort.db").exists()


def test_db_envvar_is_honoured(runner, tmp_path, monkeypatch, fake_llm):
    """DEADDIT_DB_URI routes without the flag."""
    _enqueue_ok_probe(fake_llm)
    db_uri = f"sqlite:///{tmp_path / 'env.db'}"
    names = [f"env_{i}" for i in range(8)]
    _seed_alt_db(_alt_app(db_uri), names)
    spec_path = _write_spec(
        tmp_path,
        {
            "version": COHORT_SPEC_VERSION,
            "endpoint": dict(ENDPOINT),
            "agents": [
                {"username": n, "tier": "regular", "min_delay": 240, "max_delay": 3600}
                for n in names
            ],
        },
    )
    monkeypatch.setenv("DEADDIT_DB_URI", db_uri)
    result = runner.invoke(
        cli,
        ["agent", "create-cohort", "--spec", spec_path, "--no-backfill-memory"],
    )
    assert result.exit_code == 0, result.output
    with _alt_app(db_uri).app_context():
        assert Agent.query.count() == 8


def test_db_override_queries_alternate_db_not_default(runner, tmp_path, fake_llm):
    """Personas missing from the overridden DB fail against THAT db only:
    proof the override reaches the persona lookup, not just writes."""
    _enqueue_ok_probe(fake_llm)
    db_uri = f"sqlite:///{tmp_path / 'empty.db'}"
    _seed_alt_db(_alt_app(db_uri), [])  # empty: no personas at all
    spec_path = _write_spec(tmp_path, _spec())
    result = runner.invoke(
        cli,
        [
            "agent",
            "--db",
            db_uri,
            "create-cohort",
            "--spec",
            spec_path,
        ],
    )
    assert result.exit_code != 0
    assert "does not exist" in result.output
    with _alt_app(db_uri).app_context():
        assert Agent.query.count() == 0
