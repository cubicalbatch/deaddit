"""Add LLMProvider table for admin UI provider management

Revision ID: 8f1e2d3c4b5a
Revises: f3b8e2a6c9d4
Create Date: 2026-08-26 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "8f1e2d3c4b5a"
down_revision = "f3b8e2a6c9d4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "llm_provider",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("api_url", sa.String(length=255), nullable=False),
        sa.Column("api_key", sa.String(length=255), nullable=True),
        sa.Column("default_model", sa.String(length=100), nullable=True),
        sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index(op.f("ix_llm_provider_api_url"), "llm_provider", ["api_url"])


def downgrade():
    op.drop_index(op.f("ix_llm_provider_api_url"), table_name="llm_provider")
    op.drop_table("llm_provider")
