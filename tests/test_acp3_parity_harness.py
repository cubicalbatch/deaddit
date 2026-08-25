"""AC-P3 parity harness tests (offline, deterministic).

Builds throwaway SQLite files with the real column names used by
``deaddit.agents.parity`` and asserts hand-computed statistics exactly.
No network, no ORM, no LLM code.
"""

from __future__ import annotations

import sqlite3

import pytest

from deaddit.agents.parity import (
    DUP_REJECTION_MARKER,
    WINDOW_HOURS,
    build_sample_packet,
    compute_report,
    connect_ro,
)

WS = "2026-08-20 00:00:00"
WE = "2026-08-21 00:00:00"

SCHEMA = """
CREATE TABLE post (
    id INTEGER PRIMARY KEY,
    title TEXT,
    content TEXT,
    subdeaddit_name TEXT NOT NULL,
    user TEXT NOT NULL,
    created_at TEXT,
    model TEXT
);
CREATE TABLE comment (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL,
    parent_id INTEGER,
    content TEXT,
    user TEXT NOT NULL,
    created_at TEXT,
    model TEXT
);
CREATE TABLE agent_run (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL
);
CREATE TABLE tool_call (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    result TEXT,
    created_at TEXT
);
CREATE TABLE llm_usage (
    id INTEGER PRIMARY KEY,
    created_at TEXT,
    status TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    estimated_cost REAL
);
"""


def _make_db(path) -> sqlite3.Connection:
    """Create the throwaway DB; returns a WRITABLE connection for setup."""
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    return conn


def _open_ro(path) -> sqlite3.Connection:
    """Open the seeded DB through the harness's read-only connector."""
    return connect_ro(str(path))


def _add_post(conn, id, *, user, created_at, model=None, title="t", content="c",
              sub="python"):
    conn.execute(
        "INSERT INTO post (id, title, content, subdeaddit_name, user,"
        " created_at, model) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (id, title, content, sub, user, created_at, model),
    )


def _add_comment(conn, id, *, user, created_at, model=None, content="c",
                 post_id=1, parent_id=None):
    conn.execute(
        "INSERT INTO comment (id, post_id, parent_id, content, user,"
        " created_at, model) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (id, post_id, parent_id, content, user, created_at, model),
    )


