"""prompt versioning tables (Phase LLM-5)

prompt_template / prompt_template_version / prompt_pin /
prompt_render_audit, plus the one-shot seed of legacy
GenerationTemplate.parameters rows into immutable v1 rows.

Revision ID: b2d4f6a8c0e1
Revises: a9c1e5f7b3d2
Create Date: 2026-08-25 00:00:00.000000

"""

import json

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b2d4f6a8c0e1"
down_revision = "a9c1e5f7b3d2"
branch_labels = None
depends_on = None


def _seed_generation_templates() -> None:
    """One-shot migration of GenerationTemplate.parameters into v1 rows.

    Deterministic body derivation (mirrors
    ``deaddit.llm.prompts.seed_from_generation_templates``): a string
    ``parameters['prompt']`` becomes the body verbatim; anything else is
    canonical JSON. Idempotent per template name.
    """
    conn = op.get_bind()
    if "generation_template" not in sa.inspect(conn).get_table_names():
        return
    legacy = conn.execute(
        sa.text("SELECT id, name, description, parameters FROM generation_template")
    ).fetchall()
    for row in legacy:
        exists = conn.execute(
            sa.text("SELECT 1 FROM prompt_template WHERE name = :name"),
            {"name": row.name},
        ).first()
        if exists:
            continue
        params = (
            json.loads(row.parameters)
            if isinstance(row.parameters, str)
            else (row.parameters or {})
        )
        if isinstance(params, dict) and isinstance(params.get("prompt"), str):
            body = params["prompt"]
        else:
            body = json.dumps(params, sort_keys=True, separators=(",", ":"))
        result = conn.execute(
            sa.text(
                "INSERT INTO prompt_template (name, description, created_at) "
                "VALUES (:name, :description, CURRENT_TIMESTAMP)"
            ),
            {"name": row.name, "description": row.description},
        )
        conn.execute(
            sa.text(
                "INSERT INTO prompt_template_version "
                "(template_id, version, body, created_by, created_at) VALUES "
                "(:tid, 1, :body, 'migration:generation_template', CURRENT_TIMESTAMP)"
            ),
            {"tid": result.lastrowid, "body": body},
        )


def upgrade():
    op.create_table(
        "prompt_template",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "prompt_template_version",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "template_id",
            sa.Integer(),
            sa.ForeignKey("prompt_template.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["template_id"], ["prompt_template.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("template_id", "version", name="uq_prompt_version"),
    )
    op.create_index(
        "ix_prompt_template_version_template",
        "prompt_template_version",
        ["template_id"],
        unique=False,
    )
    op.create_table(
        "prompt_pin",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("target_kind", sa.String(length=20), nullable=False),
        sa.Column("target_key", sa.String(length=120), nullable=False),
        sa.Column(
            "template_id",
            sa.Integer(),
            sa.ForeignKey("prompt_template.id"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["template_id"], ["prompt_template.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("target_kind", "target_key", name="uq_prompt_pin_target"),
    )
    op.create_table(
        "prompt_render_audit",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column(
            "template_id",
            sa.Integer(),
            sa.ForeignKey("prompt_template.id"),
            nullable=False,
        ),
        sa.Column(
            "template_version_id",
            sa.Integer(),
            sa.ForeignKey("prompt_template_version.id"),
            nullable=False,
        ),
        sa.Column("subject_kind", sa.String(length=20), nullable=False),
        sa.Column("subject_key", sa.String(length=120), nullable=True),
        sa.Column("rendered_sha256", sa.String(length=64), nullable=False),
        sa.Column("variables_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["template_id"], ["prompt_template.id"]),
        sa.ForeignKeyConstraint(
            ["template_version_id"], ["prompt_template_version.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_prompt_render_audit_created_at",
        "prompt_render_audit",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_prompt_render_audit_version",
        "prompt_render_audit",
        ["template_version_id"],
        unique=False,
    )
    _seed_generation_templates()


def downgrade():
    op.drop_index("ix_prompt_render_audit_version", table_name="prompt_render_audit")
    op.drop_index("ix_prompt_render_audit_created_at", table_name="prompt_render_audit")
    op.drop_table("prompt_render_audit")
    op.drop_table("prompt_pin")
    op.drop_index(
        "ix_prompt_template_version_template", table_name="prompt_template_version"
    )
    op.drop_table("prompt_template_version")
    op.drop_table("prompt_template")
