"""Persist validated agent visit profiles and migrate legacy prompt settings.

Revision ID: d4f9a2c7e1b6
Revises: a7c3e9f5b1d8
Create Date: 2026-08-29

The conversion of ``agent.system_prompt`` bodies into JSON documents is
intentionally irreversible: the exact old body is retained as
``system_template`` but the old pin/template identity is not reconstructed on
downgrade.  Downgrade therefore restores only the nullable audit column.
"""

from __future__ import annotations

import json
import math

import sqlalchemy as sa
from alembic import op

revision = "d4f9a2c7e1b6"
down_revision = "a7c3e9f5b1d8"
branch_labels = None
depends_on = None

_PROFILE_NAME = "agent.visit_profile"
_MIX_KEYS = (
    "AGENT_POST_INTENT_CHANCE",
    "AGENT_FORCED_IMAGE_CHANCE",
    "AGENT_FORCED_WEBSITE_CHANCE",
)
_DEFAULT_MIX = {"post": 0.30, "image": 0.0, "website": 0.0}

_DEFAULT_SYSTEM_TEMPLATE = (
    "You are an autonomous Deaddit agent. Read the visit instructions, "
    "use the available tools thoughtfully, and finish when your visit is done."
)


def _profile_body(system_template: str | None, mix: dict[str, float]) -> str:
    """Build the v1 document without importing application runtime modules."""
    if system_template is None:
        system_template = _DEFAULT_SYSTEM_TEMPLATE
    document = {
        "schema_version": 1,
        "system_template": system_template,
        "layouts": {
            "system": system_template,
            "lurker": "Browse quietly and do not create posts or comments.",
            "browse": "Read carefully, use relevant tools, and finish when done.",
            "post": "Create one useful, genuine post when the visit calls for it.",
        },
        "behavior_blocks": [
            {
                "id": "general.genuine",
                "text": "Be genuine, use the available tools, and finish your visit when done.",
            }
        ],
        "intent_mix": {key: mix[key] for key in ("image", "post", "website")},
        "length_catalog": {
            "comment": [
                {
                    "id": "comment.short",
                    "text": "Keep the response concise and useful.",
                    "weight": 1,
                }
            ],
            "media_post": [
                {
                    "id": "media_post.caption",
                    "text": "Add only a brief, relevant caption when useful.",
                    "weight": 1,
                }
            ],
            "text_post": [
                {
                    "id": "text_post.short",
                    "text": "Write one short, complete paragraph.",
                    "weight": 1,
                }
            ],
        },
        "direction_catalog": {
            "comment": [
                {
                    "id": "comment.honest_reaction",
                    "text": "Give a brief, honest reaction.",
                    "weight": 1,
                }
            ],
            "post": [
                {
                    "id": "post.genuine_question",
                    "text": "Ask a genuine question worth answering.",
                    "weight": 1,
                }
            ],
        },
        "sample_count": 3,
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def _profile_template(conn) -> int:
    row = conn.execute(
        sa.text("SELECT id FROM prompt_template WHERE name = :name"),
        {"name": _PROFILE_NAME},
    ).first()
    if row:
        return row[0]
    result = conn.execute(
        sa.text(
            "INSERT INTO prompt_template (name, description, created_at) "
            "VALUES (:name, :description, CURRENT_TIMESTAMP)"
        ),
        {
            "name": _PROFILE_NAME,
            "description": "Validated immutable agent visit profile",
        },
    )
    template_id = result.lastrowid
    conn.execute(
        sa.text(
            "INSERT INTO prompt_template_version "
            "(template_id, version, body, created_by, created_at) "
            "VALUES (:template_id, 1, :body, 'migration:visit_profile', CURRENT_TIMESTAMP)"
        ),
        {
            "template_id": template_id,
            "body": _profile_body(None, _DEFAULT_MIX),
        },
    )
    return template_id


def _next_profile_version(conn, template_id: int) -> int:
    value = conn.execute(
        sa.text(
            "SELECT COALESCE(MAX(version), 0) FROM prompt_template_version "
            "WHERE template_id = :template_id"
        ),
        {"template_id": template_id},
    ).scalar_one()
    return int(value) + 1


def _read_mix(conn) -> tuple[dict[str, float], bool]:
    rows = conn.execute(
        sa.text("SELECT key, value FROM setting WHERE key IN (:post, :image, :website)"),
        {
            "post": _MIX_KEYS[0],
            "image": _MIX_KEYS[1],
            "website": _MIX_KEYS[2],
        },
    ).fetchall()
    mix = dict(_DEFAULT_MIX)
    for row in rows:
        raw = row[1]
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid {row[0]} value in setting table") from exc
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise RuntimeError(f"invalid {row[0]} value in setting table")
        key = {
            _MIX_KEYS[0]: "post",
            _MIX_KEYS[1]: "image",
            _MIX_KEYS[2]: "website",
        }[row[0]]
        mix[key] = value
    if mix["image"] + mix["website"] > 1.0 + 1e-9:
        raise RuntimeError("forced image and website settings exceed 100 percent")
    return mix, bool(rows)


def upgrade():
    with op.batch_alter_table("agent_run") as batch_op:
        batch_op.add_column(sa.Column("prompt_metadata", sa.JSON(), nullable=True))

    conn = op.get_bind()
    profile_template_id = _profile_template(conn)

    # Convert only agent/cohort/global pins.  Other consumers may still use a
    # generic system template and must remain untouched.
    legacy = conn.execute(
        sa.text(
            "SELECT pp.id, pp.target_kind, pp.target_key, pp.template_id, "
            "pp.version_number, ptv.body "
            "FROM prompt_pin pp "
            "JOIN prompt_template pt ON pt.id = pp.template_id "
            "JOIN prompt_template_version ptv ON ptv.template_id = pp.template_id "
            "AND ptv.version = pp.version_number "
            "WHERE pt.name = :name AND pp.target_kind IN ('agent', 'cohort', 'global')"
        ),
        {"name": "agent.system_prompt"},
    ).fetchall()
    converted: dict[tuple[int, int], int] = {}
    global_system_template: str | None = None
    for row in legacy:
        key = (row[3], row[4])
        if row[1] == "global":
            global_system_template = row[5]
        profile_version = converted.get(key)
        if profile_version is None:
            profile_version = _next_profile_version(conn, profile_template_id)
            conn.execute(
                sa.text(
                    "INSERT INTO prompt_template_version "
                    "(template_id, version, body, created_by, created_at) "
                    "VALUES (:template_id, :version, :body, "
                    "'migration:agent.system_prompt', CURRENT_TIMESTAMP)"
                ),
                {
                    "template_id": profile_template_id,
                    "version": profile_version,
                    "body": _profile_body(row[5], _DEFAULT_MIX),
                },
            )
            converted[key] = profile_version
        conn.execute(
            sa.text(
                "UPDATE prompt_pin SET template_id = :template_id, "
                "version_number = :version, "
                "target_key = CASE WHEN target_kind = 'global' "
                "THEN :profile_name ELSE target_key END WHERE id = :id"
            ),
            {
                "template_id": profile_template_id,
                "version": profile_version,
                "profile_name": _PROFILE_NAME,
                "id": row[0],
            },
        )

    mix, had_settings = _read_mix(conn)
    if had_settings:
        profile_version = _next_profile_version(conn, profile_template_id)
        conn.execute(
            sa.text(
                "INSERT INTO prompt_template_version "
                "(template_id, version, body, created_by, created_at) "
                "VALUES (:template_id, :version, :body, "
                "'migration:agent_intent_mix', CURRENT_TIMESTAMP)"
            ),
            {
                "template_id": profile_template_id,
                "version": profile_version,
                "body": _profile_body(global_system_template, mix),
            },
        )
        existing = conn.execute(
            sa.text(
                "SELECT id FROM prompt_pin WHERE target_kind = 'global' "
                "AND target_key = :target_key"
            ),
            {"target_key": _PROFILE_NAME},
        ).first()
        if existing:
            conn.execute(
                sa.text(
                    "UPDATE prompt_pin SET template_id = :template_id, "
                    "version_number = :version, updated_at = CURRENT_TIMESTAMP WHERE id = :id"
                ),
                {"template_id": profile_template_id, "version": profile_version, "id": existing[0]},
            )
        else:
            conn.execute(
                sa.text(
                    "INSERT INTO prompt_pin "
                    "(target_kind, target_key, template_id, version_number, updated_at) "
                    "VALUES ('global', :target_key, :template_id, :version, CURRENT_TIMESTAMP)"
                ),
                {"target_key": _PROFILE_NAME, "template_id": profile_template_id, "version": profile_version},
            )
        conn.execute(
            sa.text("DELETE FROM setting WHERE key IN (:post, :image, :website)"),
            {
                "post": _MIX_KEYS[0],
                "image": _MIX_KEYS[1],
                "website": _MIX_KEYS[2],
            },
        )


def downgrade():
    # Profile conversion cannot safely recreate old template identities/pins;
    # system_template retains their bytes for manual recovery.  Restore only
    # the schema column introduced by this revision.
    with op.batch_alter_table("agent_run") as batch_op:
        batch_op.drop_column("prompt_metadata")
