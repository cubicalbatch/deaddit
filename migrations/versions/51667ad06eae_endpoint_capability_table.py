"""endpoint capability table

Revision ID: 51667ad06eae
Revises: 5b2dab0b6816
Create Date: 2026-08-24

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "51667ad06eae"
down_revision = "5b2dab0b6816"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "endpoint_capability",
        sa.Column("api_url", sa.String(length=255), nullable=False),
        sa.Column("model_name", sa.String(length=100), nullable=False),
        sa.Column("supports_tools", sa.Boolean(), nullable=False),
        sa.Column("supports_streaming", sa.Boolean(), nullable=True),
        sa.Column("context_tokens", sa.Integer(), nullable=True),
        sa.Column("probed_at", sa.DateTime(), nullable=True),
        sa.Column("probe_method", sa.String(length=20), nullable=True),
        sa.PrimaryKeyConstraint("api_url", "model_name"),
    )


def downgrade():
    op.drop_table("endpoint_capability")
