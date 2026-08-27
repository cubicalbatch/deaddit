"""UX-5 job log table

Revision ID: a9c1e5f7b3d2
Revises: b8e2f4a6c9d1
Create Date: 2026-08-25 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a9c1e5f7b3d2"
down_revision = "b8e2f4a6c9d1"
branch_labels = None
depends_on = None


def upgrade():
    # Phase UX-5: broker-free streamed job logs. Worker writes rows, web
    # tailer streams them to sockets / the HTTP fallback endpoint.
    op.create_table(
        "job_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_log_job_id", "job_log", ["job_id"])
    op.create_index("ix_job_log_job_seq", "job_log", ["job_id", "seq"])


def downgrade():
    op.drop_index("ix_job_log_job_seq", table_name="job_log")
    op.drop_index("ix_job_log_job_id", table_name="job_log")
    op.drop_table("job_log")
