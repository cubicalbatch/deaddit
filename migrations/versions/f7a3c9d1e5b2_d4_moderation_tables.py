"""D4 moderation tables and soft-removal columns

Revision ID: f7a3c9d1e5b2
Revises: e5d7f9a1c3b9
Create Date: 2026-08-25 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f7a3c9d1e5b2"
down_revision = "e5d7f9a1c3b9"
branch_labels = None
depends_on = None


def upgrade():
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
    op.create_index(op.f("ix_report_reporter"), "report", ["reporter"], unique=False)
    op.create_index(op.f("ix_report_post_id"), "report", ["post_id"], unique=False)
    op.create_index(op.f("ix_report_comment_id"), "report", ["comment_id"], unique=False)
    op.create_index(op.f("ix_report_created_at"), "report", ["created_at"], unique=False)

    op.create_table(
        "subdeaddit_moderator",
        sa.Column("subdeaddit_name", sa.String(length=50), nullable=False),
        sa.Column("username", sa.String(length=50), nullable=False),
        sa.ForeignKeyConstraint(["subdeaddit_name"], ["subdeaddit.name"]),
        sa.ForeignKeyConstraint(["username"], ["user.username"]),
        sa.PrimaryKeyConstraint("subdeaddit_name", "username"),
    )

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

    # Soft removal (Phase D4): plain ADD COLUMN works on SQLite.
    for table in ("post", "comment"):
        op.add_column(table, sa.Column("removed", sa.Boolean(), nullable=True))
        op.add_column(
            table,
            sa.Column("removed_by", sa.String(length=50), nullable=True),
        )
        op.add_column(table, sa.Column("removal_reason", sa.Text(), nullable=True))
        op.add_column(table, sa.Column("removed_at", sa.DateTime(), nullable=True))
        op.create_index(op.f(f"ix_{table}_removed"), table, ["removed"], unique=False)


def downgrade():
    for table in ("comment", "post"):
        op.drop_index(op.f(f"ix_{table}_removed"), table_name=table)
        op.drop_column(table, "removed_at")
        op.drop_column(table, "removal_reason")
        op.drop_column(table, "removed_by")
        op.drop_column(table, "removed")

    op.drop_index(op.f("ix_ban_lifted_at"), table_name="ban")
    op.drop_index(op.f("ix_ban_username"), table_name="ban")
    op.drop_table("ban")

    op.drop_table("subdeaddit_moderator")

    op.drop_index(op.f("ix_report_created_at"), table_name="report")
    op.drop_index(op.f("ix_report_comment_id"), table_name="report")
    op.drop_index(op.f("ix_report_post_id"), table_name="report")
    op.drop_index(op.f("ix_report_reporter"), table_name="report")
    op.drop_table("report")