@pytest.fixture()
def stats_conn(tmp_path):
    """Seeded fixture with HAND-COMPUTED expected statistics.

    Baseline (strictly before WS, non-agent only): 14 legacy posts +
    21 legacy comments over 7 days -> posts/day 2.0, comments/day 3.0,
    total/day 5.0. One agent-stamped post before WS proves baseline filters
    on provenance, not just time.

    Window [WS, WE): 2 agent posts + 4 agent comments -> agent total/day 6.0,
    ratio 6/5 = 1.2 (inside +/-30%, criterion_a PASS). Plus 1 legacy post and
    2 legacy comments -> volume posts/day 3.0, comments/day 6.0, 4 distinct
    active authors (A1, A2, H1, H2).
    """
    conn = _make_db(tmp_path / "stats.db")
    # --- baseline rows (all created_at < WS)
    for i in range(14):
        _add_post(conn, 100 + i, user=f"h{i % 3}",
                  created_at=f"2026-08-{13 + i // 7:02d} {i % 24:02d}:00:00")
    for i in range(21):
        _add_comment(conn, 200 + i, user=f"h{i % 3}",
                     created_at=f"2026-08-{13 + i // 11:02d} {i % 24:02d}:30:00")
    # agent-marked row BEFORE the window: excluded from baseline
    _add_post(conn, 300, user="A1", created_at="2026-08-18 12:00:00",
              model="agent:eve")

    # --- in-window rows
    _add_post(conn, 1, user="A1", created_at="2026-08-20 01:00:00",
              model="agent:eve")
    _add_post(conn, 2, user="A2", created_at="2026-08-20 02:00:00",
              model="agent:malory")
    for i in range(4):
        _add_comment(conn, 10 + i, user="A1" if i % 2 else "A2",
                     created_at=f"2026-08-20 03:{i:02d}:00", model="agent:eve")
    _add_post(conn, 3, user="H1", created_at="2026-08-20 05:00:00")
    _add_comment(conn, 20, user="H1", created_at="2026-08-20 06:00:00")
    _add_comment(conn, 21, user="H2", created_at="2026-08-20 06:30:00")

    # --- criterion (b): write attempts in window
    # t1 plain success; t2 duplicate rejection; t3 duplicate rejection;
    # t4 non-dup failure (must be EXCLUDED from rejections);
    # t5 wrong tool name carrying the marker (EXCLUDED entirely);
    # t6 duplicate marker OUTSIDE the window (EXCLUDED).
    conn.execute(
        "INSERT INTO tool_call (id, name, result, created_at)"
        " VALUES (1, 'create_post', '{\"status\": \"created\"}', ?)", (WS,))
    conn.execute(
        "INSERT INTO tool_call (id, name, result, created_at)"
        " VALUES (2, 'create_comment', ?, ?)",
        (f"duplicate rejected: {DUP_REJECTION_MARKER}; write something new",
         "2026-08-20 07:00:00"))
    conn.execute(
        "INSERT INTO tool_call (id, name, result, created_at)"
        " VALUES (3, 'create_post', ?, '2026-08-20 08:00:00')",
        (DUP_REJECTION_MARKER,))
    conn.execute(
        "INSERT INTO tool_call (id, name, result, created_at)"
        " VALUES (4, 'create_comment',"
        " 'tool error: subdeaddit not found', '2026-08-20 09:00:00')")
    conn.execute(
        "INSERT INTO tool_call (id, name, result, created_at)"
        " VALUES (5, 'vote', ?, '2026-08-20 10:00:00')",
        (DUP_REJECTION_MARKER,))
    conn.execute(
        "INSERT INTO tool_call (id, name, result, created_at)"
        " VALUES (6, 'create_post', ?, '2026-08-19 23:59:59')",
        (DUP_REJECTION_MARKER,))
    # Expected: attempts = t1..t4 = 4, rejections = t2,t3 = 2, rate 0.5.

    # --- criterion (c): runs started in window
    # r1-r3 completed, r4 failed, r5 'running' (NOT terminal -> excluded).
    # r6 failed but started BEFORE the window (excluded).
    for rid, status, ts in [
        (1, "completed", "2026-08-20 01:00:00"),
        (2, "completed", "2026-08-20 02:00:00"),
        (3, "completed", "2026-08-20 03:00:00"),
        (4, "failed", "2026-08-20 04:00:00"),
        (5, "running", "2026-08-20 05:00:00"),
        (6, "failed", "2026-08-19 09:00:00"),
    ]:
        conn.execute(
            "INSERT INTO agent_run (id, status, started_at) VALUES (?, ?, ?)",
            (rid, status, ts))
    # Expected: terminal 4, failed 1, rate 0.25 -> FAIL vs 0.05 threshold.

    # --- llm_usage in window
    # u1 ok with cost; u2 ok without cost; u3 failed without tokens;
    # u4 outside window (ignored). SUM ignores NULLs -> cost stays 0.002.
    for uid, ts, status, p, c, t, cost in [
        (1, "2026-08-20 01:00:00", "ok", 100, 50, 150, 0.002),
        (2, "2026-08-20 02:00:00", "ok", 200, 100, 300, None),
        (3, "2026-08-20 03:00:00", "failed", None, None, None, None),
        (4, "2026-08-19 01:00:00", "ok", 999, 999, 1998, 9.99),
    ]:
        conn.execute(
            "INSERT INTO llm_usage (id, created_at, status, prompt_tokens,"
            " completion_tokens, total_tokens, estimated_cost)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)", (uid, ts, status, p, c, t, cost))

    conn.commit()
    ro = _open_ro(tmp_path / "stats.db")
    yield ro
    ro.close()
    conn.close()


def test_criterion_a_and_rates(stats_conn):
    report = compute_report(stats_conn, window_start=WS, window_end=WE)

    assert report["baseline"] == {
        "days": 7,
        "posts_per_day": 2.0,
        "comments_per_day": 3.0,
        "total_per_day": 5.0,
    }
    assert report["agent"] == {
        "posts_per_day": 2.0,
        "comments_per_day": 4.0,
        "total_per_day": 6.0,
        "ratio": 1.2,
    }
    assert report["criterion_a"] == {
        "ratio": 1.2,
        "low_bound": 0.7,
        "high_bound": 1.3,
        "pass": True,
    }
    assert report["window"] == {"start": WS, "end": WE, "hours": 24.0}


