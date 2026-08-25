"""platform dynamics votes & karma

Revision ID: d1f0a93b7c25
Revises: b7e4c9a02f15
Create Date: 2026-08-25 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d1f0a93b7c25"
down_revision = "b7e4c9a02f15"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "vote",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("voter", sa.String(length=50), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=True),
        sa.Column("comment_id", sa.Integer(), nullable=True),
        sa.Column("value", sa.SmallInteger(), nullable=False),
        sa.Column(
            "source", sa.String(length=16), server_default="agent", nullable=False
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["comment_id"], ["comment.id"]),
        sa.ForeignKeyConstraint(["post_id"], ["post.id"]),
        sa.ForeignKeyConstraint(["voter"], ["user.username"]),
        sa.CheckConstraint("value IN (1, -1)"),
        sa.CheckConstraint("(post_id IS NULL) != (comment_id IS NULL)"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("voter", "post_id", name="uq_vote_post"),
        sa.UniqueConstraint("voter", "comment_id", name="uq_vote_comment"),
    )
    op.create_index(op.f("ix_vote_voter"), "vote", ["voter"], unique=False)
    op.create_index(op.f("ix_vote_post_id"), "vote", ["post_id"], unique=False)
    op.create_index(op.f("ix_vote_comment_id"), "vote", ["comment_id"], unique=False)
    op.create_index(op.f("ix_vote_source"), "vote", ["source"], unique=False)
    op.create_index(op.f("ix_vote_created_at"), "vote", ["created_at"], unique=False)

    op.add_column(
        "post",
        sa.Column("score", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "post",
        sa.Column(
            "vote_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "comment",
        sa.Column("score", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "comment",
        sa.Column(
            "vote_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "user",
        sa.Column(
            "post_karma", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "user",
        sa.Column(
            "comment_karma", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )

    op.execute(
        "INSERT INTO setting (key, value, description, created_at, updated_at) "
        "VALUES ('allow_downvotes', 'true', 'Global downvote toggle (owner decision 6): "
        "when not true, cast_vote rejects value=-1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    )


def downgrade():
    op.execute("DELETE FROM setting WHERE key='allow_downvotes'")

    op.drop_column("user", "comment_karma")
    op.drop_column("user", "post_karma")
    op.drop_column("comment", "vote_count")
    op.drop_column("comment", "score")
    op.drop_column("post", "vote_count")
    op.drop_column("post", "score")

    op.drop_index(op.f("ix_vote_created_at"), table_name="vote")
    op.drop_index(op.f("ix_vote_source"), table_name="vote")
    op.drop_index(op.f("ix_vote_comment_id"), table_name="vote")
    op.drop_index(op.f("ix_vote_post_id"), table_name="vote")
    op.drop_index(op.f("ix_vote_voter"), table_name="vote")
    op.drop_table("vote")
