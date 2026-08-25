"""llm usage ledger, model prices and model routes

Revision ID: a3f1c9d2b4e7
Revises: 51667ad06eae
Create Date: 2026-08-25

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a3f1c9d2b4e7"
down_revision = "51667ad06eae"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("request_id", sa.String(length=32), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("api_url", sa.String(length=255), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("action", sa.String(length=40), nullable=True),
        sa.Column("agent", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("error_type", sa.String(length=80), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost", sa.Float(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
    )
    op.create_index("ix_llm_usage_created_at", "llm_usage", ["created_at"])

    op.create_table(
        "model_price",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pattern", sa.String(length=200), nullable=False),
        sa.Column("prompt_price_per_1k", sa.Float(), nullable=False),
        sa.Column("completion_price_per_1k", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("effective_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("pattern"),
    )

    op.create_table(
        "model_route",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tier", sa.String(length=40), nullable=False),
        sa.Column("api_url", sa.String(length=255), nullable=True),
        sa.Column("model_name", sa.String(length=120), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_model_route_tier", "model_route", ["tier"])


def downgrade():
    op.drop_index("ix_model_route_tier", table_name="model_route")
    op.drop_table("model_route")
    op.drop_table("model_price")
    op.drop_index("ix_llm_usage_created_at", table_name="llm_usage")
    op.drop_table("llm_usage")
