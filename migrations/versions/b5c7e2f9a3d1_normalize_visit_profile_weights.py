"""Normalize legacy visit-profile length weights to the percentage scale.

Revision d4f9a2c7e1b6 wrote single-target length catalogs with weight 1
(fractional scale), but the runtime contract is percentage weights totalling
100 per content kind: the length target is chosen by a 0-99 quantile draw
against the cumulative weights, and profile validation requires the total
to be 100. Databases upgraded under the earlier revision carry those
fractional documents; this revision rescales every stored
``agent.visit_profile`` version whose length catalogs sum to ~1.

Direction catalog weights are not consumed by sampling and keep their
unit scale in both eras, so they are left untouched.

Revision ID: b5c7e2f9a3d1
Revises: d4f9a2c7e1b6
Create Date: 2026-08-29

"""

from __future__ import annotations

import json
import math

import sqlalchemy as sa
from alembic import op

revision = "b5c7e2f9a3d1"
down_revision = "d4f9a2c7e1b6"
branch_labels = None
depends_on = None

_PROFILE_NAME = "agent.visit_profile"
_CONTENT_KINDS = ("comment", "media_post", "text_post")


def _rescale(document: dict) -> bool:
    """Scale fractional length catalogs to percentages; report any change."""
    changed = False
    for kind in _CONTENT_KINDS:
        items = document.get("length_catalog", {}).get(kind)
        if not isinstance(items, list) or not items:
            continue
        weights = [item.get("weight") for item in items]
        if not all(isinstance(w, int | float) for w in weights):
            continue

        total = math.fsum(weights)
        if 0 < total <= 1.0 + 1e-9 and all(w <= 1.0 + 1e-9 for w in weights):
            for item in items:
                item["weight"] = round(item["weight"] * 100, 6)
            changed = True
    return changed


def upgrade():
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT ptv.id, ptv.body FROM prompt_template_version ptv "
            "JOIN prompt_template pt ON pt.id = ptv.template_id "
            "WHERE pt.name = :name"
        ),
        {"name": _PROFILE_NAME},
    ).fetchall()
    for row_id, body in rows:
        try:
            document = json.loads(body)
        except (TypeError, ValueError):
            continue
        if not isinstance(document, dict) or not _rescale(document):
            continue
        conn.execute(
            sa.text("UPDATE prompt_template_version SET body = :body WHERE id = :id"),
            {
                "body": json.dumps(document, sort_keys=True, separators=(",", ":")),
                "id": row_id,
            },
        )


def downgrade():
    # Percentage weights are the canonical contract; the fractional scale
    # was never valid, so there is nothing to restore.
    pass
