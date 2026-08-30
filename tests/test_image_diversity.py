"""Tests for image art-direction diversity sampling."""

import json
import random

import pytest

from deaddit.images import diversity
from deaddit.images.diversity import (
    DiversityOption,
    ImageDiversity,
    diversity_ids,
    render_image_diversity,
    sample_image_diversity,
)

POOLS = (
    ("framings", "framing.", diversity._FRAMING_POOL),
    ("subjects", "subject.", diversity._SUBJECT_POOL),
    ("lighting", "lighting.", diversity._LIGHTING_POOL),
    ("palettes", "palette.", diversity._PALETTE_POOL),
    ("styles", "style.", diversity._STYLE_POOL),
    ("settings", "setting.", diversity._SETTING_POOL),
)
COLLAPSE_IDS = {
    "framings": "framing.overhead_flat_lay",
    "subjects": "subject.paper_artifact",
    "lighting": "lighting.warm_local",
    "palettes": "palette.warm_neutral",
    "styles": "style.documentary_photo",
    "settings": "setting.wooden_workbench",
}


@pytest.mark.parametrize("axis, prefix, pool", POOLS)
def test_pools_have_minimum_size_unique_ids_and_collapse_weight(axis, prefix, pool):
    assert len(pool) >= 12
    assert len({option.id for option in pool}) == len(pool)
    assert sum(option.weight == 0.45 for option in pool) == 1
    assert (
        next(option for option in pool if option.weight == 0.45).id
        == COLLAPSE_IDS[axis]
    )
    assert all(option.weight in (0.45, 1.0) for option in pool)
    assert all(option.id.startswith(prefix) for option in pool)


def test_sample_has_one_option_per_axis_in_fixed_order():
    matrix = sample_image_diversity(random.Random(4))
    assert isinstance(matrix, ImageDiversity)
    assert list(matrix.__dataclass_fields__) == [
        "framings",
        "subjects",
        "lighting",
        "palettes",
        "styles",
        "settings",
    ]
    assert all(len(options) == 1 for options in vars(matrix).values())
    assert list(diversity_ids(matrix)) == [
        "framings",
        "subjects",
        "lighting",
        "palettes",
        "styles",
        "settings",
    ]


def test_weighted_helper_samples_without_replacement():
    pool = tuple(DiversityOption(str(index), str(index), 1.0) for index in range(8))
    selected = diversity._sample_weighted_without_replacement(
        pool, 8, random.Random(12)
    )
    assert len(selected) == 8
    assert len({option.id for option in selected}) == 8
    assert {option.id for option in selected} == {option.id for option in pool}


def test_seeded_sampling_is_deterministic():
    assert sample_image_diversity(random.Random(12345)) == sample_image_diversity(
        random.Random(12345)
    )


def test_different_seeds_produce_different_sampling():
    assert sample_image_diversity(random.Random(1)) != sample_image_diversity(
        random.Random(2)
    )


def test_render_is_exact_template_and_contains_only_selected_text():
    selected = tuple(pool[1] for _, _, pool in POOLS)
    matrix = ImageDiversity(*((option,) for option in selected))
    expected = "\n".join(
        (
            "Image art direction (sampled for this generation; blend it with the persona request rather than treating it as a replacement):",
            f"- Framing and camera: {selected[0].text}",
            f"- Subject focus: {selected[1].text}",
            f"- Lighting situation: {selected[2].text}",
            f"- Palette and mood: {selected[3].text}",
            f"- Visual medium and style: {selected[4].text}",
            f"- Setting and surface: {selected[5].text}",
            "Keep the requested subject and scene. Apply each sampled direction where the prompt leaves that axis unspecified. If the prompt explicitly contradicts a sampled direction, the prompt wins for that axis.",
        )
    )
    rendered = render_image_diversity(matrix)
    assert rendered == expected
    assert len(rendered.splitlines()) == 8
    for _, _, pool in POOLS:
        for option in pool:
            if option not in selected:
                assert option.text not in rendered


def test_diversity_ids_are_ordered_json_safe_tuples():
    matrix = sample_image_diversity(random.Random(99))
    ids = diversity_ids(matrix)
    assert all(isinstance(axis_ids, tuple) for axis_ids in ids.values())
    assert json.loads(json.dumps(ids)) == {
        key: list(value) for key, value in ids.items()
    }
