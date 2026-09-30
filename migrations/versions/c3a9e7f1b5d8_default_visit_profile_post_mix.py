"""Lower the default post-family intent mix to 0.12 in pinned visit profiles.

Revision ID: c3a9e7f1b5d8
Revises: b1e3a5c7d9f2
Create Date: 2026-09-30

The source-controlled default profile drops its post-family visit
probability from 0.30 to 0.12 (same 60/15/15/10 composition inside the
family) so visits produce roughly 5-6 comments per post instead of about 2.
Pinned profiles that still carry one of the previous default mixes - the
current source default or the legacy-conversion default with image/website
shares at 0.0 - are cloned immutably with the new post value; operator
tuning (any other mix) is left untouched, matching the v4 rollout rule.
"""

from __future__ import annotations

import json
import re

import sqlalchemy as sa
from alembic import op

revision = "c3a9e7f1b5d8"
down_revision = "b1e3a5c7d9f2"
branch_labels = None
depends_on = None

_PROFILE_NAME = "agent.visit_profile"
_MIGRATION_MARKER = "migration:visit_profile_v5"
_SOURCE_MARKER = re.compile(rf"^{re.escape(_MIGRATION_MARKER)}:source_version=(\d+)$")

# The only two post-family mixes the application has ever shipped as a
# default. Keys are ("backstage"|"image"|"post"|"website", value) pairs in
# sorted order; JSON round-trips make float equality exact.
_OLD_SHAPED_SOURCE_DEFAULT = (
    ("backstage", 0.10),
    ("image", 0.15),
    ("post", 0.30),
    ("website", 0.15),
)
_OLD_SHAPED_LEGACY_DEFAULT = (
    ("backstage", 0.10),
    ("image", 0.0),
    ("post", 0.30),
    ("website", 0.0),
)
_NEW_MIX_BY_OLD_SHAPE = {
    _OLD_SHAPED_SOURCE_DEFAULT: {
        "post": 0.12,
        "image": 0.15,
        "website": 0.15,
        "backstage": 0.10,
    },
    _OLD_SHAPED_LEGACY_DEFAULT: {
        "post": 0.12,
        "image": 0.0,
        "website": 0.0,
        "backstage": 0.10,
    },
}


def _mix_key(mix: dict) -> tuple[tuple[str, float], ...] | None:
    """Canonical sort-key of a validated four-key intent mix, else None."""
    if len(mix) != 4:
        return None
    try:
        return tuple(sorted((name, float(value)) for name, value in mix.items()))
    except (TypeError, ValueError):
        return None


def _canonicalize(document: dict) -> bool:
    """Rewrite only mixes that exactly match a shipped default shape."""
    mix = document.get("intent_mix")
    if not isinstance(mix, dict):
        return False
    replacement = _NEW_MIX_BY_OLD_SHAPE.get(_mix_key(mix))
    if replacement is None:
        # Operator-tuned or unrecognized mix: leave operator intent alone.
        return False
    document["intent_mix"] = dict(replacement)
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
        # Keep clones referenced by a pin or render audit. Only unreferenced
        # versions created by this revision are eligible for deletion.
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
