"""cascade agent runtime rows when their owners are deleted.

Revision ID: 2b7c9e4d1f06
Revises: 01fe7be10643
Create Date: 2026-08-27

SQLite has no ALTER CONSTRAINT support, so each affected runtime table is
rebuilt with its existing schema and the required ON DELETE CASCADE actions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "2b7c9e4d1f06"
down_revision = "01fe7be10643"
branch_labels = None
depends_on = None


_AGENT_PERSONA_CHECK = (
    "(persona_mode = 'fixed' AND user_username IS NOT NULL) OR "
    "(persona_mode = 'random' AND user_username IS NULL)"
)


def _agent_table(metadata: sa.MetaData, *, cascade: bool) -> sa.Table:
    ondelete = "CASCADE" if cascade else None
    return sa.Table(
        "agent",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "persona_mode",
            sa.String(length=12),
            nullable=False,
            server_default="fixed",
        ),
        sa.Column("user_username", sa.String(length=50), nullable=True),
        sa.Column("autonomy_tier", sa.String(length=20), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_username"], ["user.username"], ondelete=ondelete
        ),
        sa.CheckConstraint(_AGENT_PERSONA_CHECK, name="ck_agent_persona_mode_user"),
        sa.UniqueConstraint("user_username"),
    )


def _agent_run_table(metadata: sa.MetaData, *, cascade: bool) -> sa.Table:
    ondelete = "CASCADE" if cascade else None
    table = sa.Table(
        "agent_run",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("persona_username", sa.String(length=50), nullable=False),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("turn_count", sa.Integer(), nullable=False),
        sa.Column("action_count", sa.Integer(), nullable=False),
        sa.Column("token_usage", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agent.id"], ondelete=ondelete),
        sa.ForeignKeyConstraint(
            ["persona_username"], ["user.username"], ondelete=ondelete
        ),
        sa.Index("ix_agent_run_agent_id", "agent_id"),
        sa.Index("ix_agent_run_persona_username", "persona_username"),
        sa.Index(
            "uq_agent_run_running_persona",
            "persona_username",
            unique=True,
            sqlite_where=sa.text("status = 'running'"),
        ),
    )
    return table


def _agent_turn_table(metadata: sa.MetaData, *, cascade: bool) -> sa.Table:
    ondelete = "CASCADE" if cascade else None
    return sa.Table(
        "agent_turn",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("request_messages", sa.JSON(), nullable=False),
        sa.Column("response_message", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["agent_run.id"], ondelete=ondelete),
        sa.Index("ix_agent_turn_run_id", "run_id"),
    )


def _tool_call_table(metadata: sa.MetaData, *, cascade: bool) -> sa.Table:
    ondelete = "CASCADE" if cascade else None
    return sa.Table(
        "tool_call",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("turn_id", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["turn_id"], ["agent_turn.id"], ondelete=ondelete),
        sa.ForeignKeyConstraint(["run_id"], ["agent_run.id"], ondelete=ondelete),
        sa.Index("ix_tool_call_run_id", "run_id"),
        sa.Index("ix_tool_call_created_at", "created_at"),
    )


def _agent_memory_table(metadata: sa.MetaData, *, cascade: bool) -> sa.Table:
    ondelete = "CASCADE" if cascade else None
    return sa.Table(
        "agent_memory",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_username", sa.String(length=50), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_username"], ["user.username"], ondelete=ondelete
        ),
        sa.Index(
            "ix_agent_memory_user_kind_created",
            "user_username",
            "kind",
            "created_at",
        ),
    )


def _rebuild(table_name: str, table_factory, *, cascade: bool) -> None:
    metadata = sa.MetaData()
    with op.batch_alter_table(
        table_name,
        recreate="always",
        copy_from=table_factory(metadata, cascade=cascade),
    ):
        pass


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    _rebuild("agent", _agent_table, cascade=True)
    _rebuild("agent_run", _agent_run_table, cascade=True)
    _rebuild("agent_turn", _agent_turn_table, cascade=True)
    _rebuild("tool_call", _tool_call_table, cascade=True)
    _rebuild("agent_memory", _agent_memory_table, cascade=True)
    bind.connection.connection.commit()
    bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    _rebuild("agent_memory", _agent_memory_table, cascade=False)
    _rebuild("tool_call", _tool_call_table, cascade=False)
    _rebuild("agent_turn", _agent_turn_table, cascade=False)
    _rebuild("agent_run", _agent_run_table, cascade=False)
    _rebuild("agent", _agent_table, cascade=False)
    bind.connection.connection.commit()
    bind.exec_driver_sql("PRAGMA foreign_keys=ON")
