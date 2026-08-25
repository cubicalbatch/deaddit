"""Phase D5: deterministic history seeding — scoped tests."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta

import pytest
from click.testing import CliRunner
from sqlalchemy import case, func

from deaddit import cli as cli_module
from deaddit import create_app
from deaddit import db as _db
from deaddit.dynamics import ranking, seeding
from deaddit.models import Comment, Post, Setting, User, Vote

NOW = datetime(2026, 8, 25, 12, 0, 0)
D5_REV = "b8e2f4a6c9d1"
D5_DOWN = "f7a3c9d1e5b2"
WINDOW_START = NOW - timedelta(days=14)


@pytest.fixture()
def pinned_now(app, monkeypatch):
    """Pin the seeder's wall-clock seam for full determinism."""
    monkeypatch.setattr(seeding, "_now", lambda: NOW)
    return NOW


# 1. Window containment, causality, timeline shape -------------------------


def test_fresh_seed_window_causality_and_shape(app, pinned_now, db_session):
    report = seeding.seed_history(days=14, seed=42, now=NOW)

    assert report["posts_created"] == Post.query.count()
    assert 100 <= report["posts_created"] <= 400
    assert report["users_created"] > 0
    assert report["subdeaddits_created"] > 0
    assert report["comments_created"] > 0
    for key in (
        "users_created",
        "subdeaddits_created",
        "posts_created",
        "comments_created",
        "votes_created",
        "skipped_existing_users",
        "skipped_existing_subdeaddits",
        "window_days",
        "seed",
        "vote_probability_effective",
        "dry_run",
        "elapsed_seconds",
    ):
        assert key in report

    seeded_posts = Post.query.filter_by(model="seed").all()
    for post in seeded_posts:
        assert WINDOW_START <= post.created_at <= NOW
    for comment in Comment.query.filter_by(model="seed").all():
        parent_post = db_session.get(Post, comment.post_id)
        assert parent_post is not None
        assert parent_post.created_at < comment.created_at <= NOW
        if comment.parent_id is not None:
            depth, walker = 1, comment
            while walker.parent_id is not None:
                walker = db_session.get(Comment, walker.parent_id)
                depth += 1
            assert depth <= 3
            assert walker.created_at < comment.created_at

    # Power-law spread: not all identical timestamps.
    assert len({p.created_at for p in seeded_posts}) > len(seeded_posts) // 2

    # Evening hours (18-23) strictly more frequent than 4am.
    hour_counts = [0] * 24
    for post in seeded_posts:
        hour_counts[post.created_at.hour] += 1
    assert sum(hour_counts[18:24]) > hour_counts[4]


# 2. Exact-sum invariants ---------------------------------------------------


def test_exact_sum_invariants(app, pinned_now, db_session):
    report = seeding.seed_history(days=14, seed=42, now=NOW)
    assert report["votes_created"] > 0

    checked = 0
    for model, fk in ((Post, Vote.post_id), (Comment, Vote.comment_id)):
        items = model.query.filter(model.vote_count > 0).all()
        for item in items:
            votes = _db.session.query(Vote).filter(fk == item.id).all()
            assert len(votes) == item.vote_count
            assert sum(v.value for v in votes) == item.score
            assert item.score == item.upvote_count
            checked += 1
    assert checked > 0



def test_vote_attention_shape_and_hot_feed(app, pinned_now):
    """Seeded attention is long-tailed positive; hot feed reflects scores."""
    seeding.seed_history(days=14, seed=42, now=NOW)

    posts = Post.query.filter_by(model="seed").all()
    assert len(posts) > 0
    # (a) >= 30% of posts carry a positive score.
    assert sum(1 for p in posts if p.score > 0) >= 0.3 * len(posts)
    # (b) at least one viral outlier.
    assert max(p.score for p in posts) > 20
    # (c) hot feed's top-ranked post has a positive score.
    top = Post.query.order_by(*ranking.post_order_by("hot")).first()
    assert top is not None and top.score > 0
    # (d) no fabricated display score without vote rows.
    for model in (Post, Comment):
        fabricated = model.query.filter(
            model.vote_count == 0, model.upvote_count != 0
        ).count()
        assert fabricated == 0


