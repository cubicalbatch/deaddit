"""post/comment llm_model provenance column

Revision ID: e8b1f4c7a2d9
Revises: b4d8a2c6f0e1
Create Date: 2026-08-27 00:00:00.000000

``post.model``/``comment.model`` store provenance ('agent:<username>' or
'seed'), not the LLM. This adds ``llm_model`` — the model name the
generating run resolved — and backfills it for existing agent content by
joining ``tool_call.result`` (carries post_id/comment_id of successful
create_post/create_image_post/create_comment calls) to any ``agent_turn``
of the same run (a run resolves one endpoint+model for all its turns).
Seed/legacy rows keep NULL, rendered as 'not recorded'.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e8b1f4c7a2d9"
down_revision = "b4d8a2c6f0e1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("post", sa.Column("llm_model", sa.String(length=100)))
    op.add_column("comment", sa.Column("llm_model", sa.String(length=100)))

    op.execute(
        "UPDATE post SET llm_model = ("
        "  SELECT agent_turn.model FROM tool_call"
        "  JOIN agent_turn ON agent_turn.run_id = tool_call.run_id"
        "  WHERE tool_call.name IN ('create_post', 'create_image_post')"
        "    AND tool_call.ok = 1"
        "    AND json_extract(tool_call.result, '$.post_id') = post.id"
        "    AND agent_turn.model IS NOT NULL"
        "  LIMIT 1)"
        "WHERE llm_model IS NULL AND model LIKE 'agent:%'"
    )
    op.execute(
        "UPDATE comment SET llm_model = ("
        "  SELECT agent_turn.model FROM tool_call"
        "  JOIN agent_turn ON agent_turn.run_id = tool_call.run_id"
        "  WHERE tool_call.name = 'create_comment'"
        "    AND tool_call.ok = 1"
        "    AND json_extract(tool_call.result, '$.comment_id') = comment.id"
        "    AND agent_turn.model IS NOT NULL"
        "  LIMIT 1)"
        "WHERE llm_model IS NULL AND model LIKE 'agent:%'"
    )


def downgrade():
    op.drop_column("comment", "llm_model")
    op.drop_column("post", "llm_model")
