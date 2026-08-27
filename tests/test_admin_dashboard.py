"""Behavioral tests for the agent-first admin dashboard (/admin/dashboard).

Asserts concrete rendered strings for the three dashboard sections:
Agents (Runs (24h) bucket counts, 24h window exclusions), Platform Pulse
(provenance badges agent:/seed:), and LLM Spend (SUM over
estimated_cost with NULL-cost rows ignored, em-dash fallback on empty DB).
"""

from datetime import datetime, timedelta

import pytest

from deaddit.models import (
    Agent,
    AgentRun,
    Comment,
    LLMUsage,
    Post,
    Subdeaddit,
    User,
)


@pytest.fixture()
def admin_client(client):
    """Client that passes the admin_required gate (ACP2 convention)."""
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


def _stat_value(n) -> str:
    """The exact stat_tile rendering of one numeric value."""
    return f'<div class="stat-value">{n}</div>'


def test_dashboard_renders_agent_runs_provenance_and_spend(
    app, admin_client, db_session
):
    now = datetime.utcnow()

    db_session.add_all(
        [
            User(username="agenthost"),
            Subdeaddit(name="testsub", description="dashboard seed"),
        ]
    )
    db_session.flush()

    agent = Agent(
        user_username="agenthost",
        autonomy_tier="regular",
        is_enabled=True,
        status="idle",
        config={},
        state={},
        consecutive_failures=0,
    )
    db_session.add(agent)
    db_session.flush()

    # Two completed + one failed + one interrupted inside the 24h window;
    # one completed run two days old must NOT count toward Runs (24h).
    for status, started in (
        ("completed", now),
        ("completed", now),
        ("failed", now),
        ("interrupted", now),
        ("completed", now - timedelta(days=2)),
    ):
        db_session.add(
            AgentRun(
                agent_id=agent.id,
                trigger="schedule",
                status=status,
                started_at=started,
                finished_at=started,
            )
        )

    # Provenance buckets: agent-posted and seeded content today.
    db_session.add_all(
        [
            Post(
                title="agent post",
                content="body",
                user="agenthost",
                subdeaddit_name="testsub",
                model="agent:poster-1",
                created_at=now,
            ),
            Post(
                title="seeded post",
                content="body",
                user="agenthost",
                subdeaddit_name="testsub",
                model="seed",
                created_at=now,
            ),
        ]
    )
    db_session.flush()
    db_session.add_all(
        [
            Comment(
                post_id=db_session.query(Post.id)
                .filter_by(title="agent post")
                .scalar(),
                content="agent comment",
                user="agenthost",
                model="agent:poster-1",
                created_at=now,
            ),
            Comment(
                post_id=db_session.query(Post.id)
                .filter_by(title="seeded post")
                .scalar(),
                content="seed comment",
                user="agenthost",
                model="seed",
                created_at=now,
            ),
        ]
    )

    # Spend: one unpriced row (cost NULL) plus two priced rows; the SUM must
    # ignore the NULL rather than collapse to NULL.
    db_session.add_all(
        [
            LLMUsage(
                created_at=now,
                total_tokens=100,
                estimated_cost=None,
                status="ok",
            ),
            LLMUsage(created_at=now, total_tokens=200, estimated_cost=0.5, status="ok"),
            LLMUsage(
                created_at=now,
                total_tokens=200,
                estimated_cost=0.25,
                status="ok",
            ),
        ]
    )
    db_session.commit()

    resp = admin_client.get("/admin/dashboard")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # Agents: 2 completed + 1 failed + 1 interrupted in-window (old excluded).
    assert "Runs (24h)" in html
    assert _stat_value(4) in html
    assert _stat_value(1) in html  # Failures (24h)

    # Provenance badges for posts and comments.
    assert "agent: 1" in html
    assert "seed: 1" in html

    # Spend: summed tokens (100+200+200) and summed cost (0.5+0.25); the
    # NULL-cost row must not collapse the sum.
    assert _stat_value(500) in html
    assert "$0.7500" in html


def test_dashboard_empty_db_renders_em_dash_cost_fallback(app, admin_client):
    resp = admin_client.get("/admin/dashboard")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "Runs (24h)" in html
    assert _stat_value(0) in html
    assert '<div class="stat-value">—</div>' in html  # cost fallback
    assert "No enabled agent is scheduled to wake." in html


def test_dashboard_requires_admin_when_token_set(client, app, monkeypatch):
    """With API_TOKEN configured the dashboard redirects anonymous users."""
    monkeypatch.setenv("API_TOKEN", "unit-test-token")
    resp = client.get("/admin/dashboard")
    assert resp.status_code in (301, 302)
    assert "/admin/login" in resp.headers.get("Location", "")
