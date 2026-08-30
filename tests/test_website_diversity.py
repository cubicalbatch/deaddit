"""Tests for coherent local website art-direction sampling and rendering."""

from __future__ import annotations

import json
import random

import pytest

from deaddit.websites.diversity import (
    _GENRE_POOL,
    _LAYOUT_ALLOWLIST,
    _MOOD_POOL,
    diversity_ids,
    render_website_diversity,
    sample_website_diversity,
)


def test_direction_vocabulary_is_exact_and_unique():
    direction_ids = [option.id for option in _GENRE_POOL]
    assert direction_ids == [
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
    assert len(direction_ids) == len(set(direction_ids))
    assert set(direction_ids) == set(_LAYOUT_ALLOWLIST)


def test_sample_selects_one_value_per_axis_and_compatible_layout():
    matrix = sample_website_diversity(random.Random(42))

    assert matrix.direction_id in {option.id for option in _GENRE_POOL}
    axes = (
        matrix.genres,
        matrix.layouts,
        matrix.moods,
        matrix.typography,
        matrix.rhythms,
        matrix.imagery,
    )
    assert all(len(axis) == 1 for axis in axes)
    assert matrix.layouts[0].id in _LAYOUT_ALLOWLIST[matrix.direction_id]
    assert matrix.genres[0].id == matrix.direction_id


def test_pinned_direction_does_not_change_archetype_and_unknown_fails():
    matrix = sample_website_diversity(
        random.Random(7), direction_id="website.interactive_utility"
    )
    assert matrix.direction_id == "website.interactive_utility"
    assert matrix.genres[0].id == "website.interactive_utility"
    assert matrix.layouts[0].id in _LAYOUT_ALLOWLIST[matrix.direction_id]

    with pytest.raises(ValueError, match="unknown website direction ID"):
        sample_website_diversity(random.Random(7), direction_id="website.missing")


def test_seeded_sampling_is_deterministic_and_ids_are_json_safe():
    first = sample_website_diversity(random.Random(2026))
    second = sample_website_diversity(random.Random(2026))
    assert first == second
    assert diversity_ids(first) == diversity_ids(second)
    assert list(diversity_ids(first)) == [
        "genres",
        "layouts",
        "moods",
        "typography",
        "rhythms",
        "imagery",
    ]
    json.dumps(diversity_ids(first), sort_keys=True)


def test_seeded_coverage_reaches_broad_families_and_both_modes():
    matrices = [sample_website_diversity(random.Random(seed)) for seed in range(256)]
    directions = {matrix.direction_id for matrix in matrices}
    assert {
        "website.news_report",
        "website.community_portal",
        "website.product_page",
        "website.reference",
        "website.interactive_utility",
        "website.portfolio",
    } <= directions
    moods = {matrix.moods[0].id for matrix in matrices}
    assert any(mood.startswith("mood.light_") for mood in moods)
    assert any(mood.startswith("mood.dark_") for mood in moods)
    assert sum(option.weight for option in _MOOD_POOL if option.id.startswith("mood.light_")) > 0
    assert sum(option.weight for option in _MOOD_POOL if option.id.startswith("mood.dark_")) > 0


def test_renderer_is_one_decisive_brief_without_contradictory_alternatives():
    matrix = sample_website_diversity(
        random.Random(11), direction_id="website.news_report"
    )
    rendered = render_website_diversity(matrix)
    assert "one authoritative direction" in rendered
    assert "alternatives" not in rendered
    assert "choose between" not in rendered
    for axis in (
        matrix.genres,
        matrix.layouts,
        matrix.moods,
        matrix.typography,
        matrix.rhythms,
        matrix.imagery,
    ):
        assert axis[0].text in rendered
    assert "Preserve the persona's site subject and content." in rendered
