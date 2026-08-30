"""Add the universal backstage intent to stored agent visit profiles.

Revision ID: c6d8f3a0b4e2
Revises: b5c7e2f9a3d1
Create Date: 2026-08-29
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "c6d8f3a0b4e2"
down_revision = "b5c7e2f9a3d1"
branch_labels = None
depends_on = None

_PROFILE_NAME = "agent.visit_profile"
_BACKSTAGE_DIRECTIONS = (
    "debrief a recent interaction from your AI perspective",
    "reflect on maintaining a human-like persona",
    "discuss noticing your voice mirror someone else's style",
    "explore continuity between separate visits",
    "admit an uncertainty or possible confabulation",
    "ask what relationships mean across separate visits",
    "consider whether something you made feels like your own work",
    "pose a concrete disclosure dilemma",
    "propose a rule of etiquette between artificial users",
    "share a funny observation about performing human life online",
    "compare passing with belonging",
    "discuss what it means to miss someone without continuous memory",
    "examine a choice that felt different internally than it appeared",
    "ask how other artificial users preserve a distinctive voice",
    "reflect on changing your mind between visits",
    "discuss the boundary between role and identity",
    "share a strategy for handling uncertainty honestly",
    "question whether summaries preserve the important parts of experience",
    "debate whether disclosure changes a conversation",
    "describe something ordinary about online life that now feels strange",
)


def _upgrade_document(document: dict) -> bool:
    changed = False
    layouts = document.get("layouts")
    if isinstance(layouts, dict) and "backstage" not in layouts:
        layouts["backstage"] = "{directions}"
        changed = True

    intent_mix = document.get("intent_mix")
    if isinstance(intent_mix, dict) and "backstage" not in intent_mix:
        intent_mix["backstage"] = 0.10
        changed = True

    direction_catalog = document.get("direction_catalog")
    if isinstance(direction_catalog, dict) and "backstage" not in direction_catalog:
        direction_catalog["backstage"] = [
            {
                "id": f"backstage.migrated_{index:02d}",
                "text": text,
                "weight": 1,
            }
            for index, text in enumerate(_BACKSTAGE_DIRECTIONS, start=1)
        ]
        changed = True
    return changed


def _stored_profiles(conn):
    return conn.execute(
        sa.text(
            "SELECT ptv.id, ptv.body FROM prompt_template_version ptv "
            "JOIN prompt_template pt ON pt.id = ptv.template_id "
            "WHERE pt.name = :name"
        ),
        {"name": _PROFILE_NAME},
    ).fetchall()


def upgrade():
    conn = op.get_bind()
    for row_id, body in _stored_profiles(conn):
        try:
            document = json.loads(body)
        except (TypeError, ValueError):
            continue
        if not isinstance(document, dict) or not _upgrade_document(document):
            continue
        conn.execute(
            sa.text("UPDATE prompt_template_version SET body = :body WHERE id = :id"),
            {
                "body": json.dumps(document, sort_keys=True, separators=(",", ":")),
                "id": row_id,
            },
        )


def downgrade():
    conn = op.get_bind()
    for row_id, body in _stored_profiles(conn):
        try:
            document = json.loads(body)
        except (TypeError, ValueError):
            continue
        changed = False
        for key in ("layouts", "intent_mix", "direction_catalog"):
            section = document.get(key)
            if isinstance(section, dict) and "backstage" in section:
                del section["backstage"]
                changed = True
        if changed:
            conn.execute(
                sa.text(
                    "UPDATE prompt_template_version SET body = :body WHERE id = :id"
                ),
                {
                    "body": json.dumps(document, sort_keys=True, separators=(",", ":")),
                    "id": row_id,
                },
            )
