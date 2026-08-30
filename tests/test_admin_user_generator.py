"""Tests for admin user & persona generator and agent auto-enrollment."""

from __future__ import annotations

import json
import random
import re

import pytest

from deaddit.extensions import db
from deaddit.models import Agent, Subdeaddit, User
from deaddit.services import persona_generator as pg
from deaddit.services.persona_generator import (
    PERSONA_BATCH_ATTEMPTS,
    PersonaGenerationError,
    generate_personas,
)
from deaddit.services.persona_options import PersonaAssignment


@pytest.fixture()
def admin_client(client):
    """Client authenticated for admin endpoints."""
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


#: Rich, well-formed rows echoing assignment IDs a1/a2. Demographics are
#: intentionally drifted by Phase 3 tests to ensure source assignments win.
SAMPLE_PERSONAS = [
    {
        "assignment_id": "a1",
        "username": "coffeecoder",
        "bio": "Espresso enthusiast building distributed systems.",
        "age": 29,
        "gender": "Male",
        "occupation": "Software Architect",
        "education": "M.S. Computer Science",
        "interests": ["specialty coffee", "rust", "rock climbing"],
        "personality_traits": ["analytical", "methodical", "curious"],
        "writing_style": "concise, direct, uses code snippets",
    },
    {
        "assignment_id": "a2",
        "username": "sarah_diy",
        "bio": "Restoring vintage motorcycles and woodworking in my garage.",
        "age": 34,
        "gender": "Female",
        "occupation": "Mechanical Engineer",
        "education": "B.S. Mechanical Engineering",
        "interests": ["woodworking", "motorcycles", "3d printing"],
        "personality_traits": ["practical", "resourceful", "encouraging"],
        "writing_style": "friendly, detailed instructions, bullet points",
    },
]
SAMPLE_PERSONAS_JSON = json.dumps(SAMPLE_PERSONAS)


def _persona_row(assignment_id: str, username: str, **overrides) -> dict:
    """A minimal but valid LLM row that echoes its assignment ID."""
    row = {
        "assignment_id": assignment_id,
        "username": username,
        "bio": "A bio",
        "age": 30,
        "gender": "Male",
    }
    row.update(overrides)
    return row


def _personas_json(n: int, start: int = 1) -> str:
    """A JSON array of ``n`` rows echoing consecutive assignment IDs."""
    return json.dumps(
        [_persona_row(f"a{i}", f"user_{i}") for i in range(start, start + n)]
    )


def _fixed_assignment(index: int, *, troll: bool = False) -> PersonaAssignment:
    """Deterministic assignment row with per-index-unique prompt facts."""
    return PersonaAssignment(
        id=f"a{index}",
        age=30 + index,
        age_band_id="age.25_34",
        occupation_id=f"occupation.fixture_{index}",
        occupation=f"matrix job {index}",
        occupation_sector="sector.fixture",
        employment_context_id="context.full_time",
        employment_context="full-time",
        education_level_id="education.bachelor",
        education=f"degree {index}",
        trait_ids=(
            "trait.fixture_1",
            "trait.fixture_2",
            "trait.fixture_3",
            "trait.fixture_4",
        ),
        traits=(f"trait one {index}", "blunt", "methodical", f"quirk {index}"),
        writing_style_id="style.fixture",
        writing_style=f"style card {index}",
        interest_seeds=(f"seed {index}a", f"seed {index}b"),
        troll_modifier_id="troll.pedantic" if troll else None,
        troll_modifier="pedantic" if troll else None,
        username_style=f"username style card {index}",
    )


def _fixture_assignments(count: int, troll_count: int = 0) -> tuple:
    """Rows a1..aN: normals first, then exactly ``troll_count`` troll rows."""
    normals = count - troll_count
    rows = [_fixed_assignment(i) for i in range(1, normals + 1)]
    rows += [
        _fixed_assignment(normals + k, troll=True) for k in range(1, troll_count + 1)
    ]
    return tuple(rows)


def _install_fixed_plan(monkeypatch, count: int, troll_count: int = 0) -> tuple:
    """Freeze planning: known rows, styles drawn from the same fixture."""
    rows = _fixture_assignments(count, troll_count)
    monkeypatch.setattr(pg, "build_persona_assignments", lambda *a, **k: rows)
    monkeypatch.setattr(
        pg,
        "_assign_styles",
        lambda n, rng=None: [f"username style card {i}" for i in range(1, n + 1)],
    )
    return rows


