"""image post domain tables

Revision ID: 1f095c2a711e
Revises: 323c82c6f88c
Create Date: 2026-08-27 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "1f095c2a711e"
down_revision = "323c82c6f88c"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "image_provider",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("provider_type", sa.String(length=20), nullable=False),
        sa.Column("credential_env", sa.String(length=100), nullable=False),
        sa.Column("default_model", sa.String(length=200), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "image_model",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("model_identifier", sa.String(length=200), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("category", sa.String(length=50), nullable=True),
        sa.Column("provider_metadata", sa.JSON(), nullable=True),
        sa.Column("compatibility_verdict", sa.String(length=20), nullable=True),
        sa.Column("compatibility_reason", sa.Text(), nullable=True),
        sa.Column("last_fetched", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["image_provider.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_id",
            "model_identifier",
            name="uq_image_model_provider_identifier",
        ),
    )
    op.create_index(
        "ix_image_model_provider_id", "image_model", ["provider_id"], unique=False
    )

    op.create_table(
        "post_image",
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("original_path", sa.String(length=300), nullable=False),
        sa.Column("thumbnail_path", sa.String(length=300), nullable=False),
        sa.Column("mime_type", sa.String(length=50), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("alt_text", sa.String(length=500), nullable=False),
        sa.Column("source_prompt", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=True),
        sa.Column("provider_snapshot", sa.String(length=100), nullable=False),
        sa.Column("model_snapshot", sa.String(length=200), nullable=False),
        sa.Column("request_snapshot", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["post_id"], ["post.id"]),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["image_provider.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("post_id"),
    )


def downgrade():
    op.drop_table("post_image")
    op.drop_index("ix_image_model_provider_id", table_name="image_model")
    op.drop_table("image_model")
    op.drop_table("image_provider")
