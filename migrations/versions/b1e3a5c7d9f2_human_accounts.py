"""Human accounts: password_hash column and lower(username) unique index.

Revision ID: b1e3a5c7d9f2
Revises: a6b8c0d2e4f6
Create Date: 2026-09-14

Adds nullable User.password_hash (VARCHAR(255)) and a unique case-insensitive index
on lower(username). Checks for pre-existing case-folded duplicate usernames before
creating the index.
"""

import sqlalchemy as sa
from alembic import op

revision = "b1e3a5c7d9f2"
down_revision = "a6b8c0d2e4f6"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    conn = op.get_bind()
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


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
    # 1. Preflight check for case-insensitive duplicate usernames
    duplicates = bind.execute(
        sa.text(
            "SELECT lower(username) FROM user GROUP BY lower(username) HAVING count(*) > 1"
        )
    ).scalars().all()
    if duplicates:
        raise RuntimeError(
            f"Case-folded duplicate usernames found in 'user' table: {duplicates}. "
            "Case-folded duplicate usernames must be resolved before migration."
        )

    # 2. Add password_hash column if not exists
    if not _has_column("user", "password_hash"):
        if bind.engine.name == "sqlite":
            bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
        with op.batch_alter_table("user", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("password_hash", sa.String(length=255), nullable=True)
            )
        if bind.engine.name == "sqlite":
            bind.exec_driver_sql("PRAGMA foreign_keys=ON")

    # 3. Add uq_user_username_lower index if not exists
    if not _has_index("uq_user_username_lower"):
        op.create_index(
            "uq_user_username_lower",
            "user",
            [sa.text("lower(username)")],
            unique=True,
        )


def downgrade():
    bind = op.get_bind()
    if _has_index("uq_user_username_lower"):
        op.drop_index("uq_user_username_lower", table_name="user")

    if _has_column("user", "password_hash"):
        if bind.engine.name == "sqlite":
            bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
        with op.batch_alter_table("user", schema=None) as batch_op:
            batch_op.drop_column("password_hash")
        if bind.engine.name == "sqlite":
            bind.exec_driver_sql("PRAGMA foreign_keys=ON")

