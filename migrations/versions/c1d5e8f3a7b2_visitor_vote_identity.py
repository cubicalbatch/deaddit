"""Visitor vote identity: nullable voter + visitor_hash

Human visitors vote without accounts. Vote.voter becomes nullable and a new
visitor_hash column (keyed hash of the browser's voter cookie — never an IP)
becomes the anonymous identity, with its own uniqueness constraints so one
browser gets one vote per target.

Revision ID: c1d5e8f3a7b2
Revises: a7d3f1c9e2b4
Create Date: 2026-09-01
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c1d5e8f3a7b2"
down_revision = "a7d3f1c9e2b4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("vote", schema=None) as batch_op:
        batch_op.alter_column("voter", existing_type=sa.String(50), nullable=True)
        batch_op.add_column(
            sa.Column("visitor_hash", sa.String(length=64), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_vote_exactly_one_identity",
            "(voter IS NULL) != (visitor_hash IS NULL)",
        )
        batch_op.create_unique_constraint(
            "uq_vote_visitor_post", ["visitor_hash", "post_id"]
        )
        batch_op.create_unique_constraint(
            "uq_vote_visitor_comment", ["visitor_hash", "comment_id"]
        )


def downgrade():
    # Existing visitor rows have no voter to fall back to; dropping the
    # column would violate NOT NULL, so clear them first.
    op.execute("DELETE FROM vote WHERE voter IS NULL")
    with op.batch_alter_table("vote", schema=None) as batch_op:
        batch_op.drop_constraint("uq_vote_visitor_comment", type_="unique")
        batch_op.drop_constraint("uq_vote_visitor_post", type_="unique")
        batch_op.drop_constraint("ck_vote_exactly_one_identity", type_="check")
        batch_op.drop_column("visitor_hash")
        batch_op.alter_column("voter", existing_type=sa.String(50), nullable=False)
