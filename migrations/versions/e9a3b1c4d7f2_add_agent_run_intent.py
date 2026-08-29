"""add agent_run intent

Revision ID: e9a3b1c4d7f2
Revises: d6a4f8c2b901
Create Date: 2026-08-28

"""

import sqlalchemy as sa
from alembic import op

revision = "e9a3b1c4d7f2"
down_revision = "d6a4f8c2b901"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("agent_run") as batch_op:
        batch_op.add_column(sa.Column("intent", sa.String(length=12), nullable=True))


def downgrade():
    bind = op.get_bind()
    if bind.engine.name == "sqlite":
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    with op.batch_alter_table("agent_run") as batch_op:
        batch_op.drop_column("intent")
    if bind.engine.name == "sqlite":
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")
