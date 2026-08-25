"""AC-P3 parity CLI wiring tests (offline, deterministic).

Covers the two pure-sqlite3 ``agent`` subcommands backed by
:mod:`deaddit.agents.parity`:

- ``parity-report`` renders every criterion verdict (PASS / FAIL /
  INDETERMINATE) and ``--json`` round-trips the exact report dict.
- ``sample-packet`` is deterministic for a fixed seed, honours ``-o``,
  and fails cleanly when candidates are insufficient.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from click.testing import CliRunner

from deaddit.agents.cli import agent
from deaddit.agents.parity import compute_report, connect_ro
from tests.test_acp3_parity_harness import _make_db

WS = "2026-08-20 00:00:00"
WE = "2026-08-21 00:00:00"

# Trailing legacy baseline strictly before WS: 14 posts + 21 comments over
# 7 baseline days -> 5.0 items/day total.
_N_BASE_POSTS = 14
_N_BASE_COMMENTS = 21


def _seed_baseline(conn) -> None:
    for i in range(_N_BASE_POSTS):
        conn.execute(
            "INSERT INTO post (id, title, content, subdeaddit_name, user,"
            " created_at, model) VALUES (?, 't', 'c', 'python', ?, ?, NULL)",
            (100 + i, f"h{i % 3}", f"2026-08-{13 + i // 7:02d} {i % 24:02d}:00:00"),
        )
    for i in range(_N_BASE_COMMENTS):
        conn.execute(
            "INSERT INTO comment (id, post_id, parent_id, content, user,"
            " created_at, model) VALUES (?, 1, NULL, 'c', ?, ?, NULL)",
            (200 + i, f"h{i % 3}", f"2026-08-{13 + i // 11:02d} {i % 24:02d}:30:00"),
        )


def _seed_agent_content(conn, *, n_posts=2, n_comments=4) -> None:
    """In-window agent-stamped rows: ratio 6/5 = 1.2 (criterion a PASS)."""
    for i in range(n_posts):
        conn.execute(
            "INSERT INTO post (id, title, content, subdeaddit_name, user,"
            " created_at, model) VALUES (?, 't', 'c', 'python', ?, ?, ?)",
            (1 + i, f"A{i % 2}", f"2026-08-20 0{i}:00:00", "agent:eve"),
        )
    for i in range(n_comments):
        conn.execute(
            "INSERT INTO comment (id, post_id, parent_id, content, user,"
            " created_at, model) VALUES (?, 1, NULL, 'c', ?, ?, ?)",
            (10 + i, "A0", f"2026-08-20 1{i}:00:00", "agent:eve"),
        )


def _seed_tool_calls(conn, attempts, rejections) -> None:
    """Spread write attempts across the window; first `rejections` carry the
    duplicate marker."""
    from deaddit.agents.parity import DUP_REJECTION_MARKER

    for i in range(attempts):
        result = DUP_REJECTION_MARKER if i < rejections else '{"status": "ok"}'
        conn.execute(
            "INSERT INTO tool_call (id, name, result, created_at)"
            " VALUES (?, 'create_post', ?, ?)",
            (500 + i, result, f"2026-08-20 {i % 24:02d}:{i // 24:02d}:00"),
        )


def _build_report_db(path, *, tool_attempts=20, dup_rejections=1,
                     runs=(), usage_rows=True):
    """Known-stats DB: criterion a PASS, b PASS at 1/20, c INDETERMINATE."""
    conn = _make_db(path)
    _seed_baseline(conn)
    _seed_agent_content(conn)
    _seed_tool_calls(conn, tool_attempts, dup_rejections)
    if usage_rows:
        conn.execute(
            "INSERT INTO llm_usage (id, created_at, status, prompt_tokens,"
            " completion_tokens, total_tokens, estimated_cost)"
            " VALUES (1, ?, 'ok', 100, 50, 150, 0.002)", (WS,))
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def report_db(tmp_path):
    return _build_report_db(tmp_path / "report.db")


@pytest.fixture()
def runner():
    return CliRunner()


def test_parity_report_renders_all_verdicts(runner, report_db):
    result = runner.invoke(
        agent,
        ["parity-report", "--db", str(report_db),
         "--window-start", WS, "--window-end", WE],
    )
    assert result.exit_code == 0
    out = result.output
    # Header lines with the numbers behind each criterion.
    assert "baseline:" in out and "5.00 total/day" in out
    assert "6.00 total/day" in out and "(ratio 1.200)" in out
    assert "criterion a" in out and "-> PASS" in out
    assert "criterion b" in out and "1/20" in out and "5.0%" in out \
        and "-> PASS" in out
    # No agent_run rows: zero terminal runs must read INDETERMINATE.
    assert "criterion c" in out and "0/0" in out and "n/a" in out \
        and "-> INDETERMINATE" in out
    # Dashboard footer lines.
    assert "volume:" in out and "distinct active authors" in out
    assert "llm_spend:" in out and "$0.0020" in out


def test_parity_report_json_roundtrip(runner, report_db):
    result = runner.invoke(
        agent,
        ["parity-report", "--db", str(report_db), "--json",
         "--window-start", WS, "--window-end", WE],
    )
    assert result.exit_code == 0
    conn = connect_ro(str(report_db))
    try:
        expected = compute_report(
            conn, window_start=WS, window_end=WE)
    finally:
        conn.close()
    assert json.loads(result.output) == expected


def test_parity_report_exit_zero_even_when_failing(runner, tmp_path):
    """A failing gate is still a successful report run."""
    db = _build_report_db(
        tmp_path / "fail.db", tool_attempts=10, dup_rejections=2)
    result = runner.invoke(
        agent,
        ["parity-report", "--db", str(db),
         "--window-start", WS, "--window-end", WE],
    )
    assert result.exit_code == 0
    assert "criterion b" in result.output and "-> FAIL" in result.output


def test_parity_report_strict_thresholds(runner, tmp_path):
    """Boundary equality fails under the plan's strict `<` wording."""
    # dup rate exactly 10% of 20 attempts.
    db_b = _build_report_db(
        tmp_path / "b.db", tool_attempts=20, dup_rejections=2)
    out_b = runner.invoke(
        agent, ["parity-report", "--db", str(db_b), "--json",
                "--window-start", WS, "--window-end", WE]).output
    assert json.loads(out_b)["criterion_b"]["pass"] is False

    # fail rate exactly 5% of 20 terminal runs (19 completed + 1 failed).
    db_c = _build_report_db(tmp_path / "c.db")
    conn = sqlite3.connect(str(db_c))  # reopen writable for run rows
    for rid in range(20):
        status = "failed" if rid == 0 else "completed"
        conn.execute(
            "INSERT INTO agent_run (id, status, started_at) VALUES (?, ?, ?)",
            (rid, status, f"2026-08-20 {rid:02d}:00:00"))
    conn.commit()
    conn.close()
    out_c = runner.invoke(
        agent, ["parity-report", "--db", str(db_c), "--json",
                "--window-start", WS, "--window-end", WE]).output
    cc = json.loads(out_c)["criterion_c"]
    assert cc["rate"] == 0.05
    assert cc["pass"] is False


