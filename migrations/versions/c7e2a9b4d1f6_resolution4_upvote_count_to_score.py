"""Resolution 4: collapse ``upvote_count`` into the single ``score`` column

Revision ID: c7e2a9b4d1f6
Revises: b2d4f6a8c0e1
Create Date: 2026-08-25 00:00:00.000000

The displayed value before this revision was
``CASE WHEN vote_count > 0 THEN score ELSE COALESCE(upvote_count, 0) END``.
After it there is ONE column, ``score``, holding exactly that displayed
value; ``upvote_count`` is gone. Values are preserved exactly on upgrade,
including Resolution 1's 48 capacity-infeasible items whose fabricated
numbers ride over as ``score`` with ``vote_count = 0``. Downgrade restores
display truth (the old CASE collapses onto ``score``) but not pre-D1
provenance: it cannot distinguish fabricated numbers from vote-backed ones,
so it simply restores ``upvote_count = score`` everywhere.

SQLite (>= 3.35) supports ALTER TABLE DROP COLUMN natively, so no batch
table rebuild is needed — that would trip FOREIGN KEY enforcement and drop
the D2 expression index.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c7e2a9b4d1f6"
down_revision = "b2d4f6a8c0e1"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("post", "comment"):
        op.execute(
            f"UPDATE {table} SET score = CASE WHEN vote_count > 0 THEN score "
            f"ELSE COALESCE(upvote_count, 0) END"
        )

    # The baseline schema indexes comment.upvote_count; drop it before the
    # column itself goes away.
    op.drop_index(op.f("ix_comment_upvote_count"), table_name="comment")

    op.drop_column("post", "upvote_count")
    op.drop_column("comment", "upvote_count")

    # Mirror the new model metadata: Comment.score carries an index (the
    # Post.score index, ix_post_score, already exists).
    op.create_index(op.f("ix_comment_score"), "comment", ["score"], unique=False)


def downgrade():
    op.drop_index(op.f("ix_comment_score"), table_name="comment")

    for table in ("post", "comment"):
        op.add_column(table, sa.Column("upvote_count", sa.Integer(), nullable=True))
        op.execute(f"UPDATE {table} SET upvote_count = score")

    op.create_index(
        op.f("ix_comment_upvote_count"), "comment", ["upvote_count"], unique=False
    )
