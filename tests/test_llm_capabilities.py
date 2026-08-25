"""Phase LLM-2 Slice B: capability probing, gating, admin page, migration.

Covers the ``endpoint_capability`` verdict table and deaddit.llm.capabilities:
probe outcomes are VERDICTS (Resolution 11), manual overrides win over
probes, transient failures record nothing, and the Alembic revision builds
the table on a throwaway sqlite file (never the live instance DB).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from deaddit import create_app
from deaddit.llm import capabilities
from deaddit.llm.capabilities import (
    ensure_tools_allowed,
    get_capability,
    mark_stale,
    probe_endpoint,
    set_manual_override,
)
from deaddit.llm.errors import CapabilityError, TransientLLMError
from deaddit.models import EndpointCapability

API_URL = "http://llm.test/v1"
MODEL = "test-model"


def _tool_call(arguments) -> list[dict]:
    """A valid OpenAI tool_calls envelope for the probe's echo tool."""
    return [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "echo_probe", "arguments": arguments},
        }
    ]


# ---------------------------------------------------------------------------
# probe_endpoint


def test_probe_success_records_supported_verdict(app, db_session, fake_llm):
    fake_llm.enqueue_tool_calls(
        _tool_call(json.dumps({"message": "ping"})), content="done"
    )
    cap = probe_endpoint(API_URL, MODEL)

    assert cap.supports_tools is True
    assert cap.probe_method == "probe"
    assert cap.probed_at is not None
    assert get_capability(API_URL, MODEL).supports_tools is True

    # The probe request forced the echo tool.
    payload = fake_llm.requests[0]["payload"]
    assert payload["model"] == MODEL
    assert len(payload["tools"]) == 1
    assert payload["tools"][0]["type"] == "function"
    assert payload["tools"][0]["function"]["name"] == "echo_probe"
    assert payload["tool_choice"] == {
        "type": "function",
        "function": {"name": "echo_probe"},
    }

    # Raw echo-test evidence is kept for AGENT_START-style verification.
    assert capabilities.LAST_PROBE_EVIDENCE == {
        "response_id": None,  # FakeProvider responses carry no id
        "finish_reason": None,
        "tool_name": "echo_probe",
        "arguments": {"message": "ping"},
    }


@pytest.mark.parametrize(
    "arguments",
    [
        "{not valid json",  # garbage arguments JSON
        json.dumps({"wrong_field": "x"}),  # schema mismatch: missing field
        json.dumps({"message": 123}),  # schema mismatch: wrong type
    ],
    ids=["garbage-json", "missing-field", "wrong-type"],
)
def test_probe_with_schema_invalid_args_is_verdict_false(
    app, db_session, fake_llm, arguments
):
    # Schema-invalid args inside a valid tool_calls envelope mean the
    # endpoint's tool support is UNRELIABLE. That is recorded as a False
    # VERDICT (not an error): gating must treat unreliable tools as no
    # tools, per Resolution 11 — a failed probe is never a fallback trigger.
    fake_llm.enqueue_tool_calls(_tool_call(arguments))
    cap = probe_endpoint(API_URL, MODEL)

    assert cap.supports_tools is False
    assert cap.probe_method == "probe"
    # Evidence shows the envelope was seen but args failed validation.
    assert capabilities.LAST_PROBE_EVIDENCE is not None
    assert capabilities.LAST_PROBE_EVIDENCE["arguments"] is None
    assert capabilities.LAST_PROBE_EVIDENCE["tool_name"] == "echo_probe"


def test_probe_missing_tool_calls_is_verdict_false(app, db_session, fake_llm):
    fake_llm.enqueue_content("I will not use tools.")
    cap = probe_endpoint(API_URL, MODEL)
    assert cap.supports_tools is False


def test_probe_http400_tools_error_is_verdict_false(app, db_session, fake_llm):
    from deaddit.llm.errors import PermanentLLMError

    fake_llm.enqueue_error(
        PermanentLLMError("HTTP 400: this model does not support function calling")
    )
    cap = probe_endpoint(API_URL, MODEL)
    assert cap.supports_tools is False
    assert get_capability(API_URL, MODEL) is not None


def test_probe_transient_error_records_nothing_and_reraises(app, db_session, fake_llm):
    fake_llm.enqueue_error(TransientLLMError("connection timed out after retries"))
    with pytest.raises(TransientLLMError):
        probe_endpoint(API_URL, MODEL)
    assert get_capability(API_URL, MODEL) is None