def _prompt_ids(prompt: str) -> list[str]:
    """Assignment IDs of the matrix rows rendered into a prompt."""
    return re.findall(r"\[assignment_id (a\d+)\]", prompt)


def _prompt(request_record) -> str:
    return request_record["payload"]["messages"][1]["content"]


class TestPersonaGeneratorService:
    def test_generate_personas_creates_users_and_agents(self, app, fake_llm):
        fake_llm.enqueue_content(SAMPLE_PERSONAS_JSON)

        result = generate_personas(
            count=2,
            topic_hint="makers and coders",
            auto_create_agent=True,
            tier="power_user",
            troll_mode="no_troll",
        )

        assert len(result["users"]) == 2
        assert len(result["agents"]) == 2

        # Casing post-treatment may alter the LLM username; use stored values
        name1, name2 = (u["username"] for u in result["users"])

        u1 = User.query.filter(db.func.lower(User.username) == name1.lower()).first()
        assert 18 <= u1.age <= 99
        assert u1.gender == "Male"
        assert u1.occupation
        assert u1.education
        assert u1.writing_style
        assert 4 <= len(u1.get_personality_traits()) <= 6
        assert u1.agent_state["persona_seed"]["assignment_id"] == "a1"

        u2 = User.query.filter(db.func.lower(User.username) == name2.lower()).first()
        assert u2.gender == "Female"
        assert u2.agent_state["persona_seed"]["assignment_id"] == "a2"

        # Verify Agent auto-enrollment with default config requirements
        a1 = Agent.query.filter(
            db.func.lower(Agent.user_username) == name1.lower()
        ).first()
        assert a1.autonomy_tier == "power_user"
        assert a1.persona_mode == "fixed"
        assert a1.is_enabled is True
        assert a1.status == "idle"
        assert a1.next_run_at is not None
        assert a1.config["max_actions_per_run"] == 30
        assert a1.config["min_delay"] == 300
        assert a1.config["max_delay"] == 1800
        assert "api_url" in a1.config
        assert "model" in a1.config

        a2 = Agent.query.filter(
            db.func.lower(Agent.user_username) == name2.lower()
        ).first()
        assert a2.autonomy_tier == "power_user"
        assert a2.persona_mode == "fixed"
        assert a2.is_enabled is True

    def test_generate_personas_without_agent_creation(self, app, fake_llm):
        fake_llm.enqueue_content(json.dumps([SAMPLE_PERSONAS[0]]))

        result = generate_personas(
            count=1,
            auto_create_agent=False,
            troll_mode="no_troll",
        )
        assert len(result["users"]) == 1
        assert len(result["agents"]) == 0

        name1 = result["users"][0]["username"]
        u1 = User.query.filter(db.func.lower(User.username) == name1.lower()).first()
        assert u1 is not None
        assert (
            Agent.query.filter(
                db.func.lower(Agent.user_username) == name1.lower()
            ).first()
            is None
        )

    def test_generate_personas_handles_codeblock_wrapping(self, app, fake_llm):
        wrapped = f"```json\n{SAMPLE_PERSONAS_JSON}\n```"
        fake_llm.enqueue_content(wrapped)

        result = generate_personas(
            count=2, auto_create_agent=True, tier="regular", troll_mode="no_troll"
        )
        assert len(result["users"]) == 2
        assert len(result["agents"]) == 2
        assert result["agents"][0]["autonomy_tier"] == "regular"

    def test_generate_personas_handles_duplicate_usernames(self, app, fake_llm):
        # Seed an existing user with username "coffeecoder"
        existing = User(
            username="coffeecoder",
            bio="Original coffee coder",
            age=40,
            gender="Male",
        )
        db.session.add(existing)
        db.session.commit()

        fake_llm.enqueue_content(SAMPLE_PERSONAS_JSON)

        result = generate_personas(
            count=2, auto_create_agent=True, troll_mode="no_troll"
        )
        usernames = [u["username"] for u in result["users"]]
        # Case-insensitive: suffixed username, never a case-variant collision
        # Casing is applied after suffixing, so the underscore may be
        # absorbed by PascalCase/camelCase (e.g. Coffeecoder1)
        assert any(
            u.lower().startswith("coffeecoder") and u.lower() != "coffeecoder"
            for u in usernames
        )

    def test_count_and_tier_validation_limits(self, app):
        with pytest.raises(ValueError, match="between 1 and 500"):
            generate_personas(count=0)

        with pytest.raises(ValueError, match="between 1 and 500"):
            generate_personas(count=501)

        with pytest.raises(ValueError, match="Invalid tier"):
            generate_personas(count=2, tier="invalid_tier")

    def test_all_batches_failing_still_raises(self, app, fake_llm):
        # Every batch gets PERSONA_BATCH_ATTEMPTS tries; when not a single
        # persona can be created the run still reports a hard failure.
        for _ in range(PERSONA_BATCH_ATTEMPTS):
            fake_llm.enqueue_content("Sorry, I cannot help with this request.")
        with pytest.raises(PersonaGenerationError):
            generate_personas(count=1)
        assert len(fake_llm.requests) == PERSONA_BATCH_ATTEMPTS

    def test_failing_batch_is_retried_then_skipped(self, app, fake_llm, monkeypatch):
        # Batch 1 (10 personas) succeeds; batch 2 (2 personas) burns all its
        # attempts -> the run skips it and still returns the 10 created.
        _install_fixed_plan(monkeypatch, count=12)
        fake_llm.enqueue_content(_personas_json(10))
        for _ in range(PERSONA_BATCH_ATTEMPTS):
            fake_llm.enqueue_content("not json at all")
        result = generate_personas(
            count=12, auto_create_agent=False, troll_mode="no_troll"
        )
        assert len(result["users"]) == 10
        assert result["skipped"] == 2
        assert User.query.count() == 10
        assert len(fake_llm.requests) == 1 + PERSONA_BATCH_ATTEMPTS
        # Every failed retry re-prompts exactly the unresolved batch rows
        retry_prompts = [_prompt(r) for r in fake_llm.requests[1:]]
        assert all(_prompt_ids(p) == ["a11", "a12"] for p in retry_prompts)

    def test_username_style_assignments_in_prompt(self, app, fake_llm, monkeypatch):
        from deaddit.services.persona_options import USERNAME_STYLES

        fake_llm.enqueue_content(_personas_json(2))

        # Freeze the full-request style draw so the two personas
        # demonstrably get the first two catalog cards
        cards = [style.text for style in USERNAME_STYLES]
        monkeypatch.setattr(pg, "_assign_styles", lambda n, rng=None: cards[:n])
        generate_personas(count=2, auto_create_agent=False, troll_mode="no_troll")

        prompt = _prompt(fake_llm.requests[-1])
        styles = re.findall(r"- username style: (.+)", prompt)
        assert len(styles) == 2
        assert styles == cards[:2]
        assert set(styles) <= {style.text for style in USERNAME_STYLES}
        # Anti-pattern ban is present
        assert "chill_dude" in prompt
        assert pg.USERNAME_STYLE_RULES in prompt

    def test_style_wrapper_runs_once_per_full_request(self, app, fake_llm, monkeypatch):
        # The style draw is a single full-request call, not a per-batch or
        # per-retry draw: row 11 keeps style 11 in its own remainder batch.
        _install_fixed_plan(monkeypatch, count=12)
        calls: list[int] = []

        def fake_styles(n, rng=None):
            calls.append(n)
            return [f"style card {i}" for i in range(1, n + 1)]

        monkeypatch.setattr(pg, "_assign_styles", fake_styles)
        fake_llm.enqueue_content(_personas_json(10))
        fake_llm.enqueue_content(_personas_json(2, start=11))
        result = generate_personas(
            count=12, auto_create_agent=False, troll_mode="no_troll"
        )

        assert calls == [12]
        assert len(result["users"]) == 12
        prompts = [_prompt(r) for r in fake_llm.requests]
        assert "- username style: style card 11" in prompts[1]
        assert "- username style: style card 11" not in prompts[0]

    def test_persona_generation_max_tokens(self, app, fake_llm):
        fake_llm.enqueue_content(json.dumps([SAMPLE_PERSONAS[0]]))
        generate_personas(count=1, auto_create_agent=False, troll_mode="no_troll")
        assert fake_llm.requests[-1]["payload"]["max_tokens"] == 16384

    def test_apply_casing_branches(self, monkeypatch):
        from deaddit.services.persona_generator import _apply_casing

        monkeypatch.setattr(random, "random", lambda: 0.1)
        assert _apply_casing("pm_me_your_taco") == "PmMeYourTaco"
        monkeypatch.setattr(random, "random", lambda: 0.3)
        assert _apply_casing("pm_me_your_taco") == "pmMeYourTaco"
        monkeypatch.setattr(random, "random", lambda: 0.9)
        assert _apply_casing("pm_me_your_taco") == "pm_me_your_taco"

    def test_case_insensitive_dedupe_against_db(self, app, fake_llm):
        existing = User(
            username="coffeecoder",
            bio="Original coffee coder",
            age=40,
            gender="Male",
        )
        db.session.add(existing)
        db.session.commit()

        variant = dict(SAMPLE_PERSONAS[0])
        variant["username"] = "CoffeeCoder"
        fake_llm.enqueue_content(json.dumps([variant]))

        result = generate_personas(
            count=1, auto_create_agent=False, troll_mode="no_troll"
        )
        created = result["users"][0]["username"]
        assert created.lower() != "coffeecoder"
        # Casing may absorb the underscore (e.g. Coffeecoder1)
        assert created.lower().startswith("coffeecoder")


