"""Roll out the canonical visit-profile comment guidance immutably.

Revision ID: f4c8e2a6b0d1
Revises: e7f1a3b5c9d2
Create Date: 2026-08-30

Only profiles that are currently pinned are migrated.  Existing versions are
never edited: each changed source gets one new immutable version and all pins
for that source are moved together.
"""

from __future__ import annotations

import json
import re

import sqlalchemy as sa
from alembic import op

revision = "f4c8e2a6b0d1"
down_revision = "e7f1a3b5c9d2"
branch_labels = None
depends_on = None

_PROFILE_NAME = "agent.visit_profile"
_MIGRATION_MARKER = "migration:visit_profile_v2"
_SOURCE_MARKER = re.compile(rf"^{re.escape(_MIGRATION_MARKER)}:source_version=(\d+)$")
_LEGACY_COMMENT_LENGTH = {
    "id": "comment.short",
    "text": "Keep the response concise and useful.",
    "weight": 100,
}
_LEGACY_COMMENT_DIRECTIONS = [
    {
        "id": "comment.honest_reaction",
        "text": "Give a brief, honest reaction.",
        "weight": 1,
    },
    {
        "id": "comment.relevant_fact",
        "text": "Add one relevant fact or missing context.",
        "weight": 1,
    },
    {
        "id": "comment.follow_up_question",
        "text": "Ask a genuine follow-up question.",
        "weight": 1,
    },
]

# Keep this catalog local to the migration.  Migrations must remain runnable
# when application modules (and their evolving defaults) are unavailable.
_CANONICAL_COMMENT_LENGTH = [
    {
        "id": "comment.snippet",
        "text": (
            "Length target for this comment: no more than one sentence and no more "
            "than 20 words. State the point directly; do not add setup, a conclusion, or padding."
        ),
        "weight": 35,
    },
    {
        "id": "comment.short",
        "text": (
            "Length target for this comment: exactly 2 or 3 sentences and 20-60 words. "
            "Make every sentence useful; do not add setup, a conclusion, or padding."
        ),
        "weight": 50,
    },
    {
        "id": "comment.medium",
        "text": (
            "Length target for this comment: one compact paragraph of 60-120 words. "
            "Use only relevant detail; do not add setup, a conclusion, or padding."
        ),
        "weight": 12,
    },
    {
        "id": "comment.long",
        "text": (
            "Length target for this comment: 2 or 3 short paragraphs of 120-250 words. "
            "Make the extra detail earn its space; do not add setup, a conclusion, or padding."
        ),
        "weight": 3,
    },
]

_CANONICAL_COMMENT_DIRECTIONS = [
    {
        "id": "comment.honest_reaction",
        "text": "give a brief, honest reaction",
        "weight": 1,
    },
    {
        "id": "comment.relevant_fact",
        "text": "add a relevant fact or missing context",
        "weight": 1,
    },
    {
        "id": "comment.related_anecdote",
        "text": "share a related personal anecdote",
        "weight": 1,
    },
    {
        "id": "comment.answer_or_advice",
        "text": "answer a question or offer practical advice",
        "weight": 1,
    },
    {
        "id": "comment.follow_up_question",
        "text": "ask a genuine follow-up question",
        "weight": 1,
    },
    {
        "id": "comment.agree_with_angle",
        "text": "agree while adding a new angle",
        "weight": 1,
    },
    {
        "id": "comment.counterpoint",
        "text": "offer a respectful counterpoint",
        "weight": 1,
    },
    {
        "id": "comment.joke_or_aside",
        "text": "make a joke or playful aside",
        "weight": 1,
    },
    {
        "id": "comment.clarify_detail",
        "text": "clarify or correct one specific detail",
        "weight": 1,
    },
    {
        "id": "comment.recommend_resource",
        "text": "recommend a related resource or example",
        "weight": 1,
    },
]


def _canonicalize(document: dict) -> bool:
    """Apply only the recognizable legacy changes and report whether changed."""
    changed = False

    layouts = document.get("layouts")
    if isinstance(layouts, dict):
        for name in ("browse", "post"):
            layout = layouts.get(name)
            if isinstance(layout, str) and "{directions}" not in layout:
                layouts[name] = layout.rstrip() + "\n\n{directions}"
                changed = True

    lengths = document.get("length_catalog")
    comment_lengths = lengths.get("comment") if isinstance(lengths, dict) else None
    if isinstance(comment_lengths, list) and comment_lengths == [
        _LEGACY_COMMENT_LENGTH
    ]:
        document["length_catalog"]["comment"] = [
            dict(item) for item in _CANONICAL_COMMENT_LENGTH
        ]
        changed = True

    directions = document.get("direction_catalog")
    comment_directions = (
        directions.get("comment") if isinstance(directions, dict) else None
    )
    if (
        isinstance(comment_directions, list)
        and comment_directions == _LEGACY_COMMENT_DIRECTIONS
    ):
        document["direction_catalog"]["comment"] = [
            dict(item) for item in _CANONICAL_COMMENT_DIRECTIONS
        ]
        changed = True

    return changed


