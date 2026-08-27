"""image provider admin-entered api key

Revision ID: b4d8a2c6f0e1
Revises: 30e1e6cd0b24
Create Date: 2026-08-27 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b4d8a2c6f0e1"
down_revision = "30e1e6cd0b24"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("image_provider", sa.Column("api_key", sa.String(length=255)))


def downgrade():
    op.drop_column("image_provider", "api_key")
