"""Deterministic Phase 1 acceptance gates for the pure persona planner."""

import random
from collections import Counter, defaultdict
from dataclasses import replace
from math import ceil, floor

import pytest

from deaddit.services.persona_options import (
    AGE_BANDS,
    CONTRADICTING_TRAIT_PAIRS,
    EDUCATION_LEVEL_TARGETS,
    EDUCATION_LEVELS,
    EMPLOYMENT_CONTEXTS,
    INTEREST_DOMAINS,
    INTERESTS,
    LEGACY_OCCUPATION_ALIASES,
    OCCUPATIONS,
    SECTOR_LABELS,
    SECTOR_RELATED_DOMAINS,
    SECTORS,
    TRAIT_AXES,
    TRAITS,
    TROLL_MODIFIERS,
    USERNAME_STYLES,
    WRITING_STYLES,
    ExistingUserSnapshot,
    build_persona_assignments,
    validate_assignment,
)

_SECTOR_IDS = {sector.id for sector in SECTORS}
_BAND_BY_ID = {band.id: band for band in AGE_BANDS}
_LEVEL_BY_ID = {level.id: level for level in EDUCATION_LEVELS}
_CONTEXT_BY_ID = {context.id: context for context in EMPLOYMENT_CONTEXTS}
_STYLE_BY_ID = {style.id: style for style in WRITING_STYLES}
_TRAIT_BY_ID = {trait.id: trait for trait in TRAITS}
_DOMAIN_IDS = set(INTEREST_DOMAINS)


def _build(count, troll_count, seed, existing=None):
    return build_persona_assignments(count, troll_count, existing, random.Random(seed))


def _assert_valid(plan):
    for assignment in plan:
        assert validate_assignment(assignment) == ()


def _trait_ids_with_axes(axis_count=4, include_limitation=None):
    """Return valid trait IDs from distinct axes, optionally requiring a limitation."""
    selected = []
    for axis in TRAIT_AXES:
        candidates = [trait for trait in TRAITS if trait.axis == axis]
        if include_limitation is True:
            candidates = [trait for trait in candidates if trait.limitation]
        elif include_limitation is False:
            candidates = [trait for trait in candidates if not trait.limitation]
        if candidates:
            selected.append(candidates[0].id)
        if len(selected) == axis_count:
            break
    return tuple(selected)


def test_catalog_sizes_and_integrity():
    assert len(AGE_BANDS) == 6
    assert len({band.id for band in AGE_BANDS}) == len(AGE_BANDS)
    assert AGE_BANDS[0].low == 18
    assert AGE_BANDS[-1].high == 75
    assert all(
        left.high + 1 == right.low
        for left, right in zip(AGE_BANDS, AGE_BANDS[1:], strict=False)
    )
    assert sum(band.target for band in AGE_BANDS) == pytest.approx(1.0)

    assert len(EDUCATION_LEVELS) == 9
    assert len({level.id for level in EDUCATION_LEVELS}) == len(EDUCATION_LEVELS)
    assert sum(EDUCATION_LEVEL_TARGETS.values()) == pytest.approx(1.0)

    assert len(EMPLOYMENT_CONTEXTS) == 11
    assert len({context.id for context in EMPLOYMENT_CONTEXTS}) == len(
        EMPLOYMENT_CONTEXTS
    )
    assert len(SECTORS) == 16
    assert set(SECTOR_LABELS) == _SECTOR_IDS
    assert len(SECTOR_LABELS) == len(SECTORS)
    occupations_by_sector = Counter(occupation.sector for occupation in OCCUPATIONS)
    assert len(OCCUPATIONS) >= 160
    assert len({occupation.id for occupation in OCCUPATIONS}) == len(OCCUPATIONS)
    assert len({occupation.label for occupation in OCCUPATIONS}) == len(OCCUPATIONS)
    assert set(occupations_by_sector) == _SECTOR_IDS
    assert all(occupations_by_sector[sector_id] >= 10 for sector_id in _SECTOR_IDS)

    assert len(TRAIT_AXES) == 8
    assert len(TRAITS) >= 96
    assert len({trait.id for trait in TRAITS}) == len(TRAITS)
    assert len({trait.text for trait in TRAITS}) == len(TRAITS)
    traits_by_axis = Counter(trait.axis for trait in TRAITS)
    assert set(traits_by_axis) == set(TRAIT_AXES)
    assert all(traits_by_axis[axis] >= 12 for axis in TRAIT_AXES)
    assert all(
        any(trait.axis == axis and trait.limitation for trait in TRAITS)
        for axis in TRAIT_AXES
    )

    assert len(WRITING_STYLES) >= 24
    assert len({style.id for style in WRITING_STYLES}) == len(WRITING_STYLES)
    assert len({style.family for style in WRITING_STYLES}) >= 4
    assert len(INTERESTS) >= 120
    assert len({interest.id for interest in INTERESTS}) == len(INTERESTS)
    assert len(INTEREST_DOMAINS) == 14
    interests_by_domain = Counter(interest.domain for interest in INTERESTS)
    assert set(interests_by_domain) <= _DOMAIN_IDS
    assert len(interests_by_domain) >= 13
    assert all(interests_by_domain[domain] >= 4 for domain in INTEREST_DOMAINS)

    assert len(TROLL_MODIFIERS) == 6
    assert len({modifier.id for modifier in TROLL_MODIFIERS}) == len(TROLL_MODIFIERS)
    assert len({modifier.text for modifier in TROLL_MODIFIERS}) == len(TROLL_MODIFIERS)
    assert all(modifier.id and modifier.text for modifier in TROLL_MODIFIERS)
    assert len(USERNAME_STYLES) == 5
    assert len({style.id for style in USERNAME_STYLES}) == len(USERNAME_STYLES)
    assert len({style.text for style in USERNAME_STYLES}) == len(USERNAME_STYLES)
    assert all(style.id and style.text for style in USERNAME_STYLES)


