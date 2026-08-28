"""Add default image provider flag.

Revision ID: c5e7a9b1d3f6
Revises: 2b7c9e4d1f06
Create Date: 2026-08-28 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c5e7a9b1d3f6"
down_revision = "2b7c9e4d1f06"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "image_provider",
        sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
    )
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE image_provider SET is_default = 1 "
            "WHERE id = (SELECT MIN(id) FROM image_provider)"
        )
    )


def downgrade():
    op.drop_column("image_provider", "is_default")
