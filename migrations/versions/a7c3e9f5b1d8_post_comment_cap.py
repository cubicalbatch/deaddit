"""post.comment_cap thread-realism column

Revision ID: a7c3e9f5b1d8
Revises: e9a3b1c4d7f2
Create Date: 2026-08-29

Adds ``post.comment_cap``: each post's frozen total-comment ceiling,
sampled at creation by the content service
(``thread_comment_cap_min``/``thread_comment_cap_max`` Settings,
defaults 20/39) and enforced by the agent ``create_comment`` tool only -
never by the service, so seeding and other non-agent paths keep control
of their own volume.

Existing posts are backfilled deterministically with the same default
20-39 spread (``20 + (id * 31) % 20``). Posts already over their
backfilled cap simply stop accepting new agent comments - no trimming
of existing threads. NULL (only possible on hand-inserted rows) reads
as uncapped everywhere.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a7c3e9f5b1d8"
down_revision = "e9a3b1c4d7f2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("post", sa.Column("comment_cap", sa.Integer()))
    op.execute(
        "UPDATE post SET comment_cap = 20 + (id * 31) % 20 WHERE comment_cap IS NULL"
    )


def downgrade():
    # Native DROP COLUMN (SQLite >= 3.35): a batch_alter_table recreate
    # would rebuild `post` from reflected metadata and silently lose the
    # expression-based ix_post_hot_expr index, which SQLAlchemy cannot
    # reflect. comment_cap carries no index/FK, so the native ALTER is
    # safe and leaves every other index untouched.
    op.drop_column("post", "comment_cap")
