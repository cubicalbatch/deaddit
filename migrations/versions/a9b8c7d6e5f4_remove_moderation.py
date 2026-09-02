"""Remove D4 moderation tables, soft-removal columns, and report metrics

Revision ID: a9b8c7d6e5f4
Revises: c1d5e8f3a7b2
Create Date: 2026-09-02 00:00:00.000000

Removes the moderation schema introduced by D4 and the reports counter from
``platform_daily``. Every drop is existence-guarded so the revision stays
re-runnable (the visit-profile migration tests stamp alembic_version back and
replay head revisions against an already-migrated schema). The post/comment
recreates cannot reflect the D2 expression-based ``ix_post_hot_expr`` (see the
same caveat in ``a7c3e9f5b1d8``), so both directions recreate it explicitly
with the byte-frozen fragment.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a9b8c7d6e5f4"
down_revision = "c1d5e8f3a7b2"
branch_labels = None
depends_on = None

# Byte-identical to the D2 index created in d2c4f8a16e90 (shared with
# deaddit.dynamics.ranking.HOT_SQL_FRAGMENT).
_POST_HOT_EXPR_DDL = (
    "CREATE INDEX IF NOT EXISTS ix_post_hot_expr ON post "
    "(((log10(max(abs(score),1))*sign(score) + "
    "(strftime('%s',created_at)-1134028003)/45000.0)))"
)


def _bind():
    return op.get_bind()


def _tables() -> set[str]:
    rows = _bind().exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )
    return {row[0] for row in rows}


def _columns(table: str) -> set[str]:
    rows = _bind().exec_driver_sql(f"PRAGMA table_info({table})")
    return {row[1] for row in rows}


def _has_index(table: str, name: str) -> bool:
    # sqlite_master directly: reflecting expression-based indexes is
    # unsupported and noisy.
    row = (
        _bind()
        .exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
        )
        .scalar()
    )
    return row is not None


def _drop_index_if_exists(table: str, name: str) -> None:
    # Earlier revisions batch-recreate post/comment and may already have
    # shed these indexes; dropping them first (when present) stops the
    # batch recreate below from replaying a reflected index onto a table
    # without the column.
    if _has_index(table, name):
        op.drop_index(op.f(name), table_name=table)


def _drop_columns_if_exist(table: str, columns: tuple[str, ...]) -> None:
    # A previously failed batch recreate can leave its temp table committed
    # (the instance DB carried one); sweep it or the CREATE fails.
    op.execute(f"DROP TABLE IF EXISTS _alembic_tmp_{table}")
    present = _columns(table) & set(columns)
    if not present:
        return
    with op.batch_alter_table(table) as batch_op:
        for column in columns:
            if column in present:
                batch_op.drop_column(column)


def _drop_table_if_exists(table: str) -> None:
    if table in _tables():
        op.drop_table(table)


def upgrade():
    # Moderation tables are pure children of user/post/comment: dropping
    # them first cannot violate any FK and clears the rows that would
    # otherwise trip the batch recreates below on a populated database.
    for table, indexes in (
        (
            "report",
            (
                "ix_report_created_at",
                "ix_report_comment_id",
                "ix_report_post_id",
                "ix_report_reporter",
            ),
        ),
        ("ban", ("ix_ban_lifted_at", "ix_ban_username")),
    ):
        if table in _tables():
            for index in indexes:
                if _has_index(table, index):
                    op.drop_index(op.f(index), table_name=table)
            op.drop_table(table)

    _drop_table_if_exists("subdeaddit_moderator")

    _drop_index_if_exists("post", "ix_post_removed")
    _drop_index_if_exists("comment", "ix_comment_removed")

    # The batch recreates DROP the live post/comment tables. With FK
    # enforcement on, SQLite's implicit DELETE on DROP TABLE rejects the
    # drop while child rows (comments, votes, images, websites) reference
    # them - and PRAGMA foreign_keys is a no-op inside a transaction, so
    # flip it in an autocommit block. The rename back to the original
    # table name leaves every child FK pointing where it did before.
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")

    _drop_columns_if_exist(
        "post", ("removed", "removed_by", "removal_reason", "removed_at")
    )
    op.execute(_POST_HOT_EXPR_DDL)

    _drop_columns_if_exist(
        "comment", ("removed", "removed_by", "removal_reason", "removed_at")
    )

    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=ON")

    if "platform_daily" in _tables() and "reports" in _columns("platform_daily"):
        with op.batch_alter_table("platform_daily") as batch_op:
            batch_op.drop_column("reports")


def downgrade():
    if "platform_daily" in _tables() and "reports" not in _columns("platform_daily"):
        with op.batch_alter_table("platform_daily") as batch_op:
            batch_op.add_column(
                sa.Column("reports", sa.Integer(), nullable=False, server_default="0")
            )

    if "removed" not in _columns("post"):
        with op.batch_alter_table("post") as batch_op:
            batch_op.add_column(sa.Column("removed", sa.Boolean(), nullable=True))
            batch_op.add_column(
                sa.Column("removed_by", sa.String(length=50), nullable=True)
            )
            batch_op.add_column(sa.Column("removal_reason", sa.Text(), nullable=True))
            batch_op.add_column(sa.Column("removed_at", sa.DateTime(), nullable=True))
            batch_op.create_index(op.f("ix_post_removed"), ["removed"], unique=False)
    op.execute(_POST_HOT_EXPR_DDL)

    if "removed" not in _columns("comment"):
        with op.batch_alter_table("comment") as batch_op:
            batch_op.add_column(sa.Column("removed", sa.Boolean(), nullable=True))
            batch_op.add_column(
                sa.Column("removed_by", sa.String(length=50), nullable=True)
            )
            batch_op.add_column(sa.Column("removal_reason", sa.Text(), nullable=True))
            batch_op.add_column(sa.Column("removed_at", sa.DateTime(), nullable=True))
            batch_op.create_index(op.f("ix_comment_removed"), ["removed"], unique=False)

    if "subdeaddit_moderator" not in _tables():
        op.create_table(
            "subdeaddit_moderator",
            sa.Column("subdeaddit_name", sa.String(length=50), nullable=False),
            sa.Column("username", sa.String(length=50), nullable=False),
            sa.ForeignKeyConstraint(["subdeaddit_name"], ["subdeaddit.name"]),
            sa.ForeignKeyConstraint(["username"], ["user.username"]),
            sa.PrimaryKeyConstraint("subdeaddit_name", "username"),
        )

    if "ban" not in _tables():
        op.create_table(
            "ban",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("username", sa.String(length=50), nullable=False),
            sa.Column("subdeaddit_name", sa.String(length=50), nullable=True),
            sa.Column("reason", sa.String(length=500), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("lifted_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["username"], ["user.username"]),
            sa.ForeignKeyConstraint(["subdeaddit_name"], ["subdeaddit.name"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_ban_username"), "ban", ["username"], unique=False)
        op.create_index(op.f("ix_ban_lifted_at"), "ban", ["lifted_at"], unique=False)

    if "report" not in _tables():
        op.create_table(
            "report",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("reporter", sa.String(length=50), nullable=False),
            sa.Column("post_id", sa.Integer(), nullable=True),
            sa.Column("comment_id", sa.Integer(), nullable=True),
            sa.Column("reason", sa.String(length=500), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("resolved_by", sa.String(length=50), nullable=True),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
            sa.Column("resolution_note", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["reporter"], ["user.username"]),
            sa.ForeignKeyConstraint(["post_id"], ["post.id"]),
            sa.ForeignKeyConstraint(["comment_id"], ["comment.id"]),
            sa.ForeignKeyConstraint(["resolved_by"], ["user.username"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            op.f("ix_report_reporter"), "report", ["reporter"], unique=False
        )
        op.create_index(op.f("ix_report_post_id"), "report", ["post_id"], unique=False)
        op.create_index(
            op.f("ix_report_comment_id"), "report", ["comment_id"], unique=False
        )
        op.create_index(
            op.f("ix_report_created_at"), "report", ["created_at"], unique=False
        )
