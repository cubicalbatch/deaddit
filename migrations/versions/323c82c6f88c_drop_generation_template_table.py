"""drop generation_template table

Revision ID: 323c82c6f88c
Revises: 8f1e2d3c4b5a
Create Date: 2026-08-26 21:15:24.921416

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision = "323c82c6f88c"
down_revision = "8f1e2d3c4b5a"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table("generation_template")


def downgrade():
    op.create_table(
        "generation_template",
        sa.Column("id", sa.INTEGER(), nullable=False),
        sa.Column("name", sa.VARCHAR(length=100), nullable=False),
        sa.Column("description", sa.TEXT(), nullable=True),
        sa.Column("type", sa.VARCHAR(length=17), nullable=False),
        sa.Column("parameters", sqlite.JSON(), nullable=False),
        sa.Column("created_at", sa.DATETIME(), nullable=True),
        sa.Column("updated_at", sa.DATETIME(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
