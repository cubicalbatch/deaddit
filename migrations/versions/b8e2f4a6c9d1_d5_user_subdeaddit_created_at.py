"""D5 user/subdeaddit created_at columns

Revision ID: b8e2f4a6c9d1
Revises: f7a3c9d1e5b2
Create Date: 2026-08-25 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b8e2f4a6c9d1"
down_revision = "f7a3c9d1e5b2"
branch_labels = None
depends_on = None


def upgrade():
    # Phase D5: history seeding (minimal additive; plain ADD COLUMN works on SQLite).
    op.add_column("user", sa.Column("created_at", sa.DateTime(), nullable=True))
    op.add_column("subdeaddit", sa.Column("created_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("subdeaddit", "created_at")
    op.drop_column("user", "created_at")
