"""Focused tests for coherent image art-direction sampling."""

import json
import random

import pytest

from deaddit.images import diversity
from deaddit.images.diversity import (
    diversity_ids,
    render_image_diversity,
    sample_image_diversity,
)

IMAGE_DIRECTION_IDS = (
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
)
PHOTO_DIRECTION_IDS = IMAGE_DIRECTION_IDS[:10]
DRAWN_DIRECTION_IDS = IMAGE_DIRECTION_IDS[10:]


@pytest.mark.parametrize("direction_id", IMAGE_DIRECTION_IDS)
def test_every_direction_has_one_coherent_matrix(direction_id):
    matrix = sample_image_diversity(random.Random(1), direction_id=direction_id)
    assert matrix.direction.id == direction_id
    ids = diversity_ids(matrix)
    assert list(ids) == [
        "direction",
        "subject",
        "framing",
        "lighting",
        "capture",
        "color",
    ]
    assert all(len(axis_ids) == 1 for axis_ids in ids.values())
    assert all(isinstance(axis_ids[0], str) for axis_ids in ids.values())
    medium_ids = (ids[axis][0] for axis in ("framing", "lighting", "capture", "color"))
    if matrix.is_photographic:
        assert all(".photo_" in option_id for option_id in medium_ids)
    else:
        assert all(".drawn_" in option_id for option_id in medium_ids)


def test_direction_catalog_is_exact_and_ordered():
    assert (
        tuple(spec.direction.id for spec in diversity._DIRECTION_SPECS)
        == IMAGE_DIRECTION_IDS
    )


def test_seeded_sampling_is_deterministic_and_varies_multiple_axes():
    first = [sample_image_diversity(random.Random(seed)) for seed in range(10)]
    second = [sample_image_diversity(random.Random(seed)) for seed in range(10)]

    assert first == second
    assert len({matrix.direction.id for matrix in first}) > 1
    assert len({matrix.subject.id for matrix in first}) > 1
    assert len({matrix.framing.id for matrix in first}) > 1
    assert len({matrix.capture.id for matrix in first}) > 1
    assert len({matrix.color.id for matrix in first}) > 1


def test_subject_hint_is_included_without_replacing_agent_intent():
    matrix = sample_image_diversity(random.Random(5), direction_id="image.food_photo")
    rendered = render_image_diversity(matrix)

    assert rendered.count("- Subject hint (optional):") == 1
    assert matrix.subject.text in rendered
    assert "Keep the requested subject and location" in rendered
    assert "only when compatible" in rendered


@pytest.mark.parametrize(
    ("source_prompt", "medium_marker"),
    (
        (
            "A documentary photograph taken with a phone camera.",
            "Photographic priority:",
        ),
        ("An engraved illustration in watercolor and ink.", "Drawn/design priority:"),
    ),
)
def test_subject_hint_composes_with_photo_and_drawn_modes(source_prompt, medium_marker):
    matrix = sample_image_diversity(
        random.Random(7),
        direction_id="image.food_photo",
        source_prompt=source_prompt,
    )
    rendered = render_image_diversity(matrix)

    assert f"- Subject hint (optional): {matrix.subject.text}" in rendered
    assert medium_marker in rendered


def test_subject_sampling_is_seeded_and_deterministic():
    first = [
        sample_image_diversity(random.Random(seed)).subject.id for seed in range(20)
    ]
    second = [
        sample_image_diversity(random.Random(seed)).subject.id for seed in range(20)
    ]

    assert first == second
    assert len(set(first)) > 1


@pytest.mark.parametrize("direction_id", IMAGE_DIRECTION_IDS)
def test_explicit_photo_source_wins_over_drawn_default(direction_id):
    matrix = sample_image_diversity(
        random.Random(7),
        direction_id=direction_id,
        source_prompt="A documentary photograph taken with a phone camera.",
    )

    assert matrix.is_photographic is True
    assert all(
        getattr(matrix, axis).id.startswith(f"{axis}.photo_")
        for axis in ("framing", "lighting", "capture", "color")
    )
    rendered = render_image_diversity(matrix)
    assert "Photographic priority: render a realistic photograph" in rendered
    for cue in (
        "slightly imperfect",
        "off-center framing",
        "golden raking light",
        "floating dust",
        "staged steam",
    ):
        assert cue in rendered
    assert "requested subject genuinely requires it" in rendered
    assert "Drawn/design priority:" not in rendered