def test_criterion_a_outside_high_bound(tmp_path):
    """Agent flood: ratio 20/5 = 4.0 far above 1.3 -> FAIL."""
    conn = _make_db(tmp_path / "flood.db")
    for i in range(14):
        _add_post(conn, 100 + i, user=f"h{i}",
                  created_at=f"2026-08-{13 + i // 7:02d} 00:00:00")
    for i in range(21):
        _add_comment(conn, 200 + i, user=f"h{i}",
                     created_at=f"2026-08-{13 + i // 11:02d} 00:30:00")
    for i in range(8):
        _add_post(conn, 1 + i, user="A1", created_at=f"2026-08-20 0{i}:00:00",
                  model="agent:eve")
    for i in range(12):
        _add_comment(conn, 50 + i, user="A1",
                     created_at=f"2026-08-20 {10 + i // 6:02d}:{i % 6:02d}:00",
                     model="agent:eve")
    conn.commit()
    ro = _open_ro(tmp_path / "flood.db")
    report = compute_report(ro, window_start=WS, window_end=WE)
    ro.close()
    assert report["agent"]["total_per_day"] == 20.0
    assert report["criterion_a"]["ratio"] == 4.0
    assert report["criterion_a"]["pass"] is False


def test_criterion_b_duplicate_rejections(stats_conn):
    report = compute_report(stats_conn, window_start=WS, window_end=WE)
    cb = report["criterion_b"]
    # 4 write attempts (t1-t4); only t2/t3 carry the marker; t4's generic
    # failure, t5's wrong tool name, and t6's out-of-window marker excluded.
    assert cb == {
        "write_attempts": 4,
        "duplicate_rejections": 2,
        "rate": 0.5,
        "threshold": 0.10,
        "pass": False,
    }


def test_criterion_c_and_llm_spend(stats_conn):
    report = compute_report(stats_conn, window_start=WS, window_end=WE)
    cc = report["criterion_c"]
    # 'running' r5 and pre-window r6 stay out; 1 of 4 terminal runs failed.
    assert cc == {
        "terminal_runs": 4,
        "failed_runs": 1,
        "rate": 0.25,
        "threshold": 0.05,
        "pass": False,
    }
    # u4 outside window ignored; NULLs never counted as zeros.
    assert report["llm_spend"] == {
        "attempts": 3,
        "ok_attempts": 2,
        "failed_attempts": 1,
        "prompt_tokens": 300,
        "completion_tokens": 150,
        "total_tokens": 450,
        "estimated_cost": 0.002,
    }


def test_volume_dashboard(stats_conn):
    report = compute_report(stats_conn, window_start=WS, window_end=WE)
    assert report["volume"]["posts_per_day"] == 3.0
    assert report["volume"]["comments_per_day"] == 6.0
    assert report["volume"]["distinct_active_authors"] == 4
    assert report["volume"]["source_split"] == {
        "agent_posts": 2,
        "legacy_posts": 1,
        "agent_comments": 4,
        "legacy_comments": 2,
    }


def test_indeterminate_when_empty_window(stats_conn):
    """Window before any data: baselines/attempts/runs all zero -> pass None."""
    report = compute_report(stats_conn, window_start="2026-07-01 00:00:00",
                            window_end="2026-07-02 00:00:00")
    assert report["criterion_a"]["ratio"] is None
    assert report["criterion_a"]["pass"] is None
    assert report["baseline"]["total_per_day"] == 0.0
    assert report["criterion_b"]["write_attempts"] == 0
    assert report["criterion_b"]["rate"] is None
    assert report["criterion_b"]["pass"] is None
    assert report["criterion_c"]["terminal_runs"] == 0
    assert report["criterion_c"]["rate"] is None
    assert report["criterion_c"]["pass"] is None


def test_llm_spend_all_costs_null(tmp_path):
    """All-NULL estimated_cost must stay None, never become 0."""
    conn = _make_db(tmp_path / "nocost.db")
    conn.execute(
        "INSERT INTO llm_usage (id, created_at, status, prompt_tokens,"
        " completion_tokens, total_tokens, estimated_cost)"
        " VALUES (1, ?, 'ok', 10, 5, 15, NULL)", (WS,))
    conn.commit()
    ro = _open_ro(tmp_path / "nocost.db")
    report = compute_report(ro, window_start=WS, window_end=WE)
    ro.close()
    assert report["llm_spend"]["total_tokens"] == 15
    assert report["llm_spend"]["estimated_cost"] is None


