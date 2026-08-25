"""agent runtime tables

Revision ID: c8f2a4e61b9d
Revises: a3f1c9d2b4e7
Create Date: 2026-08-24

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c8f2a4e61b9d"
down_revision = "a3f1c9d2b4e7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_username", sa.String(length=50), nullable=False),
        sa.Column("autonomy_tier", sa.String(length=20), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_username"], ["user.username"]),
        sa.UniqueConstraint("user_username"),
    )

    op.create_table(
        "agent_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("turn_count", sa.Integer(), nullable=False),
        sa.Column("action_count", sa.Integer(), nullable=False),
        sa.Column("token_usage", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agent.id"]),
    )
    op.create_index("ix_agent_run_agent_id", "agent_run", ["agent_id"])

    op.create_table(
        "agent_turn",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("request_messages", sa.JSON(), nullable=False),
        sa.Column("response_message", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["agent_run.id"]),
    )
    op.create_index("ix_agent_turn_run_id", "agent_turn", ["run_id"])

    op.create_table(
        "tool_call",
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
        sa.ForeignKeyConstraint(["turn_id"], ["agent_turn.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["agent_run.id"]),
    )
    op.create_index("ix_tool_call_run_id", "tool_call", ["run_id"])
    op.create_index("ix_tool_call_created_at", "tool_call", ["created_at"])

    op.create_table(
        "agent_memory",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agent.id"]),
    )
    op.create_index("ix_agent_memory_agent_id", "agent_memory", ["agent_id"])


def downgrade():
    op.drop_index("ix_agent_memory_agent_id", table_name="agent_memory")
    op.drop_table("agent_memory")
    op.drop_index("ix_tool_call_created_at", table_name="tool_call")
    op.drop_index("ix_tool_call_run_id", table_name="tool_call")
    op.drop_table("tool_call")
    op.drop_index("ix_agent_turn_run_id", table_name="agent_turn")
    op.drop_table("agent_turn")
    op.drop_index("ix_agent_run_agent_id", table_name="agent_run")
    op.drop_table("agent_run")
    op.drop_table("agent")