class TestMatrixPromptAndIdResolution:
    """Phase 2: numbered matrix prompt and ID-keyed response mapping."""

    def test_prompt_renders_only_active_catalog_rows(self, app, fake_llm):
        import deaddit.services.persona_options as po

        fake_llm.enqueue_content(_personas_json(2))
        generate_personas(count=2, auto_create_agent=False, troll_mode="no_troll")

        prompt = _prompt(fake_llm.requests[-1])
        assert _prompt_ids(prompt) == ["a1", "a2"]
        # Rendered occupations and traits are assigned catalog entries, so
        # no unassigned label or invented string can appear
        occupations = re.findall(r"- occupation: (.+?) \(", prompt)
        assert len(occupations) == 2
        assert set(occupations) <= {o.label for o in po.OCCUPATIONS}
        traits = [
            trait.strip()
            for line in re.findall(r"- required traits: (.+)", prompt)
            for trait in line.split(";")
        ]
        assert set(traits) <= {t.text for t in po.TRAITS}
        # The demographic JSON example anchor is gone and reuse is banned
        assert "example_user" not in prompt
        assert "Never copy any example username, phrase, or name" in prompt
        assert "Rows are not interchangeable" in prompt
        assert "exactly the occupation" in prompt

    def test_each_batch_prompt_renders_only_its_rows(self, app, fake_llm, monkeypatch):
        _install_fixed_plan(monkeypatch, count=12)
        fake_llm.enqueue_content(_personas_json(10))
        fake_llm.enqueue_content(_personas_json(2, start=11))
        result = generate_personas(
            count=12, auto_create_agent=False, troll_mode="no_troll"
        )

        assert len(result["users"]) == 12
        prompts = [_prompt(r) for r in fake_llm.requests]
        assert _prompt_ids(prompts[0]) == [f"a{i}" for i in range(1, 11)]
        assert _prompt_ids(prompts[1]) == ["a11", "a12"]
        # Unassigned rows never leak into another batch's prompt
        assert "- occupation: matrix job 11 (" not in prompts[0]
        assert "- occupation: matrix job 1 (" not in prompts[1]
        assert "- occupation: matrix job 10 (" not in prompts[1]

    def test_rows_pair_by_assignment_id_not_position(self, app, fake_llm, monkeypatch):
        # Returned rows arrive reversed: demographics follow the ID, while
        # source-owned fields come from the matching assignment.
        assignments = _install_fixed_plan(monkeypatch, count=2)
        fake_llm.enqueue_content(
            json.dumps(
                [
                    _persona_row("a2", "user_two", age=44),
                    _persona_row("a1", "user_one", age=33),
                ]
            )
        )
        result = generate_personas(
            count=2, auto_create_agent=False, troll_mode="no_troll"
        )

        by_key = {u["username"].replace("_", "").lower(): u for u in result["users"]}
        assert by_key["userone"]["age"] == assignments[0].age
        assert by_key["usertwo"]["age"] == assignments[1].age

    def test_missing_ids_are_retried_without_rerolling(
        self, app, fake_llm, monkeypatch
    ):
        _install_fixed_plan(monkeypatch, count=2)
        # Attempt 1 resolves only a1; attempt 2 finally returns a2.
        fake_llm.enqueue_content(json.dumps([_persona_row("a1", "user_one")]))
        fake_llm.enqueue_content(json.dumps([_persona_row("a2", "user_two")]))

        result = generate_personas(
            count=2, auto_create_agent=False, troll_mode="no_troll"
        )

        assert len(result["users"]) == 2
        assert result["skipped"] == 0
        assert len(fake_llm.requests) == 2
        prompts = [_prompt(r) for r in fake_llm.requests]
        assert _prompt_ids(prompts[0]) == ["a1", "a2"]
        # The retry prompts only the unresolved row with unchanged facts
        assert _prompt_ids(prompts[1]) == ["a2"]
        assert "- occupation: matrix job 2 (full-time)" in prompts[0]
        assert "- occupation: matrix job 2 (full-time)" in prompts[1]
        assert "- username style: username style card 2" in prompts[0]
        assert "- username style: username style card 2" in prompts[1]
        for fact in (
            "- age: 32",
            "- occupation: matrix job 2 (full-time)",
            '- education: "degree 2"',
            "- required traits: trait one 2; blunt; methodical; quirk 2",
            '- writing style: "style card 2"',
            "- interest seeds: seed 2a; seed 2b",
            "- username style: username style card 2",
        ):
            assert fact in prompts[0]
            assert fact in prompts[1]
        assert "matrix job 1" not in prompts[1]
        assert "degree 1" not in prompts[1]
        assert "seed 1a" not in prompts[1]
        assert User.query.count() == 2

    def test_partial_resolution_keeps_partial_success_contract(
        self, app, fake_llm, monkeypatch
    ):
        _install_fixed_plan(monkeypatch, count=2)
        fake_llm.enqueue_content(json.dumps([_persona_row("a1", "user_one")]))
        fake_llm.enqueue_content("not json")
        fake_llm.enqueue_content("still not json")

        result = generate_personas(
            count=2, auto_create_agent=False, troll_mode="no_troll"
        )

        assert len(result["users"]) == 1
        assert result["skipped"] == 1
        assert User.query.count() == 1

    def test_duplicate_assignment_ids_resolve_once(self, app, fake_llm):
        fake_llm.enqueue_content(
            json.dumps(
                [
                    _persona_row("a1", "user_one"),
                    _persona_row("a1", "squatting_duplicate"),
                    _persona_row("a2", "user_two"),
                ]
            )
        )
        result = generate_personas(
            count=2, auto_create_agent=False, troll_mode="no_troll"
        )

        assert len(result["users"]) == 2
        assert result["skipped"] == 0
        # The first row for an ID wins; the duplicate never creates a user
        assert len(fake_llm.requests) == 1
        assert User.query.count() == 2

    def test_unknown_ids_are_ignored_not_positionally_mapped(
        self, app, fake_llm, monkeypatch
    ):
        assignments = _install_fixed_plan(monkeypatch, count=2)
        fake_llm.enqueue_content(
            json.dumps(
                [
                    _persona_row("a99", "ghost_row", age=66),
                    _persona_row("a1", "user_one", age=33),
                ]
            )
        )
        fake_llm.enqueue_content(json.dumps([_persona_row("a2", "user_two", age=44)]))

        result = generate_personas(
            count=2, auto_create_agent=False, troll_mode="no_troll"
        )

        by_key = {u["username"].replace("_", "").lower(): u for u in result["users"]}
        # a99's payload never lands on a1 by position; each row uses its ID.
        assert by_key["userone"]["age"] == assignments[0].age
        assert by_key["usertwo"]["age"] == assignments[1].age
        assert "ghostrow" not in by_key
        assert User.query.count() == 2

    def test_response_without_assignment_ids_is_not_a_success(self, app, fake_llm):
        # Well-formed persona JSON lacking assignment_id can no longer
        # create anyone: every row stays unresolved until the run fails.
        legacy = json.dumps(
            [
                {"username": "legacy_one", "bio": "A bio", "age": 30, "gender": "Male"},
                {"username": "legacy_two", "bio": "A bio", "age": 31, "gender": "Male"},
            ]
        )
        for _ in range(PERSONA_BATCH_ATTEMPTS):
            fake_llm.enqueue_content(legacy)

        with pytest.raises(PersonaGenerationError):
            generate_personas(count=2, auto_create_agent=False, troll_mode="no_troll")

        assert len(fake_llm.requests) == PERSONA_BATCH_ATTEMPTS
        assert User.query.count() == 0

    def test_topic_hint_is_an_interest_lens_only(self, app, fake_llm):
        fake_llm.enqueue_content(_personas_json(2))
        generate_personas(
            count=2,
            topic_hint="coffee",
            auto_create_agent=False,
            troll_mode="no_troll",
        )

        prompt = _prompt(fake_llm.requests[-1])
        assert "Interest lens" in prompt
        assert "coffee" in prompt
        assert "interests only" in prompt
        # The old whole-population phrasing is gone
        assert "should relate to" not in prompt


