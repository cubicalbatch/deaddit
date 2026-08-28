"""Persistence and schema guarantees for the generated-website domain model.

Mirrors tests/test_img_models.py for the website twin of PostImage. Covers
one-site-per-post uniqueness, public_path/storage_path uniqueness, cascade
on hard post deletion, SET NULL on agent/agent_run deletion, and that the
public serializer never leaks private generation provenance.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from deaddit import create_app
from deaddit.models import Agent, AgentRun, GeneratedWebsite, Post, Subdeaddit, User

_PRE_WEBSITE_HEAD = "f4a8c2d6b901"
_WEBSITE_TABLES = {"generated_website"}

_PRIVATE_FIELDS = (
    "source_description",
    "storage_path",
    "api_url_snapshot",
    "model_snapshot",
    "request_id",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "finish_reason",
    "agent_id",
    "agent_run_id",
    "creator_username_snapshot",
    "byte_size",
    "sha256",
    "id",
    "post_id",
    "created_at",
)


def _post(db_session, *, user="website-author", subdeaddit="website-subdeaddit"):
    user_row = User(username=user)
    subdeaddit_row = Subdeaddit(name=subdeaddit)
    post = Post(
        title="A link post about a fictional site",
        content="Found this while browsing.",
        user=user_row.username,
        subdeaddit_name=subdeaddit_row.name,
    )
    db_session.add_all([user_row, subdeaddit_row, post])
    db_session.commit()
    return post


def _website(post_id, *, agent_id=None, agent_run_id=None, public_path=None):
    return GeneratedWebsite(
        post_id=post_id,
        public_path=public_path or "www.fake-observatory.example/aurora-map.html",
        storage_path="pages/11111111-1111-1111-1111-111111111111.html",
        hostname="www.fake-observatory.example",
        page_name="aurora-map.html",
        source_description="A private, thorough site-generation brief.",
        byte_size=4096,
        sha256="a" * 64,
        agent_id=agent_id,
        creator_username_snapshot="website-author",
        agent_run_id=agent_run_id,
        api_url_snapshot="http://example.test/v1",
        model_snapshot="example-model",
        request_id="req-abc123",
        prompt_tokens=100,
        completion_tokens=200,
        total_tokens=300,
        finish_reason="stop",
    )


def test_website_rows_are_unique_per_post_and_outlive_agent_and_run(app, db_session):
    post = _post(db_session)
    assert post.website is None, "a text post carries no generated website"

    agent = Agent(user_username=post.user)
    db_session.add(agent)
    db_session.commit()

    run = AgentRun(
        agent_id=agent.id,
        persona_username=post.user,
        trigger="manual",
        status="completed",
        started_at=datetime(2026, 1, 1, 12),
    )
    db_session.add(run)
    db_session.commit()

    website = _website(post.id, agent_id=agent.id, agent_run_id=run.id)
    db_session.add(website)
    db_session.commit()
    assert post.website is not None
    assert post.website.hostname == "www.fake-observatory.example"

    # At most one website per post.
    db_session.add(
        _website(post.id, public_path="www.other-example.example/other-page.html")
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # public_path must be unique across posts.
    other_post = _post(db_session, user="other-author", subdeaddit="other-sub")
    dupe_public_path = _website(other_post.id, public_path=website.public_path)
    dupe_public_path.storage_path = "pages/22222222-2222-2222-2222-222222222222.html"
    db_session.add(dupe_public_path)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # storage_path must be unique across posts (opaque path never reused).
    dupe_storage_path = _website(
        other_post.id, public_path="www.third-example.example/third-page.html"
    )
    dupe_storage_path.storage_path = website.storage_path
    db_session.add(dupe_storage_path)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Deleting the agent run unlinks it but keeps the provenance snapshot.  Keep
    # this first to verify the website's independent ON DELETE SET NULL behavior;
    # Agent deletion below also cascades any remaining runtime rows.
    db_session.delete(run)
    db_session.commit()
    remaining = db_session.get(GeneratedWebsite, website.id)
    assert remaining.agent_run_id is None
    assert remaining.agent_id == agent.id

    # Deleting the agent unlinks it too, independently of the run.
    db_session.delete(agent)
    db_session.commit()
    remaining = db_session.get(GeneratedWebsite, website.id)
    assert remaining.agent_id is None
    assert remaining.creator_username_snapshot == "website-author"
    assert remaining.source_description == (
        "A private, thorough site-generation brief."
    )
    assert remaining.api_url_snapshot == "http://example.test/v1"
    assert remaining.model_snapshot == "example-model"
    assert remaining.request_id == "req-abc123"
    assert (remaining.prompt_tokens, remaining.completion_tokens) == (100, 200)
    assert remaining.total_tokens == 300
    assert remaining.finish_reason == "stop"

    # Deleting the post cascades to its website row (hard delete).
    db_session.delete(post)
    db_session.commit()
    assert db_session.get(GeneratedWebsite, website.id) is None


def test_public_serializer_exposes_only_hostname_page_name_and_url(app, db_session):
    post = _post(db_session)
    website = _website(post.id)
    db_session.add(website)
    db_session.commit()

    public = website.to_public_dict()

    assert public == {
        "url": "/out/www.fake-observatory.example/aurora-map.html",
        "hostname": "www.fake-observatory.example",
        "page_name": "aurora-map.html",
    }
    for field in _PRIVATE_FIELDS:
        assert field not in public, f"{field} leaked into the public serializer"


def test_website_table_migration_round_trip(tmp_path):
    db_path = tmp_path / "mig.db"
    app = create_app(
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "TESTING": True}
    )
    runner = app.test_cli_runner()

    def query(sql, params=()):
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def execute(sql, params=()):
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def tables():
        return {
            row[0]
            for row in query(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    # Upgrade to the pre-website head first and populate representative
    # rows in tables that already existed, matching a real deployment.
    pre = runner.invoke(args=["db", "upgrade", _PRE_WEBSITE_HEAD])
    assert pre.exit_code == 0, pre.output
    stamp = "2026-08-27 12:00:00"
    execute("INSERT INTO user (username, bio) VALUES ('alice', 'bio')")
    execute("INSERT INTO subdeaddit (name, description) VALUES ('ask', 'Ask')")
    execute(
        """
        INSERT INTO post
            (id, title, score, vote_count, content, subdeaddit_name, user,
             created_at, model)
        VALUES (11, 'Alice post', 0, 0, 'Body', 'ask', 'alice', ?, 'seed')
        """,
        (stamp,),
    )
    execute(
        """
        INSERT INTO agent
            (id, user_username, autonomy_tier, is_enabled, status, config,
             state, consecutive_failures)
        VALUES (101, 'alice', 'regular', 1, 'idle', '{}', '{}', 0)
        """
    )
    execute(
        """
        INSERT INTO agent_run
            (id, agent_id, persona_username, trigger, status, started_at,
             turn_count, action_count)
        VALUES (201, 101, 'alice', 'manual', 'completed', ?, 0, 0)
        """,
        (stamp,),
    )

    upgraded = runner.invoke(args=["db", "upgrade"])
    assert upgraded.exit_code == 0, upgraded.output
    assert _WEBSITE_TABLES <= tables()

    columns = {row[1] for row in query("PRAGMA table_info(generated_website)")}
    assert {
        "id",
        "post_id",
        "public_path",
        "storage_path",
        "hostname",
        "page_name",
        "source_description",
        "byte_size",
        "sha256",
        "agent_id",
        "creator_username_snapshot",
        "agent_run_id",
        "api_url_snapshot",
        "model_snapshot",
        "request_id",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "finish_reason",
        "created_at",
    } <= columns

    fks = query("PRAGMA foreign_key_list(generated_website)")
    assert any(
        row[3] == "post_id" and row[2] == "post" and row[6] == "CASCADE" for row in fks
    )
    assert any(
        row[3] == "agent_id" and row[2] == "agent" and row[6] == "SET NULL"
        for row in fks
    )
    assert any(
        row[3] == "agent_run_id" and row[2] == "agent_run" and row[6] == "SET NULL"
        for row in fks
    )

    indexes = {row[1] for row in query("PRAGMA index_list(generated_website)")}
    assert "ix_generated_website_public_path" in indexes
    assert "ix_generated_website_post_id" in indexes
    assert "ix_generated_website_agent_id" in indexes
    assert "ix_generated_website_created_at" in indexes

    # Populate a website row against the already-populated data and prove
    # DB-level CASCADE/SET NULL fire for a raw SQL delete, not only ORM
    # session.delete() (relevant to bulk-delete paths added in a later
    # subphase).
    execute(
        """
        INSERT INTO generated_website
            (id, post_id, public_path, storage_path, hostname, page_name,
             source_description, byte_size, sha256, agent_id,
             creator_username_snapshot, agent_run_id, api_url_snapshot,
             model_snapshot, request_id, prompt_tokens, completion_tokens,
             total_tokens, finish_reason, created_at)
        VALUES
            (1, 11, 'www.example.test/page.html',
             'pages/11111111-1111-1111-1111-111111111111.html',
             'www.example.test', 'page.html', 'brief', 10, ?, 101, 'alice',
             201, 'http://example.test/v1', 'example-model', 'req-1',
             1, 2, 3, 'stop', ?)
        """,
        ("a" * 64, stamp),
    )
    execute("DELETE FROM agent_run WHERE id = 201")
    row = query("SELECT agent_id, agent_run_id FROM generated_website WHERE id = 1")[0]
    assert row == (101, None)
    execute("DELETE FROM agent WHERE id = 101")
    row = query("SELECT agent_id FROM generated_website WHERE id = 1")[0]
    assert row == (None,)
    execute("DELETE FROM post WHERE id = 11")
    assert query("SELECT id FROM generated_website WHERE id = 1") == []

    # Downgrade removes only this table; the populated data it references
    # (independent of the deletes above) is untouched.
    down = runner.invoke(args=["db", "downgrade", _PRE_WEBSITE_HEAD])
    assert down.exit_code == 0, down.output
    assert not (_WEBSITE_TABLES & tables())
    assert query("SELECT username FROM user WHERE username = 'alice'") == [("alice",)]

    again = runner.invoke(args=["db", "upgrade"])
    assert again.exit_code == 0, again.output
    assert _WEBSITE_TABLES <= tables()
