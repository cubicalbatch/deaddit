"""5a vision capability column

Revision ID: 30e1e6cd0b24
Revises: 1f095c2a711e
Create Date: 2026-08-27 00:13:03.374451

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "30e1e6cd0b24"
down_revision = "1f095c2a711e"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "endpoint_capability",
        sa.Column("supports_vision", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "endpoint_capability",
        sa.Column("vision_probed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "endpoint_capability",
        sa.Column("vision_probe_method", sa.String(length=20), nullable=True),
    )


def downgrade():
    op.drop_column("endpoint_capability", "vision_probe_method")
    op.drop_column("endpoint_capability", "vision_probed_at")
    op.drop_column("endpoint_capability", "supports_vision")
