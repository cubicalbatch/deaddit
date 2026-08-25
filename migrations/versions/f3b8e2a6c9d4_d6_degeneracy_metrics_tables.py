"""Phase D6: anti-degeneracy instrumentation + metrics tables

Revision ID: f3b8e2a6c9d4
Revises: c7e2a9b4d1f6
Create Date: 2026-08-26 00:00:00.000000

Additive DDL only (plan §8): ``activity_event`` is the raw event log,
``platform_daily`` the nightly rollup, ``degeneracy_flag`` feeds the admin
watchlist and hot-feed demotion. No existing table or row is touched; the
revision therefore round-trips losslessly.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f3b8e2a6c9d4"
down_revision = "c7e2a9b4d1f6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "activity_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=True),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("username", sa.String(length=50), nullable=True),
        sa.Column("post_id", sa.Integer(), nullable=True),
        sa.Column("comment_id", sa.Integer(), nullable=True),
        sa.Column("meta", sa.Text(), nullable=True),
    )
    op.create_index(
        op.f("ix_activity_event_occurred_at"), "activity_event", ["occurred_at"]
    )
    op.create_index(
        op.f("ix_activity_event_event_type"), "activity_event", ["event_type"]
    )
    op.create_index(
        op.f("ix_activity_event_username"), "activity_event", ["username"]
    )

    op.create_table(
        "platform_daily",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("posts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comments", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("votes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reports", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_agents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("actions_per_active", sa.Float(), nullable=True),
        sa.Column("llm_tokens_in", sa.Integer(), nullable=True),
        sa.Column("llm_tokens_out", sa.Integer(), nullable=True),
        sa.Column("llm_cost_usd", sa.Float(), nullable=True),
        sa.Column("cost_per_engagement", sa.Float(), nullable=True),
        sa.Column("median_thread_depth", sa.Float(), nullable=True),
        sa.Column("dissent_share_avg", sa.Float(), nullable=True),
        sa.Column("gini_participation_avg", sa.Float(), nullable=True),
        sa.Column("provenance_json", sa.Text(), nullable=True),
    )

    op.create_table(
        "degeneracy_flag",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("username", sa.String(length=50), nullable=True),
        sa.Column("subdeaddit_name", sa.String(length=50), nullable=True),
        sa.Column("post_id", sa.Integer(), nullable=True),
        sa.Column("comment_id", sa.Integer(), nullable=True),
        sa.Column("metric", sa.Float(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index(op.f("ix_degeneracy_flag_kind"), "degeneracy_flag", ["kind"])
    op.create_index(
        op.f("ix_degeneracy_flag_username"), "degeneracy_flag", ["username"]
    )
    op.create_index(
        op.f("ix_degeneracy_flag_created_at"), "degeneracy_flag", ["created_at"]
    )


def downgrade():
    op.drop_index(op.f("ix_degeneracy_flag_created_at"), table_name="degeneracy_flag")
    op.drop_index(op.f("ix_degeneracy_flag_username"), table_name="degeneracy_flag")
    op.drop_index(op.f("ix_degeneracy_flag_kind"), table_name="degeneracy_flag")
    op.drop_table("degeneracy_flag")
    op.drop_table("platform_daily")
    op.drop_index(op.f("ix_activity_event_username"), table_name="activity_event")
    op.drop_index(op.f("ix_activity_event_event_type"), table_name="activity_event")
    op.drop_index(op.f("ix_activity_event_occurred_at"), table_name="activity_event")
    op.drop_table("activity_event")
