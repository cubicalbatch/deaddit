"""D2 feed ranking indexes

Revision ID: d2c4f8a16e90
Revises: d1f0a93b7c25
Create Date: 2026-08-25 00:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "d2c4f8a16e90"
down_revision = "d1f0a93b7c25"
branch_labels = None
depends_on = None

_HOT_SQL_FRAGMENT = (
    "(log10(max(abs(score),1))*sign(score)"
    " + (strftime('%s',created_at)-1134028003)/45000.0)"
)


def upgrade():
    # Plain score index serves the 'top' sort; the expression index serves
    # 'hot'. The stored expression text must be byte-identical to the
    # ORDER BY fragment so SQLite's planner recognizes and uses it.
    op.create_index(op.f("ix_post_score"), "post", ["score"], unique=False)
    op.execute(f"CREATE INDEX ix_post_hot_expr ON post (({_HOT_SQL_FRAGMENT}))")


def downgrade():
    op.drop_index("ix_post_hot_expr", table_name="post")
    op.drop_index(op.f("ix_post_score"), table_name="post")
