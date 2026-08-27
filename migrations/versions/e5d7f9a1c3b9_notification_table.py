"""notification table

Revision ID: e5d7f9a1c3b9
Revises: d2c4f8a16e90
Create Date: 2026-08-25 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e5d7f9a1c3b9"
down_revision = "d2c4f8a16e90"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "notification",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("recipient", sa.String(length=50), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.String(length=50), nullable=True),
        sa.Column("post_id", sa.Integer(), nullable=True),
        sa.Column("comment_id", sa.Integer(), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["recipient"], ["user.username"]),
        sa.ForeignKeyConstraint(["actor"], ["user.username"]),
        sa.ForeignKeyConstraint(["post_id"], ["post.id"]),
        sa.ForeignKeyConstraint(["comment_id"], ["comment.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_notification_recipient"), "notification", ["recipient"], unique=False
    )
    op.create_index(
        op.f("ix_notification_post_id"), "notification", ["post_id"], unique=False
    )
    op.create_index(
        op.f("ix_notification_created_at"), "notification", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_notification_read_at"), "notification", ["read_at"], unique=False
    )


def downgrade():
    op.drop_index(op.f("ix_notification_read_at"), table_name="notification")
    op.drop_index(op.f("ix_notification_created_at"), table_name="notification")
    op.drop_index(op.f("ix_notification_post_id"), table_name="notification")
    op.drop_index(op.f("ix_notification_recipient"), table_name="notification")
    op.drop_table("notification")