@pytest.mark.llm_live
class TestUsernameDiversityLive:
    """Hits the configured LLM endpoint; excluded from deterministic runs."""

    def test_username_diversity_across_batches(self, app):
        from deaddit.services.persona_generator import generate_personas

        all_names: list[str] = []
        for _ in range(3):
            result = generate_personas(
                count=10, auto_create_agent=False, troll_mode="no_troll"
            )
            all_names.extend(u["username"] for u in result["users"])

        lowers = [n.lower() for n in all_names]
        assert len(set(lowers)) >= 0.9 * len(lowers)

        def shape(n: str) -> str:
            if any(c.isdigit() for c in n):
                return "digits"
            if n != n.lower():
                return "mixed"
            if n.count("_") >= 2:
                return "phrase"
            return "simple"

        assert len({shape(n) for n in all_names}) >= 3


class TestAuthoritativePersonaMerge:
    def test_assignment_facts_traits_and_interest_fallback_win(
        self, app, fake_llm, monkeypatch
    ):
        assignments = _install_fixed_plan(monkeypatch, count=1)
        monkeypatch.setattr(pg, "_apply_casing", lambda name: name)
        fake_llm.enqueue_content(
            json.dumps(
                [
                    _persona_row(
                        "a1",
                        "drifted_user",
                        age=99,
                        occupation="Unassigned occupation",
                        education="Unassigned education",
                        writing_style="Unassigned style",
                        interests=[],
                        personality_traits=[
                            "BLUNT",
                            "new trait",
                            "new trait",
                            "another trait",
                            "ignored trait",
                        ],
                    )
                ]
            )
        )

        result = generate_personas(
            count=1, auto_create_agent=False, troll_mode="no_troll"
        )
        user = User.query.filter_by(username=result["users"][0]["username"]).first()
        assignment = assignments[0]

        assert user.age == assignment.age
        assert user.occupation == assignment.occupation
        assert user.education == assignment.education
        assert user.writing_style == assignment.writing_style
        assert user.get_interests() == list(assignment.interest_seeds)
        assert user.get_personality_traits() == [
            "trait one 1",
            "blunt",
            "methodical",
            "quirk 1",
            "new trait",
            "another trait",
        ]

        seed = user.agent_state["persona_seed"]
        assert seed["catalog_version"] == pg.PERSONA_CATALOG_VERSION
        assert seed["assignment_id"] == assignment.id
        assert seed["age_band_id"] == assignment.age_band_id
        assert seed["occupation_id"] == assignment.occupation_id
        assert seed["employment_context_id"] == assignment.employment_context_id
        assert seed["education_level_id"] == assignment.education_level_id
        assert seed["trait_ids"] == list(assignment.trait_ids)
        assert seed["writing_style_id"] == assignment.writing_style_id
        assert seed == {
            "catalog_version": pg.PERSONA_CATALOG_VERSION,
            "assignment_id": assignment.id,
            "age_band_id": assignment.age_band_id,
            "occupation_id": assignment.occupation_id,
            "occupation_sector": assignment.occupation_sector,
            "employment_context_id": assignment.employment_context_id,
            "education_level_id": assignment.education_level_id,
            "trait_ids": list(assignment.trait_ids),
            "writing_style_id": assignment.writing_style_id,
            "interest_seed_ids": [None, None],
            "troll_modifier_id": None,
            "username_style_id": None,
        }
        assert user.agent_state == {"persona_seed": seed, "subscriptions": []}

    @pytest.mark.parametrize("auto_create_agent", [False, True])
    def test_provenance_commit_with_or_without_agent(
        self, app, fake_llm, monkeypatch, auto_create_agent
    ):
        assignments = _install_fixed_plan(monkeypatch, count=1)
        monkeypatch.setattr(pg, "_apply_casing", lambda name: name)
        fake_llm.enqueue_content(
            json.dumps([_persona_row("a1", "committed_user", subscriptions=[])])
        )

        result = generate_personas(
            count=1,
            auto_create_agent=auto_create_agent,
            troll_mode="no_troll",
        )
        user = User.query.filter_by(username=result["users"][0]["username"]).first()

        assert user.agent_state["subscriptions"] == []
        assert user.agent_state["persona_seed"]["assignment_id"] == assignments[0].id
        assert len(result["agents"]) == int(auto_create_agent)


