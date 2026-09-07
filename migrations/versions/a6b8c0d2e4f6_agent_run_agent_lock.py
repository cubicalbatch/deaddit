"""Prevent concurrent runs for one agent.

Revision ID: a6b8c0d2e4f6
Revises: a9b8c7d6e5f4
Create Date: 2026-09-07

Existing databases may contain multiple running rows for one agent because the
old constraint only covered persona usernames. The newest row remains active;
older duplicates are retained as interrupted history before the new index is
created.
"""

import sqlalchemy as sa
from alembic import op

revision = "a6b8c0d2e4f6"
down_revision = "a9b8c7d6e5f4"
branch_labels = None
depends_on = None


def _has_index(name: str) -> bool:
    row = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
        )
        .scalar()
    )
    return row is not None




def upgrade():
    bind = op.get_bind()
    duplicate_ids = bind.execute(
        sa.text(
            """
            SELECT older.id
            FROM agent_run AS older
            JOIN agent_run AS newer
              ON newer.agent_id = older.agent_id
             AND newer.status = 'running'
             AND (
                    newer.started_at > older.started_at
                 OR (newer.started_at = older.started_at AND newer.id > older.id)
             )
            WHERE older.status = 'running'
            """
        )
    ).scalars().all()
    for run_id in duplicate_ids:
        bind.execute(
            sa.text(
                "UPDATE agent_run SET status = 'interrupted', "
                "finished_at = CURRENT_TIMESTAMP, "
                "error_message = COALESCE("
                "error_message, "
                "'Migration interrupted duplicate running agent run'"
                ") WHERE id = :run_id"
            ),
            {"run_id": run_id},
        )
    if not _has_index("uq_agent_run_running_agent"):
        op.create_index(
            "uq_agent_run_running_agent",
            "agent_run",
            ["agent_id"],
            unique=True,
            sqlite_where=sa.text("status = 'running'"),
        )


def downgrade():
    op.drop_index("uq_agent_run_running_agent", table_name="agent_run")