# 3. Karma consistency -------------------------------------------------------


def test_karma_matches_effective_scores(app, pinned_now, db_session):
    seeding.seed_history(days=14, seed=42, now=NOW)

    def _effective_sums(model, owner_col):
        rows = (
            db_session.query(
                owner_col,
                func.sum(
                    case((model.vote_count > 0, model.score), else_=model.upvote_count)
                ),
            )
            .group_by(owner_col)
            .all()
        )
        return {owner: int(total or 0) for owner, total in rows}

    expected_post = _effective_sums(Post, Post.user)
    expected_comment = _effective_sums(Comment, Comment.user)

    for user in User.query.all():
        assert user.post_karma == expected_post.get(user.username, 0)
        assert user.comment_karma == expected_comment.get(user.username, 0)


# 4. Determinism --------------------------------------------------------------


def test_determinism_across_fresh_runs(app_factory, monkeypatch):
    def _snapshot(app):
        with app.app_context():
            _db.create_all()
            monkeypatch.setattr(seeding, "_now", lambda: NOW)
            report = seeding.seed_history(days=14, seed=42, now=NOW)
            tuples = [
                (v.id, v.voter, v.value, v.created_at.isoformat())
                for v in Vote.query.order_by(Vote.id).all()
            ]
            digest = hashlib.md5(repr(sorted(tuples)).encode()).hexdigest()
        return report, digest

    report_a, digest_a = _snapshot(app_factory())
    report_b, digest_b = _snapshot(app_factory())

    for key in (
        "users_created",
        "subdeaddits_created",
        "posts_created",
        "comments_created",
        "votes_created",
    ):
        assert report_a[key] == report_b[key], key
    assert digest_a == digest_b


# 5. Decay to zero ------------------------------------------------------------


def test_decay_zero_votes_and_warning(app, pinned_now, caplog):
    with app.app_context():
        Setting.set_value("SEED_DECAY_DAYS", "0", "test")
        with caplog.at_level(logging.WARNING, logger="deaddit.dynamics.seeding"):
            report = seeding.seed_history(days=14, seed=42, now=NOW)

    assert report["votes_created"] == 0
    assert report["vote_probability_effective"] == 0
    assert any("decayed to 0" in rec.message for rec in caplog.records)
    assert Vote.query.count() == 0


def test_decay_via_stale_anchor(app, pinned_now):
    with app.app_context():
        anchor = NOW - timedelta(days=365)
        Setting.set_value("SEED_ANCHOR_AT", anchor.isoformat(), "test")
        report = seeding.seed_history(days=14, seed=42, now=NOW)

    assert report["votes_created"] == 0


def test_anchor_persisted_on_first_real_run(app, pinned_now):
    with app.app_context():
        assert Setting.get_value("SEED_ANCHOR_AT") is None
        seeding.seed_history(days=14, seed=42, now=NOW)
        assert Setting.get_value("SEED_ANCHOR_AT") == NOW.isoformat()


# 6. Top-up --------------------------------------------------------------------


def test_top_up_second_run_adds_rows_no_error(app, pinned_now):
    first = seeding.seed_history(days=14, seed=42, now=NOW)
    posts_after_first = Post.query.count()

    second = seeding.seed_history(days=14, seed=43, now=NOW)

    assert second["posts_created"] > 0
    assert Post.query.count() > posts_after_first
    assert second["skipped_existing_subdeaddits"] == first["subdeaddits_created"]


def test_top_up_preserves_existing_users(app, pinned_now):
    seeding.seed_history(days=7, seed=7, now=NOW)
    snapshot = {
        u.username: (u.created_at, u.age, u.interests) for u in User.query.all()
    }
    seeding.seed_history(days=7, seed=99, now=NOW)
    for username, (created_at, age, interests) in snapshot.items():
        user = _db.session.get(User, username)
        assert user is not None
        # Existing user rows are never mutated by a top-up run.
        assert user.created_at == created_at
        assert user.age == age
        assert user.interests == interests


