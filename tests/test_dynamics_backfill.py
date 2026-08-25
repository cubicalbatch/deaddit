"""Tests for deaddit.dynamics.seeding.backfill_history (Phase D1, slice S3)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from deaddit import create_app
from deaddit.dynamics import seeding
from deaddit.extensions import db
from deaddit.models import Comment, Post, Subdeaddit, User, Vote

BASE_TIME = datetime(2026, 8, 20, 12, 0, 0)
NOW = BASE_TIME + timedelta(days=10)


def _build_fixture():
    """4 users, known-score posts/comments (incl. one pre-voted item).

    Capacity = 4 users - 1 author-exclusion = 3 eligible voters per item.
    """
    db.session.add(Subdeaddit(name="testsub", description="d", post_types="[]"))
    for i in range(4):
        db.session.add(User(username=f"u{i}"))
    posts = {
        "infeasible": Post(
            title="p1",
            upvote_count=5,  # |5| > capacity 3 => infeasible
            content="c",
            subdeaddit_name="testsub",
            user="u0",
            created_at=BASE_TIME,
        ),
        "maxed": Post(
            title="p2",
            upvote_count=3,  # exactly at capacity
            content="c",
            subdeaddit_name="testsub",
            user="u0",
            created_at=BASE_TIME,
        ),
        "negative": Post(
            title="p3",
            upvote_count=-2,
            content="c",
            subdeaddit_name="testsub",
            user="u1",
            created_at=BASE_TIME,
        ),
    }
    for post in posts.values():
        db.session.add(post)
    db.session.flush()
    comments = {
        "plain": Comment(
            post_id=posts["maxed"].id,
            content="c1",
            upvote_count=1,
            user="u2",
            created_at=BASE_TIME,
        ),
        "zeroscore": Comment(
            post_id=posts["maxed"].id,
            content="c0",
            upvote_count=0,  # must still receive >= 1 vote row (n >= 2)
            user="u3",
            created_at=BASE_TIME,
        ),
        "prevoted": Comment(
            post_id=posts["maxed"].id,
            content="c2",
            upvote_count=2,
            user="u2",
            created_at=BASE_TIME,
        ),
    }
    for comment in comments.values():
        db.session.add(comment)
    db.session.flush()
    # One legacy vote row already exists on the 'prevoted' comment.
    db.session.add(
        Vote(
            voter="u1",
            comment_id=comments["prevoted"].id,
            value=1,
            source="human",
            created_at=BASE_TIME,
        )
    )
    db.session.commit()
    return {"posts": posts, "comments": comments}


@pytest.fixture()
def scenario(app, db_session, monkeypatch):
    """Fixture data with 'now' pinned so runs are fully deterministic."""
    monkeypatch.setattr(seeding, "_now", lambda: NOW)
    return _build_fixture()


def _rows():
    """Snapshot of every vote row as comparable tuples."""
    return [
        (v.voter, v.post_id, v.comment_id, v.value, v.source, v.created_at)
        for v in Vote.query.order_by(Vote.id).all()
    ]


def _target_filter(item):
    return (
        Vote.post_id == item.id
        if isinstance(item, Post)
        else Vote.comment_id == item.id
    )


def _vote_sum(item):
    return (
        Vote.query.filter(_target_filter(item))
        .with_entities(db.func.coalesce(db.func.sum(Vote.value), 0))
        .scalar()
    )


def _item_votes(item):
    return Vote.query.filter(_target_filter(item)).all()


ORIGINAL_SCORES = {"maxed": 3, "negative": -2, "plain": 1, "zeroscore": 0}


def _item_for(scenario, key):
    return scenario["posts"].get(key) or scenario["comments"][key]


def test_backfill_exact_sum_and_report(scenario):
    report = seeding.backfill_history()

    assert report["posts_backfilled"] == 2  # maxed + negative
    assert report["comments_backfilled"] == 2  # plain + zeroscore
    assert report["skipped_already_voted"] == 1  # prevoted
    assert report["votes_created"] > 0
    assert report["unbackfilled_infeasible"] == [
        {"kind": "post", "id": scenario["posts"]["infeasible"].id, "score": 5}
    ]

    for key, original in ORIGINAL_SCORES.items():
        item = _item_for(scenario, key)
        assert _vote_sum(item) == original  # exact-sum invariant
        assert item.score == original
        assert item.upvote_count == original
        n = item.vote_count
        assert abs(original) <= n <= 3  # >= |S|, never above capacity
        if original == 0:
            assert n >= 2  # every feasible item gets >= 1 vote row
        assert (n - original) % 2 == 0  # parity keeps u = (n + S)/2 integral

        votes = _item_votes(item)
        voters = [v.voter for v in votes]
        assert len(voters) == len(set(voters)) == n  # distinct sampled voters
        assert all(name != item.user for name in voters)  # author excluded
        assert {v.source for v in votes} == {"backfill"}
        for vote in votes:
            assert BASE_TIME <= vote.created_at <= NOW

    # Infeasible item untouched: no rows, no attribute drift.
    infeasible = scenario["posts"]["infeasible"]
    assert _item_votes(infeasible) == []
    assert infeasible.score == 0
    assert infeasible.vote_count == 0
    assert infeasible.upvote_count == 5


def test_backfill_idempotent(scenario):
    first = seeding.backfill_history()
    rows_after_first = _rows()

    second = seeding.backfill_history()

    assert second["posts_backfilled"] == 0
    assert second["comments_backfilled"] == 0
    assert second["votes_created"] == 0
    assert second["skipped_already_voted"] == 5  # every previously-voted item
    assert second["unbackfilled_infeasible"] == first["unbackfilled_infeasible"]
    assert len(_rows()) == len(rows_after_first) == first["votes_created"] + 1


def test_backfill_deterministic(scenario):
    report1 = seeding.backfill_history()
    rows_first = _rows()

    db.session.remove()
    db.drop_all()
    db.create_all()
    _build_fixture()
    report2 = seeding.backfill_history()
    rows_second = _rows()

    assert report1["votes_created"] == report2["votes_created"]
    assert rows_second == rows_first


def test_backfill_dry_run_writes_nothing(scenario):
    items = list(scenario["posts"].values()) + list(scenario["comments"].values())
    upvote_counts_before = {
        (type(item).__name__, item.id): item.upvote_count for item in items
    }
    rows_before = _rows()

    report = seeding.backfill_history(dry_run=True)

    assert report["posts_backfilled"] == 2
    assert report["comments_backfilled"] == 2
    assert report["votes_created"] > 0
    assert report["unbackfilled_infeasible"] != []
    assert _rows() == rows_before
    for item in items:
        assert item.score == 0
        assert item.vote_count == 0
        assert item.upvote_count == upvote_counts_before[(type(item).__name__, item.id)]


def test_backfill_batching_matches_unbatched(scenario):
    seeding.backfill_history()
    reference_rows = _rows()

    db.session.remove()
    db.drop_all()
    db.create_all()
    _build_fixture()
    batched_report = seeding.backfill_history(batch_size=1)

    assert batched_report["votes_created"] > 0
    assert _rows() == reference_rows


def test_resolves_to_production_uri_shapes():
    """Pure URI resolution: relative, absolute, and non-prod shapes."""
    instance = "/repo/instance"
    assert seeding._resolves_to_production("sqlite:///deaddit.db", instance)
    assert seeding._resolves_to_production(
        "sqlite:////repo/instance/deaddit.db", instance
    )
    assert not seeding._resolves_to_production("sqlite://", instance)
    assert not seeding._resolves_to_production("sqlite:///:memory:", instance)
    assert not seeding._resolves_to_production("sqlite:////tmp/other.db", instance)
    assert not seeding._resolves_to_production(None, instance)


def test_production_guard_refuses_and_opt_in(scenario, tmp_path, monkeypatch):
    """Prod-shaped target refused without opt-in; opt-in runs against it.

    The 'production' path is redirected into tmp_path via monkeypatch so the
    real instance DB is unreachable from tests by construction.
    """
    fake_prod = tmp_path / "deaddit.db"
    monkeypatch.setattr(seeding, "_production_db_path", lambda _ip: str(fake_prod))

    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{fake_prod}"})
    with app.app_context():
        db.create_all()
        db.session.add(Subdeaddit(name="s", description="d", post_types="[]"))
        for i in range(4):
            db.session.add(User(username=f"u{i}"))
        db.session.add(
            Post(
                title="t",
                upvote_count=2,
                content="c",
                subdeaddit_name="s",
                user="u0",
                created_at=BASE_TIME,
            )
        )
        db.session.commit()

        with pytest.raises(RuntimeError, match="allow_production=True"):
            seeding.backfill_history()

        report = seeding.backfill_history(allow_production=True)
        assert report["posts_backfilled"] == 1
        assert report["votes_created"] > 0


def test_cli_refuses_production_without_flag(monkeypatch):
    """CLI layer: unflagged run against a prod-shaped config aborts non-zero."""
    from click.testing import CliRunner

    from deaddit import cli as cli_module

    prod_app = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite:///deaddit.db"})
    monkeypatch.setattr(cli_module, "create_app", lambda config=None: prod_app)

    result = CliRunner().invoke(cli_module.cli, ["dynamics", "backfill"])

    assert result.exit_code != 0
    assert "i-know-this-is-prod" in result.output


def test_cli_flag_wiring(monkeypatch):
    """Flagged run passes allow_production=True into backfill_history."""
    from unittest import mock

    from click.testing import CliRunner

    from deaddit import cli as cli_module

    prod_app = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite:///deaddit.db"})
    monkeypatch.setattr(cli_module, "create_app", lambda config=None: prod_app)
    stub = mock.Mock(return_value={"posts_backfilled": 0})
    monkeypatch.setattr(cli_module, "backfill_history", stub)

    result = CliRunner().invoke(
        cli_module.cli, ["dynamics", "backfill", "--i-know-this-is-prod"]
    )

    assert result.exit_code == 0
    stub.assert_called_once()
    assert stub.call_args.kwargs["allow_production"] is True
