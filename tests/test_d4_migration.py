"""Phase D4 slice 1: moderation schema migration test (tmp sqlite only)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

import deaddit
from deaddit import create_app
from deaddit.extensions import db
from deaddit.models import (
    Ban,
    Comment,
    Post,
    Report,
    Subdeaddit,
    SubdeadditModerator,
    User,
)

_D4_REVISION = "f7a3c9d1e5b2"
_PRE_D4_HEAD = "e5d7f9a1c3b9"

_NEW_TABLES = {"report", "subdeaddit_moderator", "ban"}
_SOFT_COLUMNS = {"removed", "removed_by", "removal_reason", "removed_at"}


def _table_names(db_path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def _columns(db_path, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return {r[1] for r in rows}


def _index_names(db_path, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    finally:
        conn.close()
    return {r[1] for r in rows}


def _heads() -> list[str]:
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        str(Path(deaddit.__file__).resolve().parent.parent / "migrations"),
    )
    script = ScriptDirectory.from_config(cfg)
    return script.get_heads()


def _d4_in_chain() -> bool:
    """D4's revision is an ancestor of (or is) the single head."""
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        str(Path(deaddit.__file__).resolve().parent.parent / "migrations"),
    )
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    if len(heads) != 1:
        return False
    return any(rev.revision == _D4_REVISION for rev in script.walk_revisions())


def test_single_head_after_full_upgrade(tmp_path):
    db_path = tmp_path / "head.db"
    app = create_app(
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "TESTING": True}
    )
    runner = app.test_cli_runner()

    result = runner.invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output

    # Exactly one head (later lanes may chain past D4), and D4 sits in its
    # linear ancestry. No instance/deaddit.db touched.
    heads = _heads()
    assert len(heads) == 1, f"branched alembic heads: {heads}"
    assert _d4_in_chain(), f"{_D4_REVISION} not in the ancestry of sole head {heads}"


def test_upgrade_then_downgrade_round_trip(tmp_path):
    db_path = tmp_path / "mig.db"
    app = create_app(
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "TESTING": True}
    )
    runner = app.test_cli_runner()

    up = runner.invoke(args=["db", "upgrade"])
    assert up.exit_code == 0, up.output

    tables = _table_names(db_path)
    assert _NEW_TABLES <= tables
    for table in ("post", "comment"):
        assert _SOFT_COLUMNS <= _columns(db_path, table)
        assert f"ix_{table}_removed" in _index_names(db_path, table)
    assert {
        "ix_report_reporter",
        "ix_report_post_id",
        "ix_report_comment_id",
        "ix_report_created_at",
    } <= _index_names(db_path, "report")
    assert {"ix_ban_username", "ix_ban_lifted_at"} <= _index_names(db_path, "ban")

    # One step back removes everything the revision added.
    down = runner.invoke(args=["db", "downgrade", _PRE_D4_HEAD])
    assert down.exit_code == 0, down.output

    tables = _table_names(db_path)
    assert not (_NEW_TABLES & tables)
    for table in ("post", "comment"):
        cols = _columns(db_path, table)
        assert not (_SOFT_COLUMNS & cols), cols
        assert f"ix_{table}_removed" not in _index_names(db_path, table)

    # Forward again restores everything.
    up2 = runner.invoke(args=["db", "upgrade"])
    assert up2.exit_code == 0, up2.output

    assert _NEW_TABLES <= _table_names(db_path)
    for table in ("post", "comment"):
        assert _SOFT_COLUMNS <= _columns(db_path, table)
        assert f"ix_{table}_removed" in _index_names(db_path, table)


def test_orm_soft_removal_round_trip(tmp_path):
    db_path = tmp_path / "orm.db"
    app = create_app(
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "TESTING": True}
    )
    runner = app.test_cli_runner()
    result = runner.invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output

    with app.app_context():
        db.session.add_all(
            [
                Subdeaddit(name="t", description="test"),
                User(username="author"),
                User(username="mod"),
                User(username="snitch"),
            ]
        )
        db.session.commit()

        post = Post(
            title="t",
            content="c",
            subdeaddit_name="t",
            user="author",
            post_type="text",
        )
        comment = Comment(post=post, content="c", user="author")
        db.session.add_all([post, comment])
        db.session.commit()

        # Mod action: soft-remove both.
        post.removed = True
        post.removed_by = "mod"
        post.removal_reason = "spam"
        comment.removed = True
        comment.removed_by = "mod"
        comment.removal_reason = "spam"
        db.session.add_all(
            [
                Report(
                    reporter="snitch", post_id=post.id, reason="spam", status="open"
                ),
                Report(
                    reporter="snitch",
                    comment_id=comment.id,
                    reason="spam",
                    status="open",
                ),
                SubdeadditModerator(subdeaddit_name="t", username="mod"),
                Ban(username="author", reason="spam"),
                Ban(username="author", subdeaddit_name="t", reason="off-topic"),
            ]
        )
        pid, cid = post.id, comment.id
        db.session.commit()
        db.session.expunge_all()

    with app.app_context():
        post = db.session.get(Post, pid)
        comment = db.session.get(Comment, cid)
        assert (post.removed, post.removed_by, post.removal_reason) == (
            True,
            "mod",
            "spam",
        )
        assert (comment.removed, comment.removed_by, comment.removal_reason) == (
            True,
            "mod",
            "spam",
        )
        reports = Report.query.order_by(Report.id).all()
        assert [r.post_id for r in reports] == [pid, None]
        assert [r.comment_id for r in reports] == [None, cid]
        bans = Ban.query.order_by(Ban.id).all()
        assert [b.subdeaddit_name for b in bans] == [None, "t"]
        assert SubdeadditModerator.query.count() == 1