def test_display_string_bounds():
    assert all(1 <= len(occupation.label) <= 100 for occupation in OCCUPATIONS)
    assert all(
        1 <= len(option.text) <= 100
        for occupation in OCCUPATIONS
        for option in occupation.education_options
    )
    assert all(style.text for style in WRITING_STYLES)
    assert all(interest.text for interest in INTERESTS)
    assert all(trait.text for trait in TRAITS)


def test_catalog_cross_references():
    occupation_ids = {occupation.id for occupation in OCCUPATIONS}
    education_text_levels = {}
    for occupation in OCCUPATIONS:
        level_ids = {option.level_id for option in occupation.education_options}
        # veterinarian is the plan-mandated single-credential card ("D.V.M. only")
        assert len(occupation.education_options) >= (
            1 if occupation.id == "occupation.veterinarian" else 2
        )
        assert occupation.sector in _SECTOR_IDS
        assert occupation.allowed_contexts
        assert "context.full_time" in occupation.allowed_contexts
        assert all(
            context_id in _CONTEXT_BY_ID for context_id in occupation.allowed_contexts
        )
        assert occupation.min_age is None or occupation.min_age >= 18
        assert ("context.current_student" in occupation.allowed_contexts) == (
            "education.current_student" in level_ids
        )
        assert level_ids <= set(_LEVEL_BY_ID)
        for option in occupation.education_options:
            previous = education_text_levels.setdefault(option.text, option.level_id)
            assert previous == option.level_id

    trait_ids = set(_TRAIT_BY_ID)
    for pair in CONTRADICTING_TRAIT_PAIRS:
        assert len(pair) == 2
        assert set(pair) <= trait_ids

    assert set(SECTOR_RELATED_DOMAINS) <= _SECTOR_IDS
    assert all(
        len(domains) <= 5 and domains <= _DOMAIN_IDS
        for domains in SECTOR_RELATED_DOMAINS.values()
    )
    for alias, occupation_id in LEGACY_OCCUPATION_ALIASES.items():
        assert alias == " ".join(alias.lower().split())
        assert occupation_id in occupation_ids


def test_argument_validation():
    for invalid_count in (0, -1, 501):
        with pytest.raises(ValueError, match="count"):
            build_persona_assignments(invalid_count, 0, None, random.Random(0))
    with pytest.raises(ValueError, match="count"):
        build_persona_assignments(
            count=501,
            troll_count=0,
            existing_users=None,
            rng=random.Random(0),
        )
    with pytest.raises(ValueError, match="troll_count"):
        build_persona_assignments(1, -1, None, random.Random(0))
    with pytest.raises(ValueError, match="troll_count"):
        build_persona_assignments(2, 3, None, random.Random(0))

    assert len(_build(1, 0, 0)) == 1
    assert len(_build(500, 500, 0)) == 500


def test_determinism():
    plans = {}
    for seed in (0, 7, 42):
        first = _build(20, 3, seed)
        second = _build(20, 3, seed)
        assert first == second
        plans[seed] = tuple((row.occupation_id, row.age) for row in first)

    assert len(set(plans.values())) == len(plans)
    assert _build(1, 0, 99) == _build(1, 0, 99)


