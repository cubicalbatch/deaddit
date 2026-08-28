"""user is_troll column

Revision ID: c4b9e2f7a1d3
Revises: c5e7a9b1d3f6
Create Date: 2026-08-28

Troll mode: a persona-level behavioral flag. Existing users default to
False; new persona generation may set it via a chance roll or explicit
override.

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c4b9e2f7a1d3"
down_revision = "c5e7a9b1d3f6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "user",
        sa.Column(
            "is_troll",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade():
    op.drop_column("user", "is_troll")