def test_probe_manual_override_wins(app, db_session, fake_llm):
    set_manual_override(API_URL, MODEL, True)
    before = len(fake_llm._queue)

    cap = probe_endpoint(API_URL, MODEL)

    assert cap.probe_method == "manual"
    assert cap.supports_tools is True
    # Refused to overwrite: no provider call was even attempted.
    assert len(fake_llm._queue) == before


# ---------------------------------------------------------------------------
# ensure_tools_allowed / mark_stale


def test_ensure_raises_capabilityerror_on_false_verdict(app, db_session):
    db_session.add(
        EndpointCapability(
            api_url=API_URL,
            model_name=MODEL,
            supports_tools=False,
            probe_method="probe",
        )
    )
    db_session.commit()
    with pytest.raises(CapabilityError):
        ensure_tools_allowed(API_URL, MODEL, request_id="req-1")


def test_ensure_passes_for_manual_true_even_after_failed_probes(app, db_session):
    set_manual_override(API_URL, MODEL, True)
    ensure_tools_allowed(API_URL, MODEL)


def test_ensure_no_row_passes_without_probing(app, db_session):
    ensure_tools_allowed(API_URL, MODEL)  # no lazy probe when auto_probe=False
    assert get_capability(API_URL, MODEL) is None


def test_ensure_auto_probe_runs_when_no_row(app, db_session, fake_llm):
    fake_llm.enqueue_tool_calls(_tool_call(json.dumps({"message": "ping"})))
    ensure_tools_allowed(API_URL, MODEL, auto_probe=True)
    cap = get_capability(API_URL, MODEL)
    assert cap is not None and cap.supports_tools is True


def test_mark_stale_deletes_row(app, db_session):
    set_manual_override(API_URL, MODEL, False)
    mark_stale(API_URL, MODEL)
    assert get_capability(API_URL, MODEL) is None
    ensure_tools_allowed(API_URL, MODEL)  # stale row gates nothing


# ---------------------------------------------------------------------------
# Admin page


def _seed_verdict(db_session, supports_tools=False, method="probe"):
    db_session.add(
        EndpointCapability(
            api_url=API_URL,
            model_name=MODEL,
            supports_tools=supports_tools,
            probe_method=method,
        )
    )
    db_session.commit()


def test_admin_capabilities_page_shows_verdict(app, client, db_session):
    _seed_verdict(db_session, supports_tools=False)
    resp = client.get("/admin/capabilities")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert API_URL in html
    assert MODEL in html
    assert "Not supported" in html
    assert "probe" in html


def test_admin_probe_route_records_verdict_end_to_end(app, client, fake_llm):
    fake_llm.enqueue_tool_calls(_tool_call(json.dumps({"message": "ping"})))
    resp = client.post(
        "/admin/capabilities/probe",
        data={"api_url": API_URL, "model_name": MODEL},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    cap = get_capability(API_URL, MODEL)
    assert cap is not None and cap.supports_tools is True
    html = resp.get_data(as_text=True)
    assert "Supported" in html


def test_admin_override_route_sets_manual_row(app, client, db_session):
    resp = client.post(
        "/admin/capabilities/override",
        data={"api_url": API_URL, "model_name": MODEL, "supports_tools": "false"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    cap = get_capability(API_URL, MODEL)
    assert cap.supports_tools is False
    assert cap.probe_method == "manual"


# ---------------------------------------------------------------------------
# Migration (tmp sqlite only — never the live instance DB)


_EXPECTED_COLUMNS = {
    "api_url",
    "model_name",
    "supports_tools",
    "supports_streaming",
    "context_tokens",
    "probed_at",
    "probe_method",
}


def _table_info(db_path):
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("PRAGMA table_info(endpoint_capability)").fetchall()
    finally:
        conn.close()
    return rows


def test_migration_upgrade_creates_table_and_downgrade_round_trips(tmp_path):
    db_path = tmp_path / "mig.db"
    app = create_app(
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "TESTING": True}
    )
    runner = app.test_cli_runner()

    result = runner.invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output

    rows = _table_info(db_path)
    columns = {r[1] for r in rows}
    assert columns == _EXPECTED_COLUMNS
    primary_key = sorted(r[1] for r in rows if r[5])
    assert primary_key == ["api_url", "model_name"]

    # One step back removes the table; forward again restores it.
    down = runner.invoke(args=["db", "downgrade", "5b2dab0b6816"])
    assert down.exit_code == 0, down.output
    assert _table_info(db_path) == []

    up = runner.invoke(args=["db", "upgrade"])
    assert up.exit_code == 0, up.output
    assert {r[1] for r in _table_info(db_path)} == _EXPECTED_COLUMNS