def test_seed_sweep_gates():
    counts = (1, 2, 10, 20, 50, 161, 500)
    for seed in range(100):
        for count in counts:
            troll_count = min(3, count)
            plan = _build(count, troll_count, seed)
            assert len(plan) == count
            assert {row.id for row in plan} == {f"a{i}" for i in range(1, count + 1)}

            trolls = [row for row in plan if row.troll_modifier_id is not None]
            non_trolls = [row for row in plan if row.troll_modifier_id is None]
            assert len(trolls) == troll_count
            assert all(row.troll_modifier is not None for row in trolls)
            assert all(row.troll_modifier is None for row in non_trolls)
            assert len({row.troll_modifier_id for row in trolls}) == min(troll_count, 6)

            _assert_valid(plan)
            assert len({frozenset(row.trait_ids) for row in plan}) == count

            band_counts = Counter(row.age_band_id for row in plan)
            sector_counts = Counter(row.occupation_sector for row in plan)
            level_counts = Counter(row.education_level_id for row in plan)
            assert all(
                _BAND_BY_ID[row.age_band_id].low
                <= row.age
                <= _BAND_BY_ID[row.age_band_id].high
                for row in plan
            )
            assert all(
                row.employment_context_id != "context.retired" or row.age >= 55
                for row in plan
            )
            assert all(
                row.employment_context_id != "context.apprentice" or row.age <= 45
                for row in plan
            )
            assert all(
                row.employment_context_id != "context.current_student"
                or row.education_level_id == "education.current_student"
                for row in plan
            )

            if count == 10:
                assert len({row.occupation_id for row in plan}) == 10
                assert len(sector_counts) >= 7
                assert len(band_counts) >= 5
                assert len(level_counts) >= 4
                assert (
                    len({_STYLE_BY_ID[row.writing_style_id].family for row in plan})
                    >= 4
                )
            elif count == 20:
                assert len(band_counts) == 6
                assert max(sector_counts.values()) <= floor(0.2 * count)
                assert len(level_counts) >= 6
                assert max(level_counts.values()) <= ceil(0.3 * count)
            elif count == 50:
                assert len(sector_counts) == 16
                assert len(band_counts) == 6
                assert len({row.occupation_id for row in plan}) == 50
            elif count in (161, 500):
                distinct_occupations = len({row.occupation_id for row in plan})
                assert distinct_occupations >= 160
                if count == 500:
                    assert distinct_occupations == 160
                assert max(sector_counts.values()) <= floor(0.2 * count)
                assert len(level_counts) >= 6
                assert max(level_counts.values()) <= ceil(0.3 * count)
                occupations_by_sector = defaultdict(list)
                for row in plan:
                    occupations_by_sector[row.occupation_sector].append(
                        row.occupation_id
                    )
                for sector_occupations in occupations_by_sector.values():
                    # Rows are shuffled after drawing, so output order is not
                    # bag order; the order-independent observable of the
                    # no-repeat-before-exhaustion rule is that a sector's rows
                    # cover every card before any card can appear twice.
                    assert len(set(sector_occupations)) == min(
                        len(sector_occupations), 10
                    )


