"""Phase 4 persistence upgrade preserves legacy prompt data."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory

import deaddit
from deaddit import create_app

_PREDECESSOR = "a7c3e9f5b1d8"
_PROFILE_REVISION = "d4f9a2c7e1b6"


def _script() -> ScriptDirectory:
    cfg = AlembicConfig()
    cfg.set_main_option(
        "script_location",
        str(Path(deaddit.__file__).resolve().parent.parent / "migrations"),
    )
    return ScriptDirectory.from_config(cfg)


def test_phase4_upgrade_migrates_legacy_pin_and_mix(tmp_path):
    db_path = tmp_path / "phase4.db"
    app = create_app(
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "TESTING": True}
    )
    runner = app.test_cli_runner()
    predecessor = runner.invoke(args=["db", "upgrade", _PREDECESSOR])
    assert predecessor.exit_code == 0, predecessor.output

    old_system = "legacy system prompt bytes: {opaque}\\x00\\n"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO prompt_template (name, description, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            ("agent.system_prompt", "legacy system prompt"),
        )
        legacy_template_id = conn.execute(
            "SELECT id FROM prompt_template WHERE name = ?", ("agent.system_prompt",)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO prompt_template_version (template_id, version, body, created_by, created_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (legacy_template_id, 1, old_system, "legacy-test"),
        )
        conn.execute(
            "INSERT INTO prompt_pin (target_kind, target_key, template_id, version_number, updated_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ("agent", "42", legacy_template_id, 1),
        )
        conn.executemany(
            "INSERT INTO setting (key, value, description, created_at, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            [
                ("AGENT_POST_INTENT_CHANCE", "0.45", None),
                ("AGENT_FORCED_IMAGE_CHANCE", "0.15", None),
                ("AGENT_FORCED_WEBSITE_CHANCE", "0.25", None),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    upgraded = runner.invoke(args=["db", "upgrade"])
    assert upgraded.exit_code == 0, upgraded.output

    conn = sqlite3.connect(db_path)
    try:
        profile_template_id = conn.execute(
            "SELECT id FROM prompt_template WHERE name = 'agent.visit_profile'"
        ).fetchone()[0]
        pins = conn.execute(
            "SELECT target_kind, target_key, template_id, version_number FROM prompt_pin"
        ).fetchall()
        assert any(
            kind == "agent" and key == "42" and template_id == profile_template_id
            for kind, key, template_id, _version in pins
        )
        profile_bodies = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT body FROM prompt_template_version WHERE template_id = ?",
                (profile_template_id,),
            )
        ]
        legacy_profiles = [
            p for p in profile_bodies if p["system_template"] == old_system
        ]
        assert legacy_profiles
        assert all(p["layouts"]["system"] == old_system for p in legacy_profiles)
        assert any(
            p["intent_mix"]
            == {
                "post": 0.45,
                "image": 0.15,
                "website": 0.25,
                "backstage": 0.10,
            }
            for p in profile_bodies
        )
        # Unpinned historical versions remain immutable under the pinned-only
        # rollout; validate the v3-shaped versions produced for active pins.
        from deaddit.llm.prompts import parse_visit_profile

        migrated_profiles = [
            profile
            for profile in profile_bodies
            if profile.get("sample_count") == 1
            and set(profile.get("direction_catalog", {}))
            == {"post", "comment", "backstage", "image", "website"}
        ]
        assert migrated_profiles
        for profile in migrated_profiles:
            parsed = parse_visit_profile(json.dumps(profile))
            for kind, items in parsed.length_catalog.items():
                assert sum(item.weight for item in items) == 100.0, (kind, profile)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(agent_run)").fetchall()
        }
        assert "prompt_metadata" in columns
        setting_keys = {row[0] for row in conn.execute("SELECT key FROM setting")}
        assert (
            not {
                "AGENT_POST_INTENT_CHANCE",
                "AGENT_FORCED_IMAGE_CHANCE",
                "AGENT_FORCED_WEBSITE_CHANCE",
            }
            & setting_keys
        )
    finally:
        conn.close()

    revisions = [rev.revision for rev in _script().walk_revisions()]
    assert _PROFILE_REVISION in revisions


def test_profile_data_migrations_upgrade_legacy_documents(tmp_path):
    """Stored profiles gain percentage lengths and the backstage contract."""
    db_path = tmp_path / "normalize.db"
    app = create_app(
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "TESTING": True}
    )
    runner = app.test_cli_runner()
    assert runner.invoke(args=["db", "upgrade", "d4f9a2c7e1b6"]).exit_code == 0

    fractional = {
        "schema_version": 1,
        "system_template": "system",
        "layouts": {
            "system": "system",
            "lurker": "l",
            "browse": "b",
            "post": "p",
        },
        "behavior_blocks": [{"id": "general.genuine", "text": "be genuine"}],
        "intent_mix": {"post": 0.3, "image": 0.0, "website": 0.0},
        "length_catalog": {
            kind: [
                {"id": f"{kind}.short", "text": "short", "weight": 1} for _ in range(1)
            ]
            for kind in ("comment", "media_post", "text_post")
        },
        "direction_catalog": {
            "comment": [
                {"id": "comment.a", "text": "a", "weight": 1},
                {"id": "comment.b", "text": "b", "weight": 1},
                {"id": "comment.c", "text": "c", "weight": 1},
            ],
            "post": [
                {"id": "post.a", "text": "a", "weight": 1},
                {"id": "post.b", "text": "b", "weight": 1},
                {"id": "post.c", "text": "c", "weight": 1},
            ],
            "image": [
                {"id": "image.candid_snapshot", "text": "image", "weight": 1},
                {"id": "image.object_closeup", "text": "image", "weight": 1},
                {"id": "image.place_observation", "text": "image", "weight": 1},
            ],
            "website": [
                {"id": "website.news_report", "text": "website", "weight": 1},
                {"id": "website.magazine_feature", "text": "website", "weight": 1},
                {"id": "website.personal_blog", "text": "website", "weight": 1},
            ],
        },
        "sample_count": 1,
    }
    conn = sqlite3.connect(db_path)
    inserted_version = None
    template_id = None
    try:
        template_id = conn.execute(
            "SELECT id FROM prompt_template WHERE name = 'agent.visit_profile'"
        ).fetchone()[0]
        inserted_version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 "
            "FROM prompt_template_version WHERE template_id = ?",
            (template_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO prompt_template_version "
            "(template_id, version, body, created_by, created_at) "
            "VALUES (?, ?, ?, 'legacy', CURRENT_TIMESTAMP)",
            (template_id, inserted_version, json.dumps(fractional)),
        )
        conn.commit()
    finally:
        conn.close()

    assert runner.invoke(args=["db", "upgrade"]).exit_code == 0

    from deaddit.llm.prompts import parse_visit_profile

    conn = sqlite3.connect(db_path)
    try:
        body = conn.execute(
            "SELECT body FROM prompt_template_version "
            "WHERE template_id = ? AND version = ?",
            (template_id, inserted_version),
        ).fetchone()[0]
    finally:
        conn.close()
    parsed = parse_visit_profile(body)
    for _kind, items in parsed.length_catalog.items():
        assert sum(item.weight for item in items) == 100.0

    assert parsed.intent_mix["backstage"] == 0.10
    assert parsed.layouts["backstage"] == "{directions}"
    assert len(parsed.direction_catalog["backstage"]) >= parsed.sample_count


_ROLLOUT_PREDECESSOR = "e7f1a3b5c9d2"
_ROLLOUT_REVISION = "f4c8e2a6b0d1"


def test_comment_profile_rollout_clones_active_sources_and_restores_on_downgrade(
    tmp_path,
):
    """Only active legacy sources are cloned; their immutable history survives."""
    db_path = tmp_path / "comment-rollout.db"
    app = create_app(
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "TESTING": True}
    )
    runner = app.test_cli_runner()
    assert runner.invoke(args=["db", "upgrade", _ROLLOUT_PREDECESSOR]).exit_code == 0

    conn = sqlite3.connect(db_path)
    try:
        template_id = conn.execute(
            "SELECT id FROM prompt_template WHERE name = 'agent.visit_profile'"
        ).fetchone()[0]
        source_version = 1
        source_body = conn.execute(
            "SELECT body FROM prompt_template_version "
            "WHERE template_id = ? AND version = ?",
            (template_id, source_version),
        ).fetchone()[0]
        custom = json.loads(source_body)
        custom["length_catalog"]["comment"] = [
            {
                "id": "comment.short",
                "text": "Operator-selected concise response.",
                "weight": 100,
            }
        ]
        custom["direction_catalog"]["comment"] = [
            {
                "id": "comment.honest_reaction",
                "text": "Operator-selected reaction.",
                "weight": 2,
            },
            {
                "id": "comment.relevant_fact",
                "text": "Operator-selected fact.",
                "weight": 3,
            },
            {
                "id": "comment.follow_up_question",
                "text": "Operator-selected question.",
                "weight": 5,
            },
        ]
        custom_version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 "
            "FROM prompt_template_version WHERE template_id = ?",
            (template_id,),
        ).fetchone()[0]
        custom_body = json.dumps(custom, sort_keys=True, separators=(",", ":"))
        conn.execute(
            "INSERT INTO prompt_template_version "
            "(template_id, version, body, created_by, created_at) "
            "VALUES (?, ?, ?, 'operator', CURRENT_TIMESTAMP)",
            (template_id, custom_version, custom_body),
        )
        unreferenced_version = custom_version + 1
        conn.execute(
            "INSERT INTO prompt_template_version "
            "(template_id, version, body, created_by, created_at) "
            "VALUES (?, ?, ?, 'operator', CURRENT_TIMESTAMP)",
            (template_id, unreferenced_version, source_body),
        )
        conn.executemany(
            "INSERT INTO prompt_pin "
            "(target_kind, target_key, template_id, version_number, updated_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            [
                ("agent", "101", template_id, source_version),
                ("cohort", "quiet", template_id, source_version),
                ("agent", "102", template_id, custom_version),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    assert runner.invoke(args=["db", "upgrade", _ROLLOUT_REVISION]).exit_code == 0

    conn = sqlite3.connect(db_path)
    try:
        source_after_upgrade = conn.execute(
            "SELECT body FROM prompt_template_version "
            "WHERE template_id = ? AND version = ?",
            (template_id, source_version),
        ).fetchone()[0]
        assert source_after_upgrade == source_body
        assert (
            conn.execute(
                "SELECT body FROM prompt_template_version "
                "WHERE template_id = ? AND version = ?",
                (template_id, custom_version),
            ).fetchone()[0]
            == custom_body
        )
        assert (
            conn.execute(
                "SELECT body FROM prompt_template_version "
                "WHERE template_id = ? AND version = ?",
                (template_id, unreferenced_version),
            ).fetchone()[0]
            == source_body
        )

        clones = conn.execute(
            "SELECT version, body, created_by FROM prompt_template_version "
            "WHERE template_id = ? AND created_by LIKE 'migration:visit_profile_v2:%' "
            "ORDER BY version",
            (template_id,),
        ).fetchall()
        assert len(clones) == 2
        clone_by_source = {
            int(row[2].rsplit("=", 1)[1]): (row[0], row[1], json.loads(row[1]))
            for row in clones
        }
        assert set(clone_by_source) == {source_version, custom_version}
        source_clone_version, migrated_raw, migrated = clone_by_source[source_version]
        custom_clone_version, _custom_raw, migrated_custom = clone_by_source[
            custom_version
        ]

        pins = conn.execute(
            "SELECT target_kind, target_key, version_number FROM prompt_pin "
            "WHERE template_id = ? ORDER BY target_kind, target_key",
            (template_id,),
        ).fetchall()
        assert pins == [
            ("agent", "101", source_clone_version),
            ("agent", "102", custom_clone_version),
            ("cohort", "quiet", source_clone_version),
        ]
        source_clone_id = conn.execute(
            "SELECT id FROM prompt_template_version "
            "WHERE template_id = ? AND version = ?",
            (template_id, source_clone_version),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO prompt_render_audit "
            "(id, created_at, template_id, template_version_id, subject_kind, "
            "subject_key, rendered_sha256, variables_json) "
            "VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?)",
            (
                9001,
                template_id,
                source_clone_id,
                "agent",
                "101",
                "a" * 64,
                "{}",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    assert migrated_raw == json.dumps(migrated, sort_keys=True, separators=(",", ":"))

    assert [item["id"] for item in migrated["length_catalog"]["comment"]] == [
        "comment.snippet",
        "comment.short",
        "comment.medium",
        "comment.long",
    ]
    from deaddit.agents.prompts import _LENGTH_TARGETS

    assert [item["text"] for item in migrated["length_catalog"]["comment"]] == [
        target.text for target in _LENGTH_TARGETS["comment"]
    ]
    assert [item["weight"] for item in migrated["length_catalog"]["comment"]] == [
        35,
        50,
        12,
        3,
    ]
    assert [item["id"] for item in migrated["direction_catalog"]["comment"]] == [
        "comment.honest_reaction",
        "comment.relevant_fact",
        "comment.related_anecdote",
        "comment.answer_or_advice",
        "comment.follow_up_question",
        "comment.agree_with_angle",
        "comment.counterpoint",
        "comment.joke_or_aside",
        "comment.clarify_detail",
        "comment.recommend_resource",
    ]
    for name in ("browse", "post"):
        assert "{directions}" in migrated["layouts"][name]
        assert "sampled direction" in migrated["layouts"][name].replace(
            "{directions}", "sampled direction"
        )

    # A custom catalog is not recognized as legacy and is copied byte-for-byte
    # along with the layout fix that made this source active for migration.
    assert (
        migrated_custom["length_catalog"]["comment"]
        == custom["length_catalog"]["comment"]
    )
    assert (
        migrated_custom["direction_catalog"]["comment"]
        == custom["direction_catalog"]["comment"]
    )

    assert runner.invoke(args=["db", "downgrade", _ROLLOUT_PREDECESSOR]).exit_code == 0
    conn = sqlite3.connect(db_path)
    try:
        restored_pins = conn.execute(
            "SELECT target_kind, target_key, version_number FROM prompt_pin "
            "WHERE template_id = ? ORDER BY target_kind, target_key",
            (template_id,),
        ).fetchall()
        assert restored_pins == [
            ("agent", "101", source_version),
            ("agent", "102", custom_version),
            ("cohort", "quiet", source_version),
        ]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM prompt_template_version "
                "WHERE template_id = ? AND created_by LIKE 'migration:visit_profile_v2:%'",
                (template_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM prompt_render_audit "
                "WHERE template_id = ? AND template_version_id = ("
                "SELECT id FROM prompt_template_version "
                "WHERE template_id = ? AND version = ?"
                ")",
                (template_id, template_id, source_clone_version),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM prompt_template_version "
                "WHERE template_id = ? AND version = ?",
                (template_id, custom_clone_version),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT body FROM prompt_template_version "
                "WHERE template_id = ? AND version = ?",
                (template_id, source_version),
            ).fetchone()[0]
            == source_body
        )
    finally:
        conn.close()
    assert _ROLLOUT_REVISION in [rev.revision for rev in _script().walk_revisions()]


_V3_PREDECESSOR = "f4c8e2a6b0d1"
_V3_REVISION = "f5c8e2a6b0d1"
_V3_POST_IDS = [
    "post.personal_experience",
    "post.everyday_observation",
    "post.project_in_progress",
    "post.genuine_question",
    "post.tip_or_resource",
    "post.surprising_fact",
    "post.opinion_or_argument",
    "post.recommendation",
    "post.amusing_incident",
    "post.problem_and_advice",
    "post.how_to_explain",
    "post.local_discovery",
    "post.thoughtful_comparison",
    "post.creative_prompt",
    "post.milestone_or_update",
    "post.niche_reference",
]
_V3_COMMENT_IDS = [
    "comment.honest_reaction",
    "comment.relevant_fact",
    "comment.related_anecdote",
    "comment.answer_or_advice",
    "comment.follow_up_question",
    "comment.agree_with_angle",
    "comment.counterpoint",
    "comment.joke_or_aside",
    "comment.clarify_detail",
    "comment.recommend_resource",
    "comment.share_lived_detail",
    "comment.connect_threads",
]
_V3_IMAGE_IDS = [
    "image.candid_snapshot",
    "image.object_closeup",
    "image.place_observation",
    "image.process_documentation",
    "image.finished_result",
    "image.before_after",
    "image.archival_artifact",
    "image.food_photo",
    "image.pet_wildlife",
    "image.macro_detail",
    "image.diagram_infographic",
    "image.artwork_craft",
]
_V3_WEBSITE_IDS = [
    "website.news_report",
    "website.magazine_feature",
    "website.personal_blog",
    "website.community_portal",
    "website.event_program",
    "website.local_business",
    "website.nonprofit_campaign",
    "website.product_page",
    "website.catalog",
    "website.reference",
    "website.data_dashboard",
    "website.interactive_utility",
    "website.fan_archive",
    "website.travel_guide",
    "website.portfolio",
    "website.experimental_microsite",
]


def test_v3_rollout_clones_pinned_sources_idempotently_and_downgrades(
    tmp_path,
):
    db_path = tmp_path / "visit-profile-v3.db"
    app = create_app(
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "TESTING": True}
    )
    runner = app.test_cli_runner()
    assert runner.invoke(args=["db", "upgrade", _V3_PREDECESSOR]).exit_code == 0

    conn = sqlite3.connect(db_path)
    try:
        template_id = conn.execute(
            "SELECT id FROM prompt_template WHERE name = 'agent.visit_profile'"
        ).fetchone()[0]
        source = json.loads(
            conn.execute(
                "SELECT body FROM prompt_template_version "
                "WHERE template_id = ? AND version = 1",
                (template_id,),
            ).fetchone()[0]
        )
        source["behavior_blocks"] = [
            {"id": "custom.voice", "text": "Keep the operator's quiet voice."}
        ]
        source["layouts"]["post"] = "Use this operator layout: {directions}"
        source["intent_mix"] = {
            "post": 0.20,
            "image": 0.30,
            "website": 0.10,
            "backstage": 0.10,
        }
        source["length_catalog"]["comment"] = [
            {
                "id": "comment.operator_length",
                "text": "Use the operator's compact comment style.",
                "weight": 100,
            }
        ]
        source["direction_catalog"]["backstage"] = [
            {
                "id": "backstage.operator_topic",
                "text": "Discuss the operator's latest thought.",
                "weight": 4,
            },
            {
                "id": "backstage.operator_memory",
                "text": "Connect the operator's remembered visits.",
                "weight": 3,
            },
            {
                "id": "backstage.operator_question",
                "text": "Ask the operator a useful question.",
                "weight": 2,
            },
        ]
        source["sample_count"] = 3
        source_version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 "
            "FROM prompt_template_version WHERE template_id = ?",
            (template_id,),
        ).fetchone()[0]
        source_body = json.dumps(source, sort_keys=True, separators=(",", ":"))
        conn.execute(
            "INSERT INTO prompt_template_version "
            "(template_id, version, body, created_by, created_at) "
            "VALUES (?, ?, ?, 'operator-v3-test', CURRENT_TIMESTAMP)",
            (template_id, source_version, source_body),
        )
        conn.executemany(
            "INSERT INTO prompt_pin "
            "(target_kind, target_key, template_id, version_number, updated_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            [
                ("agent", "v3-agent", template_id, source_version),
                ("cohort", "v3-cohort", template_id, source_version),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    upgraded = runner.invoke(args=["db", "upgrade"])
    assert upgraded.exit_code == 0, upgraded.output

    conn = sqlite3.connect(db_path)
    try:
        assert (
            conn.execute(
                "SELECT body FROM prompt_template_version "
                "WHERE template_id = ? AND version = ?",
                (template_id, source_version),
            ).fetchone()[0]
            == source_body
        )
        clones = conn.execute(
            "SELECT version, body, created_by FROM prompt_template_version "
            "WHERE template_id = ? AND created_by LIKE "
            "'migration:visit_profile_v3:%'",
            (template_id,),
        ).fetchall()
        assert len(clones) == 1
        clone_version, clone_body, created_by = clones[0]
        assert created_by == (
            f"migration:visit_profile_v3:source_version={source_version}"
        )
        migrated = json.loads(clone_body)
        assert migrated["sample_count"] == 1
        assert set(migrated["direction_catalog"]) == {
            "post",
            "comment",
            "backstage",
            "image",
            "website",
        }
        assert [
            item["id"] for item in migrated["direction_catalog"]["post"]
        ] == _V3_POST_IDS
        assert [
            item["id"] for item in migrated["direction_catalog"]["comment"]
        ] == _V3_COMMENT_IDS
        assert [
            item["id"] for item in migrated["direction_catalog"]["image"]
        ] == _V3_IMAGE_IDS
        assert [
            item["id"] for item in migrated["direction_catalog"]["website"]
        ] == _V3_WEBSITE_IDS
        for kind in ("post", "comment", "image", "website"):
            assert {item["weight"] for item in migrated["direction_catalog"][kind]} == {
                1
            }
        assert migrated["behavior_blocks"] == source["behavior_blocks"]
        assert migrated["layouts"] == source["layouts"]
        assert migrated["intent_mix"] == source["intent_mix"]
        assert migrated["length_catalog"] == source["length_catalog"]
        assert (
            migrated["direction_catalog"]["backstage"]
            == source["direction_catalog"]["backstage"]
        )
        assert conn.execute(
            "SELECT target_kind, target_key, version_number FROM prompt_pin "
            "WHERE template_id = ? ORDER BY target_kind, target_key",
            (template_id,),
        ).fetchall() == [
            ("agent", "v3-agent", clone_version),
            ("cohort", "v3-cohort", clone_version),
        ]

        # Exercise the revision itself a second time while the marker clone
        # remains pinned. A canonical clone must not produce another version.
        conn.execute(
            "UPDATE alembic_version SET version_num = ?",
            (_V3_PREDECESSOR,),
        )
        conn.commit()
    finally:
        conn.close()

    rerun = runner.invoke(args=["db", "upgrade"])
    assert rerun.exit_code == 0, rerun.output
    conn = sqlite3.connect(db_path)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM prompt_template_version "
                "WHERE template_id = ? AND created_by LIKE "
                "'migration:visit_profile_v3:%'",
                (template_id,),
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()

    downgraded = runner.invoke(args=["db", "downgrade", _V3_PREDECESSOR])
    assert downgraded.exit_code == 0, downgraded.output
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT target_kind, target_key, version_number FROM prompt_pin "
            "WHERE template_id = ? ORDER BY target_kind, target_key",
            (template_id,),
        ).fetchall() == [
            ("agent", "v3-agent", source_version),
            ("cohort", "v3-cohort", source_version),
        ]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM prompt_template_version "
                "WHERE template_id = ? AND created_by LIKE "
                "'migration:visit_profile_v3:%'",
                (template_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()

    assert _V3_REVISION in [rev.revision for rev in _script().walk_revisions()]
