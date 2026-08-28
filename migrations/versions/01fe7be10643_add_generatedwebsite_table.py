"""add GeneratedWebsite table

Revision ID: 01fe7be10643
Revises: f4a8c2d6b901
Create Date: 2026-08-27 20:37:21.061176

Adds the ``generated_website`` table backing the ``create_website`` agent
tool (see ``aidocs/CREATE_WEBSITE_TOOL_PLAN.md``, "Data model and
migration"). Purely additive: no existing table or column changes, no
backfill (existing posts have no generated websites).

Hand-trimmed after autogenerate: the raw diff also proposed unrelated
changes picked up by SQLite reflection drift against the current models
(FK re-detection on ``comment.removed_by``/``post.removed_by``, a
``job.type`` column type re-declaration, nullability tweaks on
``llm_usage``/``model_price``, and index renames on
``prompt_render_audit``/``prompt_template_version``). None of that belongs
to this revision; it is pre-existing drift unrelated to the
``GeneratedWebsite`` model and is intentionally left out here so this
migration's upgrade/downgrade only ever touches ``generated_website``.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "01fe7be10643"
down_revision = "f4a8c2d6b901"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "generated_website",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("public_path", sa.String(length=400), nullable=False),
        sa.Column("storage_path", sa.String(length=300), nullable=False),
        sa.Column("hostname", sa.String(length=253), nullable=False),
        sa.Column("page_name", sa.String(length=160), nullable=False),
        sa.Column("source_description", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("creator_username_snapshot", sa.String(length=50), nullable=False),
        sa.Column("agent_run_id", sa.Integer(), nullable=True),
        sa.Column("api_url_snapshot", sa.String(length=255), nullable=False),
        sa.Column("model_snapshot", sa.String(length=120), nullable=False),
        sa.Column("request_id", sa.String(length=32), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("finish_reason", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agent.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["agent_run_id"], ["agent_run.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["post_id"], ["post.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_path"),
    )
    with op.batch_alter_table("generated_website", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_generated_website_agent_id"), ["agent_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_generated_website_created_at"),
            ["created_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_generated_website_post_id"), ["post_id"], unique=True
        )
        batch_op.create_index(
            batch_op.f("ix_generated_website_public_path"),
            ["public_path"],
            unique=True,
        )


def downgrade():
    with op.batch_alter_table("generated_website", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_generated_website_public_path"))
        batch_op.drop_index(batch_op.f("ix_generated_website_post_id"))
        batch_op.drop_index(batch_op.f("ix_generated_website_created_at"))
        batch_op.drop_index(batch_op.f("ix_generated_website_agent_id"))

    op.drop_table("generated_website")