def test_existing_users_deficit_and_legacy_mapping():
    technology_users = [
        ExistingUserSnapshot(
            persona_seed={"occupation_id": "occupation.software_engineer"}
        )
        for _ in range(40)
    ]
    with_technology_snapshot = _build(10, 0, 7, technology_users)
    without_technology_snapshot = _build(10, 0, 7)
    # pinned seed; verified by lead
    assert sum(
        row.occupation_sector == "sector.technology_and_digital"
        for row in with_technology_snapshot
    ) < sum(
        row.occupation_sector == "sector.technology_and_digital"
        for row in without_technology_snapshot
    )
    age_users = [ExistingUserSnapshot(age=30) for _ in range(60)]
    # count 13 makes the age.25_34 quota fractional (2.6) so the band deficit
    # affects remainder seats; round counts (e.g. 20) give an integral quota
    # where the floor already satisfies it and the deficit cannot bite.
    with_age_snapshot = _build(13, 0, 11, age_users)
    without_age_snapshot = _build(13, 0, 11)
    assert sum(row.age_band_id == "age.25_34" for row in with_age_snapshot) < sum(
        row.age_band_id == "age.25_34" for row in without_age_snapshot
    )
    _assert_valid(with_age_snapshot)
    bachelor_users = [
        ExistingUserSnapshot(persona_seed={"education_level_id": "education.bachelor"})
        for _ in range(100)
    ]
    with_bachelor_snapshot = _build(20, 0, 17, bachelor_users)
    without_bachelor_snapshot = _build(20, 0, 17)
    assert sum(
        row.education_level_id == "education.bachelor" for row in with_bachelor_snapshot
    ) < sum(
        row.education_level_id == "education.bachelor"
        for row in without_bachelor_snapshot
    )

    legacy_occupation = [
        ExistingUserSnapshot(occupation="Software Developer") for _ in range(40)
    ]
    provenance_occupation = [
        ExistingUserSnapshot(
            persona_seed={"occupation_id": "occupation.software_engineer"}
        )
        for _ in range(40)
    ]
    assert _build(10, 0, 7, legacy_occupation) == _build(
        10, 0, 7, provenance_occupation
    )

    _assert_valid(
        _build(10, 0, 7, [ExistingUserSnapshot(occupation="Software Developer")])
    )
    _assert_valid(
        _build(
            10,
            0,
            7,
            [
                ExistingUserSnapshot(occupation="Potion Brewer"),
                ExistingUserSnapshot(persona_seed={"occupation_id": 42}),
                ExistingUserSnapshot(persona_seed="nonsense"),
                ExistingUserSnapshot(age="old"),
            ],
        )
    )
    _assert_valid(
        _build(
            10,
            0,
            7,
            [
                ExistingUserSnapshot(education="Some college"),
                ExistingUserSnapshot(education="Bachelor's degree"),
                ExistingUserSnapshot(education="Unknown qualification"),
            ],
        )
    )


def test_sequential_batches_preserve_occupation_novelty():
    existing_users = []
    assignments = []
    for batch_index in range(5):
        batch = build_persona_assignments(
            count=10,
            troll_count=0,
            existing_users=existing_users,
            rng=random.Random(1300 + batch_index),
        )
        assignments.extend(batch)
        existing_users.extend(
            ExistingUserSnapshot(
                persona_seed={"occupation_id": assignment.occupation_id},
                occupation=assignment.occupation,
            )
            for assignment in batch
        )

    assert len({assignment.occupation_id for assignment in assignments}) >= 45


