"""Tests for local website art-direction sampling and rendering."""

from __future__ import annotations

import json
import random

from deaddit.websites.diversity import (
    _GENRE_POOL,
    _LAYOUT_POOL,
    _MOOD_POOL,
    _RHYTHM_POOL,
    _TYPOGRAPHY_POOL,
    diversity_ids,
    render_website_diversity,
    sample_website_diversity,
)

_POOLS = {
    "genres": _GENRE_POOL,
    "layouts": _LAYOUT_POOL,
    "moods": _MOOD_POOL,
    "typography": _TYPOGRAPHY_POOL,
    "rhythms": _RHYTHM_POOL,
}


def test_each_axis_pool_has_expected_minimum_and_stable_unique_ids():
    for pool in _POOLS.values():
        assert len(pool) >= 10
        ids = [option.id for option in pool]
        assert len(ids) == len(set(ids))
        assert all(option.id for option in pool)


def test_sample_sizes_are_two_two_two_two_one():
    matrix = sample_website_diversity(random.Random(42))

    assert len(matrix.genres) == 2
    assert len(matrix.layouts) == 2
    assert len(matrix.moods) == 2
    assert len(matrix.typography) == 2
    assert len(matrix.rhythms) == 1


def test_sampling_is_without_replacement_per_axis():
    matrix = sample_website_diversity(random.Random(7))

    for options in (
        matrix.genres,
        matrix.layouts,
        matrix.moods,
        matrix.typography,
        matrix.rhythms,
    ):
        assert len(options) == len({option.id for option in options})


def test_weighted_seeded_sampling_is_deterministic():
    first = sample_website_diversity(random.Random(2026))
    second = sample_website_diversity(random.Random(2026))

    assert first == second
    assert diversity_ids(first) == diversity_ids(second)


def test_rendered_matrix_contains_only_selected_text_and_exact_template():
    matrix = sample_website_diversity(random.Random(99))
    rendered = render_website_diversity(matrix)
    expected = "\n".join(
        (
            "Website art direction matrix (sampled for this generation; treat it as a coherent constraint, not a list to copy verbatim):",
            f"- Site archetypes: {'; '.join(option.text for option in matrix.genres)}",
            f"- Layout structures: {'; '.join(option.text for option in matrix.layouts)}",
            f"- Visual moods and palettes: {'; '.join(option.text for option in matrix.moods)}",
            f"- Typographic character: {'; '.join(option.text for option in matrix.typography)}",
            f"- Content rhythm and interaction: {'; '.join(option.text for option in matrix.rhythms)}",
            "Interpret the matrix through the persona brief, invent concrete content, and make the result feel like a complete independent website. Prefer visible navigation, section links, and a footer when the selected structure calls for them; in-page links may be inert anchors.",
        )
    )

    assert rendered == expected
    for axis, options in vars(matrix).items():
        for option in options:
            assert option.text in rendered
        for option in _POOLS[axis]:
            if option not in options:
                assert option.text not in rendered


def test_ids_are_ordered_and_json_safe():
    matrix = sample_website_diversity(random.Random(13))
    ids = diversity_ids(matrix)

    assert list(ids) == ["genres", "layouts", "moods", "typography", "rhythms"]
    assert ids == {
        "genres": tuple(option.id for option in matrix.genres),
        "layouts": tuple(option.id for option in matrix.layouts),
        "moods": tuple(option.id for option in matrix.moods),
        "typography": tuple(option.id for option in matrix.typography),
        "rhythms": tuple(option.id for option in matrix.rhythms),
    }
    json.dumps(ids, sort_keys=True)