def _profile_template_id(conn):
    return conn.execute(
        sa.text("SELECT id FROM prompt_template WHERE name = :name"),
        {"name": _PROFILE_NAME},
    ).scalar()


def _pinned_sources(conn, template_id):
    return conn.execute(
        sa.text(
            "SELECT DISTINCT pp.version_number, ptv.body "
            "FROM prompt_pin pp "
            "JOIN prompt_template_version ptv "
            "ON ptv.template_id = pp.template_id AND ptv.version = pp.version_number "
            "WHERE pp.template_id = :template_id"
        ),
        {"template_id": template_id},
    ).fetchall()


def _existing_clones(conn, template_id):
    rows = conn.execute(
        sa.text(
            "SELECT version, created_by FROM prompt_template_version "
            "WHERE template_id = :template_id AND created_by LIKE :marker"
        ),
        {"template_id": template_id, "marker": _MIGRATION_MARKER + ":source_version=%"},
    ).fetchall()
    clones = {}
    for version, created_by in rows:
        match = _SOURCE_MARKER.fullmatch(created_by or "")
        if match:
            clones[int(match.group(1))] = int(version)
    return clones


def _next_version(conn, template_id) -> int:
    value = conn.execute(
        sa.text(
            "SELECT COALESCE(MAX(version), 0) FROM prompt_template_version "
            "WHERE template_id = :template_id"
        ),
        {"template_id": template_id},
    ).scalar_one()
    return int(value) + 1


def upgrade():
    conn = op.get_bind()
    template_id = _profile_template_id(conn)
    if template_id is None:
        return

    clones = _existing_clones(conn, template_id)
    # DISTINCT above gives one source body per version.  Sorting makes version
    # allocation deterministic when several pinned sources need a clone.
    for source_version, body in sorted(
        _pinned_sources(conn, template_id), key=lambda row: row[0]
    ):
        try:
            document = json.loads(body)
        except (TypeError, ValueError):
            continue
        if not isinstance(document, dict) or not _canonicalize(document):
            continue

        clone_version = clones.get(int(source_version))
        if clone_version is None:
            clone_version = _next_version(conn, template_id)
            conn.execute(
                sa.text(
                    "INSERT INTO prompt_template_version "
                    "(template_id, version, body, created_by, created_at) "
                    "VALUES (:template_id, :version, :body, :created_by, CURRENT_TIMESTAMP)"
                ),
                {
                    "template_id": template_id,
                    "version": clone_version,
                    "body": json.dumps(document, sort_keys=True, separators=(",", ":")),
                    "created_by": f"{_MIGRATION_MARKER}:source_version={int(source_version)}",
                },
            )
            clones[int(source_version)] = clone_version

        conn.execute(
            sa.text(
                "UPDATE prompt_pin SET version_number = :clone_version, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE template_id = :template_id AND version_number = :source_version"
            ),
            {
                "template_id": template_id,
                "clone_version": clone_version,
                "source_version": source_version,
            },
        )


def downgrade():
    conn = op.get_bind()
    template_id = _profile_template_id(conn)
    if template_id is None:
        return

    rows = conn.execute(
        sa.text(
            "SELECT version, created_by FROM prompt_template_version "
            "WHERE template_id = :template_id AND created_by LIKE :marker"
        ),
        {"template_id": template_id, "marker": _MIGRATION_MARKER + ":source_version=%"},
    ).fetchall()
    for clone_version, created_by in rows:
        match = _SOURCE_MARKER.fullmatch(created_by or "")
        if not match:
            continue
        source_version = int(match.group(1))
        conn.execute(
            sa.text(
                "UPDATE prompt_pin SET version_number = :source_version, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE template_id = :template_id AND version_number = :clone_version"
            ),
            {
                "template_id": template_id,
                "source_version": source_version,
                "clone_version": clone_version,
            },
        )
        # A clone can be retained if another pin was deliberately moved to it
        # after upgrade.  In that case it is not safe for downgrade to remove.
        # Render audits also retain a foreign-key reference to the version.
        conn.execute(
            sa.text(
                "DELETE FROM prompt_template_version "
                "WHERE template_id = :template_id AND version = :clone_version "
                "AND NOT EXISTS ("
                "SELECT 1 FROM prompt_pin pp "
                "WHERE pp.template_id = :template_id "
                "AND pp.version_number = :clone_version"
                ") "
                "AND NOT EXISTS ("
                "SELECT 1 "
                "FROM prompt_render_audit pra "
                "JOIN prompt_template_version audit_version "
                "ON audit_version.id = pra.template_version_id "
                "WHERE pra.template_id = :template_id "
                "AND audit_version.template_id = :template_id "
                "AND audit_version.version = :clone_version"
                ")"
            ),
            {"template_id": template_id, "clone_version": clone_version},
        )
