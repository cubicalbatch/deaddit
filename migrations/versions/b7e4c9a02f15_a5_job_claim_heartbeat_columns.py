"""job claim and heartbeat columns

Revision ID: b7e4c9a02f15
Revises: c8f2a4e61b9d
Create Date: 2026-08-25 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b7e4c9a02f15"
down_revision = "c8f2a4e61b9d"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("job", sa.Column("claimed_at", sa.DateTime(), nullable=True))
    op.add_column("job", sa.Column("worker_id", sa.String(length=64), nullable=True))
    op.add_column("job", sa.Column("heartbeat_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("job", "heartbeat_at")
    op.drop_column("job", "worker_id")
    op.drop_column("job", "claimed_at")