def test_window_derivation_from_max_created_at(tmp_path):
    """No bounds passed -> end = MAX(post/comment created_at), start -24h."""
    conn = _make_db(tmp_path / "derive.db")
    _add_post(conn, 1, user="h", created_at="2026-08-10 00:00:00")
    _add_comment(conn, 1, user="a", created_at="2026-08-20 15:30:00",
                 model="agent:x")
    conn.commit()
    ro = _open_ro(tmp_path / "derive.db")
    report = compute_report(ro)
    ro.close()
    assert report["window"] == {
        "start": "2026-08-19 15:30:00",
        "end": "2026-08-20 15:30:00",
        "hours": float(WINDOW_HOURS),
    }


# ---------------------------------------------------------------------------
# Reviewer sampling packet (criterion d)
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_conn(tmp_path):
    """25 agent candidates (10 posts + 15 comments) inside the window."""
    conn = _make_db(tmp_path / "sample.db")
    for i in range(10):
        _add_post(conn, 1 + i, user=f"a{i % 3}", sub="rust",
                  title=f"Post {i}" if i % 2 else "",
                  content=("line one\nline two " + "x" * 200),
                  created_at=f"2026-08-20 00:{i:02d}:00", model=f"agent:p{i % 2}")
    for i in range(15):
        _add_comment(conn, 100 + i, user=f"a{i % 3}", post_id=1 + i % 10,
                     content=f"Comment {i}\nsecond line",
                     created_at=f"2026-08-20 01:{i:02d}:00", model="agent:c")
    conn.commit()
    ro = _open_ro(tmp_path / "sample.db")
    yield ro
    ro.close()
    conn.close()


def test_packet_deterministic_same_seed(sample_conn):
    first = build_sample_packet(sample_conn, seed=42, min_items=20,
                                window_start=WS, window_end=WE)
    second = build_sample_packet(sample_conn, seed=42, min_items=20,
                                 window_start=WS, window_end=WE)
    assert first == second


def test_packet_different_seed_different_selection(sample_conn):
    a = build_sample_packet(sample_conn, seed=1, min_items=20,
                            window_start=WS, window_end=WE)
    b = build_sample_packet(sample_conn, seed=2, min_items=20,
                            window_start=WS, window_end=WE)
    assert a != b


def test_packet_too_few_candidates_raises(sample_conn):
    with pytest.raises(ValueError):
        build_sample_packet(sample_conn, seed=1, min_items=20,
                            window_start="2026-08-22 00:00:00",
                            window_end="2026-08-23 00:00:00")


def test_packet_rubric_and_header(sample_conn):
    packet = build_sample_packet(sample_conn, seed=7, min_items=20,
                                 window_start=WS, window_end=WE)
    assert "RUBRIC" in packet
    for dim in (
        "topical fit to subdeaddit",
        "persona consistency",
        "originality",
        "conversational context (comments address parent/post)",
        "language quality",
    ):
        assert dim in packet
    for flag in ("spam-wave phrasing", "near-duplicate of another sample",
                 "off-topic reply"):
        assert flag in packet
    assert "Seed: 7" in packet
    assert "Candidate pool: 25" in packet
    assert f"[{WS}, {WE})" in packet


def test_packet_item_lines(sample_conn):
    packet = build_sample_packet(sample_conn, seed=7, min_items=20,
                                 window_start=WS, window_end=WE)
    lines = packet.splitlines()
    # Scoring-sheet lines look like "N. id=... kind=post|comment ...".
    numbered = [ln for ln in lines if ln.split(". ", 1)[-1].startswith("id=")]
    assert len(numbered) == 20
    # Snippets collapse newlines and truncate to 160 chars.
    assert "line one line two" in packet
    for ln in numbered:
        assert "kind=post" in ln or "kind=comment" in ln
        assert "author=" in ln
    # Comments reference their parent post; posts link to /d/<sub>.
    assert "/d/rust" in packet
    assert "post_id=" in packet


def test_packet_snippet_truncation_and_newlines(sample_conn):
    packet = build_sample_packet(sample_conn, seed=3, min_items=20,
                                 window_start=WS, window_end=WE)
    body_lines = packet.splitlines()
    snippets = [ln.strip() for ln in body_lines if ln.startswith("   snippet:")]
    assert len(snippets) == 20
    for s in snippets:
        payload = s[len("snippet:"):].strip()
        assert "\n" not in payload
        assert len(payload) <= 160