def test_validate_assignment_rejections():
    plan = _build(20, 2, 3)
    row = plan[0]
    band = _BAND_BY_ID[row.age_band_id]
    assert validate_assignment(row) == ()

    invalid = [replace(row, age=band.low - 1)]
    minimum_age_occupation = next(
        occupation for occupation in OCCUPATIONS if occupation.min_age is not None
    )
    minimum_option = minimum_age_occupation.education_options[0]
    invalid_age = (minimum_age_occupation.min_age or 18) - 1
    invalid.append(
        replace(
            row,
            age=invalid_age,
            occupation_id=minimum_age_occupation.id,
            occupation=minimum_age_occupation.label,
            occupation_sector=minimum_age_occupation.sector,
            education_level_id=minimum_option.level_id,
            education=minimum_option.text,
        )
    )

    retired = _CONTEXT_BY_ID["context.retired"]
    apprentice = _CONTEXT_BY_ID["context.apprentice"]
    student = _CONTEXT_BY_ID["context.current_student"]
    invalid.extend(
        [
            replace(
                row,
                employment_context_id=retired.id,
                employment_context=retired.label,
                age=40,
            ),
            replace(
                row,
                employment_context_id=apprentice.id,
                employment_context=apprentice.label,
                age=60,
            ),
            replace(
                row,
                employment_context_id=student.id,
                employment_context=student.label,
                education_level_id="education.some_college",
            ),
            replace(row, education="Wizardry degree"),
            replace(row, education="x" * 101),
        ]
    )

    contradiction_candidates = [
        pair
        for pair in sorted(
            CONTRADICTING_TRAIT_PAIRS, key=lambda value: tuple(sorted(value))
        )
        if len({_TRAIT_BY_ID[trait_id].axis for trait_id in pair}) == 2
    ]
    all_pairs = sorted(
        CONTRADICTING_TRAIT_PAIRS, key=lambda value: tuple(sorted(value))
    )
    contradiction = (contradiction_candidates or all_pairs)[0]
    contradiction_ids = tuple(sorted(contradiction))
    contradiction_axes = {_TRAIT_BY_ID[trait_id].axis for trait_id in contradiction_ids}
    fillers = [
        trait
        for trait in TRAITS
        if trait.axis not in contradiction_axes and trait.id not in contradiction
    ]
    contradictory_ids = contradiction_ids + tuple(
        trait.id for trait in fillers[: 4 - len(contradiction_ids)]
    )
    invalid.append(replace(row, trait_ids=contradictory_ids))

    repeated_axis_traits = []
    for axis in TRAIT_AXES[:2]:
        repeated_axis_traits.extend(
            [trait.id for trait in TRAITS if trait.axis == axis][:2]
        )
    invalid.append(replace(row, trait_ids=tuple(repeated_axis_traits)))
    invalid.append(
        replace(row, trait_ids=_trait_ids_with_axes(include_limitation=False))
    )

    same_domain = next(
        domain
        for domain in INTEREST_DOMAINS
        if sum(interest.domain == domain for interest in INTERESTS) >= 2
    )
    same_domain_seeds = tuple(
        interest.text for interest in INTERESTS if interest.domain == same_domain
    )[:2]
    invalid.append(replace(row, interest_seeds=same_domain_seeds))

    related_sector = row.occupation_sector
    related_domains = SECTOR_RELATED_DOMAINS.get(related_sector, frozenset())
    if not related_domains:
        related_sector = next(
            (
                sector_id
                for sector_id, domains in SECTOR_RELATED_DOMAINS.items()
                if domains
            ),
            None,
        )
        assert related_sector is not None
        related_domains = SECTOR_RELATED_DOMAINS[related_sector]
    related_seeds = [
        next(interest.text for interest in INTERESTS if interest.domain == domain)
        for domain in INTEREST_DOMAINS
        if domain in related_domains
    ][:2]
    if len(related_seeds) < 2:
        related_seeds = [
            interest.text
            for interest in INTERESTS
            if interest.domain in related_domains
        ][:2]
    related_row = row
    if not SECTOR_RELATED_DOMAINS.get(row.occupation_sector):
        compatible_related = next(
            (
                (occupation, option)
                for occupation in OCCUPATIONS
                if occupation.sector == related_sector
                and row.employment_context_id in occupation.allowed_contexts
                and (occupation.min_age is None or row.age >= occupation.min_age)
                for option in occupation.education_options
                if (
                    _LEVEL_BY_ID[option.level_id].min_age is None
                    or row.age >= _LEVEL_BY_ID[option.level_id].min_age
                )
            ),
            None,
        )
        if compatible_related is not None:
            occupation, option = compatible_related
            related_row = replace(
                row,
                occupation_id=occupation.id,
                occupation=occupation.label,
                occupation_sector=occupation.sector,
                education_level_id=option.level_id,
                education=option.text,
            )
    invalid.append(replace(related_row, interest_seeds=tuple(related_seeds)))
    invalid.extend(
        [
            replace(row, occupation_id="occupation.wizard"),
            replace(row, troll_modifier_id="troll.pedantic", troll_modifier="wizardly"),
            replace(row, writing_style_id="style.wizard"),
            replace(row, username_style="username style that does not exist"),
            replace(row, id=""),
        ]
    )

    assert len(invalid) == 17
    assert all(validate_assignment(candidate) for candidate in invalid)


def test_username_style_and_interest_seeds():
    plan = _build(10, 0, 5)
    username_styles = {style.text for style in USERNAME_STYLES}
    interest_by_text = {interest.text: interest for interest in INTERESTS}
    assert all(row.username_style in username_styles for row in plan)
    assert len({row.username_style for row in plan}) >= 4
    used_domains = set()
    for row in plan:
        assert len(row.interest_seeds) == 2
        seeds = [interest_by_text[text] for text in row.interest_seeds]
        assert seeds[0].domain != seeds[1].domain
        used_domains.update(seed.domain for seed in seeds)
    assert len(used_domains) >= 8


def test_trait_diversity_spread():
    plan = _build(50, 0, 9)
    trait_counts = Counter(trait_id for row in plan for trait_id in row.trait_ids)
    assert all(count <= 20 for count in trait_counts.values())
    assert len(trait_counts) >= 25
    assert all(
        len({_TRAIT_BY_ID[trait_id].axis for trait_id in row.trait_ids}) == 4
        and any(_TRAIT_BY_ID[trait_id].limitation for trait_id in row.trait_ids)
        for row in plan
    )


def test_education_string_diversity():
    plan = _build(50, 0, 13)
    assert len({row.education_level_id for row in plan}) >= 6
    assert len({row.education for row in plan}) >= 10