@pytest.mark.parametrize("direction_id", IMAGE_DIRECTION_IDS)
def test_explicit_drawn_source_wins_over_photo_default(direction_id):
    matrix = sample_image_diversity(
        random.Random(7),
        direction_id=direction_id,
        source_prompt="An engraved illustration in watercolor and ink.",
    )

    assert matrix.is_photographic is False
    assert all(
        getattr(matrix, axis).id.startswith(f"{axis}.drawn_")
        for axis in ("framing", "lighting", "capture", "color")
    )
    rendered = render_image_diversity(matrix)
    assert "Drawn/design priority:" in rendered
    for cue in (
        "slightly imperfect",
        "off-center framing",
        "golden raking light",
        "floating dust",
        "staged steam",
    ):
        assert cue not in rendered
    assert "Photographic priority:" not in rendered


def test_medium_phrase_order_handles_photo_of_drawn_artifact():
    matrix = sample_image_diversity(
        random.Random(2),
        direction_id="image.artwork_craft",
        source_prompt="A photograph of an old engraving.",
    )
    assert matrix.is_photographic is True


@pytest.mark.parametrize("direction_id", PHOTO_DIRECTION_IDS)
def test_photo_directions_never_compile_drawn_axis_choices(direction_id):
    matrix = sample_image_diversity(random.Random(11), direction_id=direction_id)
    rendered = render_image_diversity(matrix)

    assert matrix.is_photographic is True
    assert all(
        getattr(matrix, axis).id.startswith(f"{axis}.photo_")
        for axis in ("framing", "lighting", "capture", "color")
    )
    assert "realistic photograph" in rendered
    # The rejection sentence is allowed to name forbidden media, but no
    # positive drawn directive may be selected for a photographic direction.
    assert "Drawn/design priority:" not in rendered
    assert "Use a deliberate illustration" not in rendered
    assert "Use a painting" not in rendered
    assert "Use a 3D" not in rendered


@pytest.mark.parametrize("direction_id", DRAWN_DIRECTION_IDS)
def test_drawn_directions_compile_only_drawn_choices(direction_id):
    matrix = sample_image_diversity(random.Random(11), direction_id=direction_id)
    rendered = render_image_diversity(matrix)

    assert matrix.is_photographic is False
    assert all(
        getattr(matrix, axis).id.startswith(f"{axis}.drawn_")
        for axis in ("framing", "lighting", "capture", "color")
    )
    assert "Drawn/design priority:" in rendered
    assert "realistic photograph captured" not in rendered


def test_render_has_one_selected_direction_and_preserves_subject_location():
    matrix = sample_image_diversity(
        random.Random(3), direction_id="image.place_observation"
    )
    rendered = render_image_diversity(matrix)

    assert rendered.count("- Direction:") == 1
    assert "one selected direction" in rendered
    assert "Keep the requested subject and location" in rendered
    assert "subject hint only when compatible" in rendered
    # These were old replacement axes and must not leak into the prompt.
    for injected in (
        "city street",
        "grass field",
        "wooden workbench",
        "paper artifact",
    ):
        assert injected not in rendered.lower()


def test_unknown_direction_id_fails_clearly():
    with pytest.raises(ValueError, match="unknown image diversity direction ID"):
        sample_image_diversity(random.Random(1), direction_id="image.unknown")


def test_diversity_ids_are_stable_json_safe_tuples():
    ids = diversity_ids(
        sample_image_diversity(random.Random(99), direction_id="image.food_photo")
    )

    assert all(isinstance(axis_ids, tuple) for axis_ids in ids.values())
    assert json.loads(json.dumps(ids)) == {
        key: list(value) for key, value in ids.items()
    }
