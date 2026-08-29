"""simulated voting policy and hourly summaries

Revision ID: d6a4f8c2b901
Revises: c4b9e2f7a1d3
Create Date: 2026-08-28

"""

import sqlalchemy as sa
from alembic import op

revision = "d6a4f8c2b901"
down_revision = "c4b9e2f7a1d3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "vote_cadence_policy",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("preset", sa.String(length=16), nullable=False),
        sa.Column("algorithm_version", sa.Integer(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "preset IN ('quiet', 'natural', 'busy', 'custom')",
            name="ck_vote_cadence_policy_preset",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_vote_cadence_policy_effective_at",
        "vote_cadence_policy",
        ["effective_at"],
        unique=False,
    )

    op.create_table(
        "vote_simulation_hourly",
        sa.Column("hour", sa.DateTime(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("ticks", sa.Integer(), server_default="0", nullable=False),
        sa.Column("errors", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "active_proposals", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "archive_proposals", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "revival_proposals", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("inserted_votes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("switched_votes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("upvotes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("downvotes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cap_skips", sa.Integer(), server_default="0", nullable=False),
        sa.Column("min_gap_skips", sa.Integer(), server_default="0", nullable=False),
        sa.Column("no_voter_skips", sa.Integer(), server_default="0", nullable=False),
        sa.Column("guardrail_skips", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("hour", "mode"),
    )


def downgrade():
    op.drop_table("vote_simulation_hourly")
    op.drop_index(
        "ix_vote_cadence_policy_effective_at", table_name="vote_cadence_policy"
    )
    op.drop_table("vote_cadence_policy")
