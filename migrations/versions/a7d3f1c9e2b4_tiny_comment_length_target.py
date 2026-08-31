"""Add the tiny comment length target to pinned visit profiles immutably.

Revision ID: a7d3f1c9e2b4
Revises: f5c8e2a6b0d1
Create Date: 2026-08-30

Real-world Reddit threads mix a mass of very short reactions into the
ordinary comment lengths. This revision clones each currently pinned
``agent.visit_profile`` source whose comment length catalog predates
``comment.tiny`` onto a new immutable version carrying the canonical
five-tier comment distribution, and moves every pin for that source
together. Existing versions are never edited.
"""

from __future__ import annotations

import json
import re

import sqlalchemy as sa
from alembic import op

revision = "a7d3f1c9e2b4"
down_revision = "f5c8e2a6b0d1"
branch_labels = None
depends_on = None

_PROFILE_NAME = "agent.visit_profile"
_MIGRATION_MARKER = "migration:visit_profile_v4"
_SOURCE_MARKER = re.compile(rf"^{re.escape(_MIGRATION_MARKER)}:source_version=(\d+)$")

# Keep the catalog local to the migration. Migrations must remain runnable
# when application modules (and their evolving defaults) are unavailable.
_CANONICAL_COMMENT_LENGTHS = [
    {
        "id": "comment.tiny",
        "text": (
            "Length target for this comment: a very short reaction, at most about "
            "8 words; a sentence fragment is fine. No setup, no explanation, no punctuation polish."
        ),
        "weight": 18,
    },
    {
        "id": "comment.snippet",
        "text": (
            "Length target for this comment: no more than one sentence and no more "
            "than 20 words. State the point directly; do not add setup, a conclusion, or padding."
        ),
        "weight": 30,
    },
    {
        "id": "comment.short",
        "text": (
            "Length target for this comment: exactly 2 or 3 sentences and 20-60 words. "
            "Make every sentence useful; do not add setup, a conclusion, or padding."
        ),
        "weight": 42,
    },
    {
        "id": "comment.medium",
        "text": (
            "Length target for this comment: one compact paragraph of 60-120 words. "
            "Use only relevant detail; do not add setup, a conclusion, or padding."
        ),
        "weight": 8,
    },
    {
        "id": "comment.long",
        "text": (
            "Length target for this comment: 2 or 3 short paragraphs of 120-250 words. "
            "Make the extra detail earn its space; do not add setup, a conclusion, or padding."
        ),
        "weight": 2,
    },
]


def _canonicalize(document: dict) -> bool:
    """Install the five-tier comment lengths when the source predates tiny."""
    lengths = document.get("length_catalog")
    if not isinstance(lengths, dict) or not isinstance(lengths.get("comment"), list):
        return False
    comment_lengths = lengths["comment"]
    if any(item.get("id") == "comment.tiny" for item in comment_lengths):
        return False
    if [item.get("id") for item in comment_lengths] != [
        "comment.snippet",
        "comment.short",
        "comment.medium",
        "comment.long",
    ]:
        # Unrecognized custom distribution: leave operator tuning alone.
        return False
    lengths["comment"] = [dict(item) for item in _CANONICAL_COMMENT_LENGTHS]
    return True


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
                "SELECT 1 FROM prompt_render_audit pra "
                "JOIN prompt_template_version audit_version "
                "ON audit_version.id = pra.template_version_id "
                "WHERE pra.template_id = :template_id "
                "AND audit_version.template_id = :template_id "
                "AND audit_version.version = :clone_version"
                ")"
            ),
            {"template_id": template_id, "clone_version": clone_version},
        )