class TestAdminUserGeneratorAPI:
    def test_unauthenticated_request_rejected(self, client, monkeypatch):
        monkeypatch.setenv("API_TOKEN", "unit-test-admin-token")
        resp = client.post(
            "/admin/api/users/generate",
            json={"count": 2, "auto_create_agent": True, "tier": "regular"},
        )
        assert resp.status_code == 302
        assert "/admin/login" in resp.headers.get("Location", "")

    def test_api_validation_errors(self, admin_client):
        # Count too low
        resp = admin_client.post("/admin/api/users/generate", json={"count": 0})
        assert resp.status_code == 400
        assert "between 1 and 500" in resp.get_json()["error"]

        # Count too high
        resp = admin_client.post("/admin/api/users/generate", json={"count": 501})
        assert resp.status_code == 400
        assert "between 1 and 500" in resp.get_json()["error"]

        # Non-integer count
        resp = admin_client.post("/admin/api/users/generate", json={"count": "three"})
        assert resp.status_code == 400

        # Invalid tier
        resp = admin_client.post(
            "/admin/api/users/generate",
            json={"count": 2, "tier": "ultra_admin"},
        )
        assert resp.status_code == 400
        assert "Unknown tier" in resp.get_json()["error"]

    def test_api_generate_success(self, admin_client, fake_llm):
        fake_llm.enqueue_content(SAMPLE_PERSONAS_JSON)

        payload = {
            "count": 2,
            "auto_create_agent": True,
            "tier": "lurker",
            "topic_hint": "coffee lovers",
            "troll_mode": "no_troll",
        }
        resp = admin_client.post("/admin/api/users/generate", json=payload)
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["success"] is True
        assert set(data) == {"success", "users", "agents", "skipped"}
        assert len(data["users"]) == 2
        assert len(data["agents"]) == 2
        assert all(
            "assignment_id" not in user and "persona_seed" not in user
            for user in data["users"]
        )

        # Verify agent tier and config
        for agent in data["agents"]:
            assert agent["autonomy_tier"] == "lurker"
            assert agent["is_enabled"] is True
            assert agent["config"]["max_actions_per_run"] == 30
            assert agent["config"]["min_delay"] == 300
            assert agent["config"]["max_delay"] == 1800

        # Verify LLM request included topic hint
        assert len(fake_llm.requests) == 1
        prompt = _prompt(fake_llm.requests[0])
        assert "coffee lovers" in prompt

    def test_api_generate_without_agents(self, admin_client, fake_llm):
        fake_llm.enqueue_content(json.dumps([SAMPLE_PERSONAS[0]]))

        payload = {
            "count": 1,
            "auto_create_agent": False,
            "troll_mode": "no_troll",
        }
        resp = admin_client.post("/admin/api/users/generate", json=payload)
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["users"]) == 1
        assert len(data["agents"]) == 0

    def test_api_generate_defaults_to_personas_only(self, admin_client, fake_llm):
        """Omitting auto_create_agent must NOT enroll agents (admin UI default)."""
        fake_llm.enqueue_content(json.dumps([SAMPLE_PERSONAS[0]]))

        resp = admin_client.post(
            "/admin/api/users/generate",
            json={"count": 1, "troll_mode": "no_troll"},
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["users"]) == 1
        assert data["agents"] == []
        assert Agent.query.count() == 0

    def test_generate_personas_batches_over_ten(self, app, fake_llm, monkeypatch):
        # Two batches: rows a1/a2, then the remainder row a3
        _install_fixed_plan(monkeypatch, count=3)
        fake_llm.enqueue_content(_personas_json(2))
        fake_llm.enqueue_content(_personas_json(1, start=3))
        result = generate_personas(
            count=3, auto_create_agent=False, troll_mode="no_troll"
        )
        assert len(result["users"]) == 3


