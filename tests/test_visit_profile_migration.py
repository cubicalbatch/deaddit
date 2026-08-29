"""Phase 4 persistence upgrade preserves legacy prompt data."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import deaddit
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory

from deaddit import create_app

_PREDECESSOR = "a7c3e9f5b1d8"
_PROFILE_REVISION = "d4f9a2c7e1b6"


def _script() -> ScriptDirectory:
    cfg = AlembicConfig()
    cfg.set_main_option(
        "script_location", str(Path(deaddit.__file__).resolve().parent.parent / "migrations")
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
            kind == "agent"
            and key == "42"
            and template_id == profile_template_id
            for kind, key, template_id, _version in pins
        )
        profile_bodies = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT body FROM prompt_template_version WHERE template_id = ?",
                (profile_template_id,),
            )
        ]
        legacy_profiles = [p for p in profile_bodies if p["system_template"] == old_system]
        assert legacy_profiles
        assert all(p["layouts"]["system"] == old_system for p in legacy_profiles)
        assert any(
            p["intent_mix"] == {"post": 0.45, "image": 0.15, "website": 0.25}
            for p in profile_bodies
        )
        # Every migrated profile is valid under current validation and uses
        # percentage length weights (quantile draws are 0-99).
        from deaddit.llm.prompts import parse_visit_profile

        for profile in profile_bodies:
            parsed = parse_visit_profile(json.dumps(profile))
            for kind, items in parsed.length_catalog.items():
                assert sum(item.weight for item in items) == 100.0, (kind, profile)
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(agent_run)").fetchall()
        }
        assert "prompt_metadata" in columns
        setting_keys = {row[0] for row in conn.execute("SELECT key FROM setting")}
        assert not {
            "AGENT_POST_INTENT_CHANCE",
            "AGENT_FORCED_IMAGE_CHANCE",
            "AGENT_FORCED_WEBSITE_CHANCE",
        } & setting_keys
    finally:
        conn.close()

    revisions = [rev.revision for rev in _script().walk_revisions()]
    assert _PROFILE_REVISION in revisions


def test_weight_normalization_rescales_fractional_documents(tmp_path):
    """Profiles written by the original d4f9a2c7e1b6 reach percentage scale."""
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
                {"id": f"{kind}.short", "text": "short", "weight": 1}
                for _ in range(1)
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
        },
        "sample_count": 3,
    }
    conn = sqlite3.connect(db_path)
    inserted_version = None
    template_id = None
    try:
        template_id = conn.execute(
            "SELECT id FROM prompt_template WHERE name = 'agent.visit_profile'"
        ).fetchone()[0]
        inserted_version = (
            conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 "
                "FROM prompt_template_version WHERE template_id = ?",
                (template_id,),
            ).fetchone()[0]
        )
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
    for kind, items in parsed.length_catalog.items():
        assert sum(item.weight for item in items) == 100.0