# 7. Production guard ------------------------------------------------------------


def test_cli_refuses_production_without_flag(monkeypatch):
    prod_app = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite:///deaddit.db"})
    monkeypatch.setattr(cli_module, "create_app", lambda config=None: prod_app)

    result = CliRunner().invoke(cli_module.cli, ["dynamics", "seed-history"])

    assert result.exit_code != 0
    assert "i-know-this-is-prod" in result.output


def test_cli_flag_wiring(monkeypatch):
    from unittest import mock

    prod_app = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite:///deaddit.db"})
    monkeypatch.setattr(cli_module, "create_app", lambda config=None: prod_app)
    stub = mock.Mock(return_value={"dry_run": False})
    monkeypatch.setattr(cli_module.seeding, "seed_history", stub)

    result = CliRunner().invoke(
        cli_module.cli,
        ["dynamics", "seed-history", "--days", "7", "--i-know-this-is-prod"],
    )

    assert result.exit_code == 0
    stub.assert_called_once()
    kwargs = stub.call_args.kwargs
    assert kwargs["allow_production"] is True
    assert kwargs["days"] == 7 and kwargs["seed"] == 42 and kwargs["dry_run"] is False


def test_runtime_guard_inside_seeder():
    """Direct call against a prod-shaped URI raises without allow_production."""
    prod_app = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite:///deaddit.db"})
    with prod_app.app_context(), pytest.raises(RuntimeError):
        seeding.seed_history(days=14, seed=42)


# 8. Migration round-trip + single head -------------------------------------------


def _user_sub_columns(db_path):
    conn = sqlite3.connect(db_path)
    try:
        users = {r[1] for r in conn.execute("PRAGMA table_info(user)").fetchall()}
        subs = {r[1] for r in conn.execute("PRAGMA table_info(subdeaddit)").fetchall()}
    finally:
        conn.close()
    return users, subs


def test_migration_round_trip(tmp_path):
    db_path = tmp_path / "mig.db"
    app = create_app(
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "TESTING": True}
    )
    runner = app.test_cli_runner()

    result = runner.invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output
    users, subs = _user_sub_columns(db_path)
    assert "created_at" in users
    assert "created_at" in subs

    down = runner.invoke(args=["db", "downgrade", D5_DOWN])
    assert down.exit_code == 0, down.output
    users, subs = _user_sub_columns(db_path)
    assert "created_at" not in users
    assert "created_at" not in subs

    up = runner.invoke(args=["db", "upgrade"])
    assert up.exit_code == 0, up.output
    users, subs = _user_sub_columns(db_path)
    assert "created_at" in users and "created_at" in subs


def test_single_head():
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    cfg = AlembicConfig()
    cfg.set_main_option("script_location", "migrations")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1  # exactly one head, even as sibling lanes stack
    rev = script.get_revision(D5_REV)
    assert rev.down_revision == D5_DOWN
    # D5 revision remains an ancestor of the (single) head.
    assert script.get_revision(heads[0]).revision == heads[0]
    walked = {s.revision for s in script.walk_revisions()}
    assert D5_REV in walked


# 9. Hot feed sanity -----------------------------------------------------------------


def test_hot_feed_ordered_and_nonempty(app, pinned_now):
    seeding.seed_history(days=14, seed=42, now=NOW)

    posts = Post.query.order_by(*ranking.post_order_by("hot")).limit(20).all()
    assert posts

    keys = [
        ranking.post_rank_key("hot", score=p.score, created_at=p.created_at, now=NOW)
        for p in posts
    ]
    assert keys == sorted(keys, reverse=True)


# Fixtures ----------------------------------------------------------------------


@pytest.fixture()
def app_factory(monkeypatch):
    """Fresh in-memory app per invocation (for cross-run determinism)."""
    apps = []
    monkeypatch.setattr(seeding, "_now", lambda: NOW)

    def _make():
        app = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite://", "TESTING": True})
        apps.append(app)
        return app

    yield _make
    for app in apps:
        with app.app_context():
            _db.session.remove()
            _db.drop_all()