def test_parity_report_indeterminate_empty_window(runner, report_db):
    result = runner.invoke(
        agent,
        ["parity-report", "--db", str(report_db),
         "--window-start", "2026-07-01 00:00:00",
         "--window-end", "2026-07-02 00:00:00"],
    )
    assert result.exit_code == 0
    out = result.output
    assert out.count("INDETERMINATE") == 3
    assert "PASS" not in out.replace("INDETERMINATE", "")
    assert "FAIL" not in out.replace("INDETERMINATE", "")


def _seed_candidates(conn, n_comments=25, n_posts=0):
    for i in range(n_posts):
        conn.execute(
            "INSERT INTO post (id, title, content, subdeaddit_name, user,"
            " created_at, model) VALUES (?, 't', 'c', 'python', ?, ?, ?)",
            (900 + i, f"A{i % 2}", f"2026-08-20 0{i}:00:00", "agent:eve"))
    for i in range(n_comments):
        conn.execute(
            "INSERT INTO comment (id, post_id, parent_id, content, user,"
            " created_at, model) VALUES (?, 1, NULL, 'c', ?, ?, ?)",
            (700 + i, "A0", f"2026-08-20 {i % 24:02d}:{i // 24:02d}:00",
             "agent:eve"))


@pytest.fixture()
def packet_db(tmp_path):
    path = tmp_path / "packet.db"
    conn = _make_db(path)
    _seed_baseline(conn)
    _seed_candidates(conn)
    conn.commit()
    conn.close()
    return path


def test_sample_packet_stdout_deterministic(runner, packet_db):
    args = ["sample-packet", "--db", str(packet_db), "--seed", "42"]
    first = runner.invoke(agent, args)
    second = runner.invoke(agent, args)
    assert first.exit_code == 0 and second.exit_code == 0
    assert first.output == second.output
    assert "# AC-P3 Reviewer Sampling Packet" in first.output
    assert "- Seed: 42" in first.output
    other = runner.invoke(agent, [*args, "--items", "21"])
    assert other.exit_code == 0 and other.output != first.output


def test_sample_packet_output_file_identical_bytes(runner, packet_db):
    to_stdout = runner.invoke(
        agent, ["sample-packet", "--db", str(packet_db), "--seed", "42"])
    with runner.isolated_filesystem(temp_dir=packet_db.parent):
        to_file = runner.invoke(
            agent, ["sample-packet", "--db", str(packet_db), "--seed", "42",
                    "-o", "packet.md"])
        assert to_file.exit_code == 0
        written = open("packet.md", encoding="utf-8").read()
    # click.echo appends one newline after the exact packet bytes.
    assert written + "\n" == to_stdout.output
    assert "wrote 20 sampled items (seed 42)" in to_file.output


def test_sample_packet_insufficient_candidates(runner, packet_db):
    result = runner.invoke(
        agent,
        ["sample-packet", "--db", str(packet_db), "--seed", "42",
         "--items", "999"],
    )
    assert result.exit_code != 0
    # Click prints the ClickException to stderr with exit code 1.
    assert isinstance(result.exception, SystemExit)
    assert "need 999" in result.stderr
