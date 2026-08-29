"""Phase 1 simulated-voting persistence migration coverage."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from deaddit import create_app
from deaddit.dynamics.engagement import upsert_hourly_summary
from deaddit.dynamics.votes import cast_vote
from deaddit.extensions import db
from deaddit.models import (
    Post,
    Subdeaddit,
    User,
    VoteCadencePolicy,
)

_PRE_PHASE1_HEAD = "c4b9e2f7a1d3"
_PHASE1_HEAD = "d6a4f8c2b901"

_CONFIG = {
    "post": {
        "mean_active_votes": 8.0,
        "attention_shape": 1.0,
        "half_life_minutes": 90,
        "active_window_hours": 48,
        "catchup_grace_hours": 12,
        "max_active_votes": 80,
        "tail_half_life_days": 14,
        "tail_max_age_days": 365,
        "tail_vote_probability_per_exposure": 0.015,
    },
    "comment": {
        "mean_active_votes": 3.0,
        "attention_shape": 1.0,
        "half_life_minutes": 60,
        "active_window_hours": 24,
        "catchup_grace_hours": 6,
        "max_active_votes": 30,
        "tail_half_life_days": 7,
        "tail_max_age_days": 90,
        "tail_vote_probability_per_exposure": 0.005,
    },
    "voter": {
        "default_hourly_cap": 20,
        "minimum_gap_seconds": 45,
        "subscription_weight": 4.0,
        "max_activity_weight": 3.0,
    },
    "direction": {
        "base_downvote_probability": 0.05,
        "minimum_downvote_probability": 0.01,
        "maximum_downvote_probability": 0.15,
    },
}


def _table_names(path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def _index_names(path, table: str) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[1] for row in connection.execute(f"PRAGMA index_list({table})")}


def test_phase1_migration_preserves_populated_data_on_downgrade(tmp_path):
    path = tmp_path / "simulated-voting.db"
    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{path}", "TESTING": True})
    runner = app.test_cli_runner()
    assert runner.invoke(args=["db", "upgrade"]).exit_code == 0

    # Populate at the current schema: the ORM maps every head column, so
    # rows must exist before any downgrade walks below the newest
    # migration (later revisions may have added post columns the ORM
    # now inserts unconditionally).
    with app.app_context():
        db.session.add_all(
            [
                Subdeaddit(name="testsub", description="test"),
                User(username="voter"),
                User(username="author"),
            ]
        )
        db.session.commit()
        post = Post(
            title="populated",
            content="existing content",
            user="author",
            subdeaddit_name="testsub",
            post_type="text",
        )
        db.session.add(post)
        db.session.commit()
        post_id = post.id
        assert (
            cast_vote(
                "voter", "post", post_id, 1, source="simulated", allow_recast=False
            )["change_kind"]
            == "insert"
        )

    # The downgrade under test must leave populated rows intact.
    assert runner.invoke(args=["db", "downgrade", _PRE_PHASE1_HEAD]).exit_code == 0
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT source FROM vote WHERE voter = 'voter' AND post_id = ?",
                (post_id,),
            ).fetchone()[0]
            == "simulated"
        )
        assert (
            connection.execute(
                "SELECT title FROM post WHERE id = ?", (post_id,)
            ).fetchone()[0]
            == "populated"
        )

    assert runner.invoke(args=["db", "upgrade"]).exit_code == 0
    with app.app_context():
        policy = VoteCadencePolicy(
            preset="natural",
            algorithm_version=1,
            config=_CONFIG,
            effective_at=datetime(2026, 1, 1, 10),
            created_at=datetime(2026, 1, 1, 10),
        )
        db.session.add(policy)
        upsert_hourly_summary(
            datetime(2026, 1, 1, 10, 59),
            "shadow",
            ticks=4,
            errors=1,
            active_proposals=2,
            archive_proposals=3,
            revival_proposals=4,
            inserted_votes=5,
            switched_votes=6,
            upvotes=7,
            downvotes=8,
            cap_skips=9,
            min_gap_skips=10,
            no_voter_skips=11,
            guardrail_skips=12,
        )
        upsert_hourly_summary(datetime(2026, 1, 1, 10), "live", ticks=1)
        db.session.commit()

    with sqlite3.connect(path) as connection:
        policy_json = connection.execute(
            "SELECT config FROM vote_cadence_policy"
        ).fetchone()[0]
        assert json.loads(policy_json) == _CONFIG
        assert (
            connection.execute(
                "SELECT source FROM vote WHERE voter = 'voter' AND post_id = ?",
                (post_id,),
            ).fetchone()[0]
            == "simulated"
        )
        assert _index_names(path, "vote_cadence_policy") >= {
            "ix_vote_cadence_policy_effective_at"
        }
        summary = connection.execute(
            "SELECT * FROM vote_simulation_hourly WHERE mode = 'shadow'"
        ).fetchone()
        assert summary[0].startswith("2026-01-01 10:00:00")
        assert summary[1] == "shadow"
        assert summary[2:15] == (4, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM vote_simulation_hourly "
                "WHERE hour LIKE '2026-01-01 10:00:00%'"
            ).fetchone()[0]
            == 2
        )

    assert runner.invoke(args=["db", "downgrade", _PRE_PHASE1_HEAD]).exit_code == 0
    tables = _table_names(path)
    assert "vote_cadence_policy" not in tables
    assert "vote_simulation_hourly" not in tables
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT source FROM vote WHERE voter = 'voter' AND post_id = ?",
                (post_id,),
            ).fetchone()[0]
            == "simulated"
        )


def test_phase1_revision_is_current_single_head(tmp_path):
    path = tmp_path / "head.db"
    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{path}", "TESTING": True})
    result = app.test_cli_runner().invoke(args=["db", "upgrade", _PHASE1_HEAD])
    assert result.exit_code == 0, result.output
    with sqlite3.connect(path) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
    assert revision == (_PHASE1_HEAD,)
