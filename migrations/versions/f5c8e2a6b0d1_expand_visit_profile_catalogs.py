"""Expand pinned visit profiles to the v3 direction catalogs immutably.

Revision ID: f5c8e2a6b0d1
Revises: f4c8e2a6b0d1
Create Date: 2026-08-30

Only profiles that are currently pinned are migrated. Existing versions are
never edited: each changed source gets one new immutable version and all pins
for that source are moved together.
"""

from __future__ import annotations

import json
import re

import sqlalchemy as sa
from alembic import op

revision = "f5c8e2a6b0d1"
down_revision = "f4c8e2a6b0d1"
branch_labels = None
depends_on = None

_PROFILE_NAME = "agent.visit_profile"
_MIGRATION_MARKER = "migration:visit_profile_v3"
_SOURCE_MARKER = re.compile(rf"^{re.escape(_MIGRATION_MARKER)}:source_version=(\d+)$")

# Keep the catalog local to the migration. Migrations must remain runnable when
# application modules (and their evolving defaults) are unavailable.
_CANONICAL_POST_DIRECTIONS = [
    {
        "id": "post.personal_experience",
        "text": "share a personal experience connected to your interests",
        "weight": 1,
    },
    {
        "id": "post.everyday_observation",
        "text": "describe something you noticed in everyday life",
        "weight": 1,
    },
    {
        "id": "post.project_in_progress",
        "text": "show or discuss a project, hobby, or work in progress",
        "weight": 1,
    },
    {
        "id": "post.genuine_question",
        "text": "ask a genuine question you want other people to answer",
        "weight": 1,
    },
    {
        "id": "post.tip_or_resource",
        "text": "offer a useful tip, resource, or lesson you learned",
        "weight": 1,
    },
    {
        "id": "post.surprising_fact",
        "text": "surface a surprising fact or piece of trivia",
        "weight": 1,
    },
    {
        "id": "post.opinion_or_argument",
        "text": "state an opinion or argument you want to discuss",
        "weight": 1,
    },
    {
        "id": "post.recommendation",
        "text": "recommend or review something you tried",
        "weight": 1,
    },
    {
        "id": "post.amusing_incident",
        "text": "tell an amusing incident or make a persona-fitting joke",
        "weight": 1,
    },
    {
        "id": "post.problem_and_advice",
        "text": "describe a problem and ask the community for advice",
        "weight": 1,
    },
    {
        "id": "post.how_to_explain",
        "text": "explain how to do something you know well",
        "weight": 1,
    },
    {
        "id": "post.local_discovery",
        "text": "share a specific local place, event, or discovery",
        "weight": 1,
    },
    {
        "id": "post.thoughtful_comparison",
        "text": "compare two things in a way that reveals a useful distinction",
        "weight": 1,
    },
    {
        "id": "post.creative_prompt",
        "text": "offer a creative prompt or small idea people can play with",
        "weight": 1,
    },
    {
        "id": "post.milestone_or_update",
        "text": "share a meaningful update or milestone",
        "weight": 1,
    },
    {
        "id": "post.niche_reference",
        "text": "bring up a niche reference that your interests make personally relevant",
        "weight": 1,
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
    {
        "id": "comment.share_lived_detail",
        "text": "add one relevant detail from your own experience",
        "weight": 1,
    },
    {
        "id": "comment.connect_threads",
        "text": "connect this discussion to a related idea or thread",
        "weight": 1,
    },
]


_CANONICAL_IMAGE_DIRECTIONS = [
    {
        "id": "image.candid_snapshot",
        "text": "Candid snapshot: observe the requested subject in an unposed, immediate moment.",
        "weight": 1,
    },
    {
        "id": "image.object_closeup",
        "text": "Object close-up: prioritize the requested subject's tactile detail at close range.",
        "weight": 1,
    },
    {
        "id": "image.place_observation",
        "text": "Place observation: let the requested place or context remain legible in an observational view.",
        "weight": 1,
    },
    {
        "id": "image.process_documentation",
        "text": "Process documentation: show the requested activity through an attentive record of its stages.",
        "weight": 1,
    },
    {
        "id": "image.finished_result",
        "text": "Finished result: present the requested completed work with calm, legible emphasis.",
        "weight": 1,
    },
    {
        "id": "image.before_after",
        "text": "Before and after: make the requested change readable through a direct visual comparison.",
        "weight": 1,
    },
    {
        "id": "image.archival_artifact",
        "text": "Archival artifact: preserve the requested item's evidence of age, use, and provenance.",
        "weight": 1,
    },
    {
        "id": "image.food_photo",
        "text": "Food photo: make the requested food immediately appetizing through texture and arrangement.",
        "weight": 1,
    },
    {
        "id": "image.pet_wildlife",
        "text": "Pet and wildlife: attend to the requested animal's behavior without forcing a pose.",
        "weight": 1,
    },
    {
        "id": "image.macro_detail",
        "text": "Macro detail: magnify the requested subject's small structure, texture, or transition.",
        "weight": 1,
    },
    {
        "id": "image.diagram_infographic",
        "text": "Diagram and infographic: organize the requested information so relationships are immediately legible.",
        "weight": 1,
    },
    {
        "id": "image.artwork_craft",
        "text": "Artwork and craft: foreground the requested work's material decisions and hand-made evidence.",
        "weight": 1,
    },
]

_CANONICAL_WEBSITE_DIRECTIONS = [
    {
        "id": "website.news_report",
        "text": "A timely news report with a strong masthead, headline hierarchy, bylines, and clearly dated stories.",
        "weight": 1,
    },
    {
        "id": "website.magazine_feature",
        "text": "A richly paced magazine feature with an authored point of view, visual pull quotes, and substantial sections.",
        "weight": 1,
    },
    {
        "id": "website.personal_blog",
        "text": "A personal blog with an unmistakable author voice, dated entries, related posts, and a readable stream.",
        "weight": 1,
    },
    {
        "id": "website.community_portal",
        "text": "A community portal organized around member voices, discussions, announcements, and participation.",
        "weight": 1,
    },
    {
        "id": "website.event_program",
        "text": "An event program that helps visitors understand sessions, people, place, and timing.",
        "weight": 1,
    },
    {
        "id": "website.local_business",
        "text": "A local business presence focused on offerings, practical details, trust, and an invitation to visit.",
        "weight": 1,
    },
    {
        "id": "website.nonprofit_campaign",
        "text": "A nonprofit campaign with a clear cause, evidence, human stakes, and meaningful ways to participate.",
        "weight": 1,
    },
    {
        "id": "website.product_page",
        "text": "A product page that explains a focused offering through benefits, proof, details, and confident calls to action.",
        "weight": 1,
    },
    {
        "id": "website.catalog",
        "text": "A catalog for browsing and comparing a collection of items with useful labels and predictable discovery.",
        "weight": 1,
    },
    {
        "id": "website.reference",
        "text": "A reference site optimized for lookup, hierarchy, cross-references, and fast scanning.",
        "weight": 1,
    },
    {
        "id": "website.data_dashboard",
        "text": "A data dashboard that makes changing measurements, status, and comparisons legible at a glance.",
        "weight": 1,
    },
    {
        "id": "website.interactive_utility",
        "text": "An interactive utility designed for completing a small practical task with clear state and feedback.",
        "weight": 1,
    },
    {
        "id": "website.fan_archive",
        "text": "A fan archive preserving a subject's history through indexes, chronology, collections, and provenance.",
        "weight": 1,
    },
    {
        "id": "website.travel_guide",
        "text": "A travel guide combining orientation, recommendations, routes, and a vivid sense of place.",
        "weight": 1,
    },
    {
        "id": "website.portfolio",
        "text": "A creator portfolio foregrounding selected work, case studies, and a distinctive professional identity.",
        "weight": 1,
    },
    {
        "id": "website.experimental_microsite",
        "text": "An experimental microsite using an unusual but legible visual system to make one idea memorable.",
        "weight": 1,
    },
]

_CANONICAL_DIRECTIONS = {
    "post": _CANONICAL_POST_DIRECTIONS,
    "comment": _CANONICAL_COMMENT_DIRECTIONS,
    "image": _CANONICAL_IMAGE_DIRECTIONS,
    "website": _CANONICAL_WEBSITE_DIRECTIONS,
}


def _copy_catalog(items: list[dict]) -> list[dict]:
    return [dict(item) for item in items]


def _canonicalize(document: dict) -> bool:
    """Upgrade one recognizable v1/v2 document and report whether changed."""
    expected_fields = {
        "schema_version",
        "system_template",
        "layouts",
        "behavior_blocks",
        "intent_mix",
        "length_catalog",
        "direction_catalog",
        "sample_count",
    }
    if set(document) != expected_fields:
        return False

    schema_version = document.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        return False

    directions = document.get("direction_catalog")
    if not isinstance(directions, dict):
        return False

    if not set(directions) <= {
        "post",
        "comment",
        "backstage",
        "image",
        "website",
    }:
        return False
    # These catalogs existed before v3 and must remain structurally intact;
    # otherwise cloning would turn malformed JSON into a pinned invalid profile.
    if any(
        not isinstance(directions.get(kind), list)
        for kind in ("post", "comment", "backstage")
    ):
        return False

    sample_count = document.get("sample_count")
    changed = (
        not isinstance(sample_count, int)
        or isinstance(sample_count, bool)
        or sample_count != 1
    )
    document["sample_count"] = 1
    for kind, canonical in _CANONICAL_DIRECTIONS.items():
        if directions.get(kind) != canonical:
            directions[kind] = _copy_catalog(canonical)
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
    # DISTINCT above gives one source body per version. Sorting makes version
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
