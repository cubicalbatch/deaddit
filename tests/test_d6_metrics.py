"""Phase D6: PlatformDaily rollup math vs hand-computed fixtures (plan §8)."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from deaddit.dynamics.metrics import (
    gini_coefficient,
    rollup_day,
    run_nightly_rollup,
)
from deaddit.models import ActivityEvent, Comment, LLMUsage, PlatformDaily, Post

_DAY = date(2026, 8, 25)


def _dt(day=_DAY, hour=12, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute)


def _alembic_heads() -> set[str]:
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    cfg = AlembicConfig()
    package_root = Path(__file__).resolve().parent.parent
    cfg.set_main_option("script_location", str(package_root / "migrations"))
    return set(ScriptDirectory.from_config(cfg).get_heads())


def _session():
    from deaddit import db as _db

    return _db.session


@pytest.fixture()
def rollup_fixtures(app, db_session):
    """Hand-computed raw truth for one UTC day.

    Events: 2 posts (alice, bob), 3 comments (bob x2, carol), 4 votes
    (alice x3, bob), 1 report → total 10 actions by 3 distinct users.
    LLM usage that day: one priced attempt (100/50 tokens, $0.02) and one
    UNPRICED attempt (10/5 tokens, estimated_cost NULL).
    Provenance: posts split agent:alice=1 / seed=1; comments seed=2, other=1.
    Health: one thread with a depth-1 and a depth-2 comment chain plus one
    standalone comment (depths {1,2,1}); dissent scores 1, -1, -1.
    """
    from deaddit.models import Subdeaddit, User

    db_session.add_all(
        [User(username=n) for n in ("alice", "bob", "carol")]
    )
    db_session.add(Subdeaddit(name="metrics", description="m"))
    db_session.commit()

    p_agent = Post(
        title="agent post",
        content="x",
        score=1,
        user="alice",
        subdeaddit_name="metrics",
        model="agent:alice",
    )
    p_seed = Post(
        title="seed post",
        content="y",
        score=0,
        user="bob",
        subdeaddit_name="metrics",
        model="seed",
    )
    p_other = Post(
        title="other post",
        content="z",
        score=0,
        user="carol",
        subdeaddit_name="metrics",
        model="legacy-model",
    )
    db_session.add_all([p_agent, p_seed, p_other])
    db_session.commit()

    c_top = Comment(
        post_id=p_agent.id, user="bob", content="top", score=1, model="seed"
    )
    c_reply = Comment(
        post_id=p_agent.id, parent_id=None, user="carol", content="reply", score=-1, model="seed"
    )
    c_standalone = Comment(
        post_id=p_seed.id, user="bob", content="solo", score=-1, model="legacy-model"
    )
    db_session.add_all([c_top, c_reply, c_standalone])
    db_session.commit()
    c_reply.parent_id = c_top.id
    db_session.commit()

    events = [
        ("post", "alice", _dt(hour=9)),
        ("post", "bob", _dt(hour=9)),
        ("comment", "bob", _dt(hour=10)),
        ("comment", "bob", _dt(hour=11)),
        ("comment", "carol", _dt(hour=11)),
        ("vote", "alice", _dt(hour=12)),
        ("vote", "alice", _dt(hour=13)),
        ("vote", "alice", _dt(hour=14)),
        ("vote", "bob", _dt(hour=15)),
        ("report", "carol", _dt(hour=16)),
    ]
    for kind, who, when in events:
        db_session.add(ActivityEvent(occurred_at=when, event_type=kind, username=who))
    # One event YESTERDAY that must NOT leak into today's rollup.
    db_session.add(
        ActivityEvent(
            occurred_at=datetime(2026, 8, 24, 23, 59),
            event_type="vote",
            username="alice",
        )
    )

    db_session.add(
        LLMUsage(
            created_at=_dt(hour=10),
            status="ok",
            model="qwen3.8-27b",
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            estimated_cost=0.02,
        )
    )
    db_session.add(
        LLMUsage(
            created_at=_dt(hour=11),
            status="ok",
            model="unpriced-model",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            estimated_cost=None,
        )
    )
    # A priced row YESTERDAY that must not leak either.
    db_session.add(
        LLMUsage(
            created_at=datetime(2026, 8, 24, 10),
            status="ok",
            model="qwen3.8-27b",
            prompt_tokens=999,
            completion_tokens=999,
            estimated_cost=99.0,
        )
    )
    db_session.commit()
    return {"p_agent": p_agent, "p_seed": p_seed}


class TestGini:
    def test_equal_distribution_is_zero(self):
        assert gini_coefficient([4, 4, 4, 4]) == pytest.approx(0.0)

    def test_hand_computed_concentration(self):
        # sorted [0,0,0,10]: cumulative 40, n=4, total=10
        # gini = 2*40/(4*10) - 5/4 = 0.75
        assert gini_coefficient([10, 0, 0, 0]) == pytest.approx(0.75)

    def test_degenerate_inputs(self):
        assert gini_coefficient([]) is None
        assert gini_coefficient([0, 0, 0]) is None


class TestRollupDay:
    def test_every_column_matches_hand_computed_fixture(
        self, app, db_session, rollup_fixtures
    ):
        row = rollup_day(_DAY)

        assert row.posts == 2
        assert row.comments == 3
        assert row.votes == 4
        assert row.reports == 1
        assert row.active_agents == 3
        assert row.actions_per_active == pytest.approx(10 / 3, abs=1e-3)

        # LLM-3 conventions: token sums COALESCE to 0 over BOTH attempts;
        # cost SUM skips the NULL unpriced row → exactly the priced subtotal.
        assert row.llm_tokens_in == 110
        assert row.llm_tokens_out == 55
        assert row.llm_cost_usd == pytest.approx(0.02)

        # cost_per_engagement = 0.02 / (2+3)
        assert row.cost_per_engagement == pytest.approx(0.004)

        # Provenance intact across agent-authored AND seeded rows.
        import json

        provenance = json.loads(row.provenance_json)
        assert provenance["posts"] == {"agent": 1, "seed": 1, "other": 1}
        assert provenance["comments"]["seed"] == 2
        assert provenance["comments"]["other"] == 1

        # Health trio: depths are {1 (top), 2 (reply), 1 (standalone)} → 1.5;
        # two active threads: p_agent comments {1,-1} → share 1/2,
        # p_seed thread {-1} → share 1/1 → avg across threads = 0.75;
        # participation counts CONTENT rows: alice 1p, bob 1p+2c, carol 1p+1c
        # = [1, 3, 2] → hand-computed gini: sorted [1,2,3], total 6,
        # cumulative 14 → gini = 2*14/(3*6) - 4/3 = 0.2222...
        assert row.dissent_share_avg == pytest.approx(0.75)
        # sorted [1,2,3], total 6, cumulative 1*1+2*2+3*3=14,
        assert row.gini_participation_avg == pytest.approx(
            2 * 14 / 18 - 4 / 3, rel=1e-6
        )

    def test_null_cost_semantics_never_fake_zero(self, app, db_session):
        """No priced attempt that day → llm_cost_usd stays NULL even with spend."""
        from deaddit.models import User

        db_session.add(User(username="solo"))
        db_session.add(
            LLMUsage(
                created_at=_dt(),
                status="ok",
                model="unpriced-model",
                prompt_tokens=7,
                completion_tokens=3,
                estimated_cost=None,
            )
        )
        db_session.add(ActivityEvent(occurred_at=_dt(), event_type="post", username="solo"))
        db_session.commit()

        row = rollup_day(_DAY)
        assert row.posts == 1
        assert row.llm_tokens_in == 7
        assert row.llm_cost_usd is None
        assert row.cost_per_engagement is None

    def test_no_engagement_yields_null_cpe(self, app, db_session):
        db_session.add(
            LLMUsage(
                created_at=_dt(),
                status="ok",
                model="m",
                prompt_tokens=1,
                completion_tokens=1,
                estimated_cost=1.0,
            )
        )
        db_session.commit()
        row = rollup_day(_DAY)
        assert row.llm_cost_usd == pytest.approx(1.0)
        assert row.cost_per_engagement is None
        assert row.actions_per_active is None

    def test_rollup_is_idempotent(self, app, db_session, rollup_fixtures):
        first = rollup_day(_DAY)
        second = rollup_day(_DAY)
        assert first.day == second.day
        assert PlatformDaily.query.count() == 1
        assert second.posts == first.posts
        assert second.gini_participation_avg == first.gini_participation_avg

    def test_nightly_entry_rolls_up_yesterday(self, app, db_session, rollup_fixtures):
        now = datetime(2026, 8, 26, 3, 55)
        row = run_nightly_rollup(now=now)
        assert row.day == date(2026, 8, 25)
        assert row.posts == 2

    def test_rollup_under_five_seconds(self, app, db_session, rollup_fixtures):
        """Plan acceptance: nightly rollup completes < 5 s (scaled fixture)."""
        from deaddit.models import User

        db_session.add_all([User(username=f"u{i}") for i in range(50)])
        db_session.commit()
        bulk_events = [
            ActivityEvent(
                occurred_at=_dt(hour=(i % 24)), event_type="vote", username=f"u{i % 50}"
            )
            for i in range(5000)
        ]
        bulk_comments = [
            Comment(
                post_id=rollup_fixtures["p_seed"].id,
                user=f"u{i % 50}",
                content=f"bulk {i}",
                score=i % 3 - 1,
                model="seed",
                created_at=_dt(hour=i % 24),
            )
            for i in range(2000)
        ]
        db_session.add_all(bulk_events)
        db_session.add_all(bulk_comments)
        db_session.commit()

        start = time.monotonic()
        rollup_day(_DAY)
        elapsed = time.monotonic() - start
        assert elapsed < 5.0, f"rollup took {elapsed:.2f}s"


class TestMigrationAndAdminSurface:
    def test_single_head_and_round_trip(self, tmp_path):
        """D6 revision stacks on c7e2a9b4d1f6; upgrade/downgrade/upgrade is
        lossless on a throwaway DB (additive DDL only)."""
        import sqlite3

        from deaddit import create_app

        db_path = tmp_path / "d6.db"
        app = create_app(
            {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "TESTING": True}
        )
        runner = app.test_cli_runner()

        up = runner.invoke(args=["db", "upgrade"])
        assert up.exit_code == 0, up.output

        conn = sqlite3.connect(db_path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"activity_event", "platform_daily", "degeneracy_flag"} <= tables
        stamp = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        heads = _alembic_heads()
        assert heads == {"f3b8e2a6c9d4"}, f"branched or wrong head: {heads}"
        assert stamp == "f3b8e2a6c9d4"

        # Seed rows in the new tables, then round-trip below the revision.
        conn.execute(
            "INSERT INTO activity_event (event_type) VALUES ('post')"
        )
        conn.execute("INSERT INTO platform_daily (day, posts) VALUES ('2026-08-25', 3)")
        conn.commit()
        conn.close()

        down = runner.invoke(args=["db", "downgrade", "c7e2a9b4d1f6"])
        assert down.exit_code == 0, down.output
        conn = sqlite3.connect(db_path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not ({"activity_event", "platform_daily", "degeneracy_flag"} & tables)
        conn.close()

        up2 = runner.invoke(args=["db", "upgrade"])
        assert up2.exit_code == 0, up2.output
        conn = sqlite3.connect(db_path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"activity_event", "platform_daily", "degeneracy_flag"} <= tables
        conn.close()

    def test_analytics_page_shows_seven_day_series(self, app, client):
        """Acceptance: analytics page renders the rollup series + watchlist."""

        from deaddit.models import DegeneracyFlag

        today = datetime.combine(date.today(), datetime.min.time())
        # Two days of real events, then rolled up.
        for offset in (1, 0):
            when = today - timedelta(days=offset) + timedelta(hours=10)
            db = _session()
            db.add_all(
                [
                    ActivityEvent(occurred_at=when, event_type="post", username="a"),
                    ActivityEvent(occurred_at=when, event_type="comment", username="b"),
                    ActivityEvent(occurred_at=when, event_type="vote", username="a"),
                ]
            )
            db.add(
                LLMUsage(
                    created_at=when,
                    status="ok",
                    model="m",
                    prompt_tokens=100,
                    completion_tokens=50,
                    estimated_cost=0.01,
                )
            )
            db.commit()
        rollup_day(date.today() - timedelta(days=1))
        rollup_day(date.today())

        db = _session()
        db.add(
            DegeneracyFlag(
                kind="repetition", username="spammer", metric=0.91,
                created_at=datetime.utcnow(),
            )
        )
        db.commit()

        with client.session_transaction() as sess:
            sess["admin_authenticated"] = True
        resp = client.get("/admin/analytics")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Active agents" in html
        assert "Cost per engagement" in html
        assert "Degeneracy watchlist" in html
        assert "spammer" in html

