"""drop job unused columns and api_endpoint_config

Revision ID: e7f1a3b5c9d2
Revises: c6d8f3a0b4e2
Create Date: 2026-08-29 18:30:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e7f1a3b5c9d2"
down_revision = "c6d8f3a0b4e2"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.engine.name == "sqlite":
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")

    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_job_rq_job_id"))
        batch_op.drop_column("rq_job_id")
        batch_op.drop_column("estimated_completion")

    if bind.engine.name == "sqlite":
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")

    op.drop_table("api_endpoint_config")


def downgrade():
    op.create_table(
        "api_endpoint_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("api_url", sa.String(length=255), nullable=False),
        sa.Column("default_model", sa.String(length=100), nullable=True),
        sa.Column("last_updated", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("api_endpoint_config", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_api_endpoint_config_api_url"), ["api_url"], unique=True
        )

    bind = op.get_bind()
    if bind.engine.name == "sqlite":
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")

    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("estimated_completion", sa.DateTime(), nullable=True)
        )
        batch_op.add_column(sa.Column("rq_job_id", sa.String(length=36), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_job_rq_job_id"), ["rq_job_id"], unique=True
        )

    if bind.engine.name == "sqlite":
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")