class TestPersonaSubscriptions:
    """Creation-time LLM-picked subscriptions (initial condition for the
    subscription graph; validated against real communities, never forced)."""

    @pytest.fixture()
    def subs(self, app):
        rows = [
            Subdeaddit(name="books", description="Books, authors, literature."),
            Subdeaddit(name="CasualConversation", description="Casual talk."),
            Subdeaddit(name="localllama", description="Local LLMs."),
        ]
        db.session.add_all(rows)
        db.session.commit()
        return rows

    @staticmethod
    def _persona(assignment_id, username, subscriptions):
        return {
            "assignment_id": assignment_id,
            "username": username,
            "bio": "A bio",
            "age": 30,
            "gender": "Male",
            "subscriptions": subscriptions,
        }

    def test_subscriptions_validated_and_persisted(self, app, fake_llm, subs):
        fake_llm.enqueue_content(
            json.dumps(
                [
                    # Case canonicalization, ghost drop, dedupe.
                    self._persona("a1", "user_a", ["Books", "askdaddit", "books"]),
                    # Cap at 3: nosleep is a ghost, the rest survive.
                    self._persona(
                        "a2",
                        "user_b",
                        ["localllama", "CasualConversation", "books", "nosleep"],
                    ),
                    # Comma-separated string form with one ghost.
                    self._persona("a3", "user_c", "books, quietthoughts"),
                ]
            )
        )

        result = generate_personas(
            count=3, auto_create_agent=False, troll_mode="no_troll"
        )

        # Casing post-treatment may alter the LLM username; match ignoring
        # case and underscores ("user_a" may be stored as "UserA").
        by_key = {u["username"].replace("_", "").lower(): u for u in result["users"]}
        assert by_key["usera"]["subscriptions"] == ["books"]
        assert by_key["userb"]["subscriptions"] == [
            "localllama",
            "CasualConversation",
            "books",
        ]
        assert by_key["userc"]["subscriptions"] == ["books"]

        row_a = User.query.filter_by(username=by_key["usera"]["username"]).first()
        row_b = User.query.filter_by(username=by_key["userb"]["username"]).first()
        assert row_a.agent_state["subscriptions"] == ["books"]
        assert row_a.agent_state["persona_seed"]["assignment_id"] == "a1"
        assert row_b.agent_state["subscriptions"] == [
            "localllama",
            "CasualConversation",
            "books",
        ]

    def test_missing_or_empty_subscriptions_stay_empty(self, app, fake_llm, subs):
        fake_llm.enqueue_content(
            json.dumps(
                [
                    self._persona("a1", "user_d", ["ghostsub", "another_ghost"]),
                    _persona_row("a2", "user_e"),
                ]
            )
        )

        result = generate_personas(
            count=2, auto_create_agent=False, troll_mode="no_troll"
        )

        by_key = {u["username"].replace("_", "").lower(): u for u in result["users"]}
        assert by_key["userd"]["subscriptions"] == []
        assert by_key["usere"]["subscriptions"] == []
        for key in ("userd", "usere"):
            row = User.query.filter_by(username=by_key[key]["username"]).first()
            assert row.agent_state["subscriptions"] == []
            assert row.agent_state["persona_seed"]["assignment_id"] in {"a1", "a2"}

    def test_prompt_lists_real_communities(self, app, fake_llm, subs):
        fake_llm.enqueue_content(_personas_json(1))
        generate_personas(count=1, auto_create_agent=False, troll_mode="no_troll")

        prompt = _prompt(fake_llm.requests[-1])
        assert "The forum currently has these communities" in prompt
        assert "- books: Books, authors, literature." in prompt
        assert '- "subscriptions"' in prompt

    def test_prompt_omits_community_section_without_subs(self, app, fake_llm):
        fake_llm.enqueue_content(_personas_json(1))
        generate_personas(count=1, auto_create_agent=False, troll_mode="no_troll")

        prompt = _prompt(fake_llm.requests[-1])
        assert "communities" not in prompt
        assert '"subscriptions"' not in prompt

    def test_agents_created_with_subscribed_users(self, app, fake_llm, subs):
        fake_llm.enqueue_content(
            json.dumps([self._persona("a1", "user_f", ["books", "localllama"])])
        )
        result = generate_personas(
            count=1, auto_create_agent=True, troll_mode="no_troll"
        )
        assert len(result["agents"]) == 1
        stored = result["users"][0]["username"]
        row = User.query.filter_by(username=stored).first()
        assert row.agent_state["subscriptions"] == ["books", "localllama"]
        assert row.agent_state["persona_seed"]["assignment_id"] == "a1"
