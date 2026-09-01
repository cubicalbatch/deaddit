"""Sample and render coherent art direction for generated images.

The sampler chooses one stable creative direction and then derives compatible
capture choices from that direction.  It consumes only a caller-provided random
stream; source prompts are inspected only for their explicit medium intent.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DiversityOption:
    """One stable art-direction choice."""

    id: str
    text: str
    weight: float = 1.0


@dataclass(frozen=True)
class ImageDiversity:
    """One selected direction and its compatible rendering choices."""

    direction: DiversityOption
    subject: DiversityOption
    framing: DiversityOption
    lighting: DiversityOption
    capture: DiversityOption
    color: DiversityOption
    is_photographic: bool


@dataclass(frozen=True)
class _DirectionVariant:
    """Axis choices for one direction in one medium."""

    framing: DiversityOption
    lighting: DiversityOption
    capture: DiversityOption
    color: DiversityOption


@dataclass(frozen=True)
class _DirectionSpec:
    """A direction with coherent photographic and drawn variants."""

    direction: DiversityOption
    photographic: _DirectionVariant
    drawn: _DirectionVariant


def _option(axis: str, name: str, text: str, *, medium: str) -> DiversityOption:
    return DiversityOption(f"{axis}.{medium}_{name}", text)


def _photo(
    framing: tuple[str, str],
    lighting: tuple[str, str],
    capture: tuple[str, str],
    color: tuple[str, str],
) -> _DirectionVariant:
    return _DirectionVariant(
        framing=_option("framing", framing[0], framing[1], medium="photo"),
        lighting=_option("lighting", lighting[0], lighting[1], medium="photo"),
        capture=_option("capture", capture[0], capture[1], medium="photo"),
        color=_option("color", color[0], color[1], medium="photo"),
    )


def _drawn(
    framing: tuple[str, str],
    lighting: tuple[str, str],
    capture: tuple[str, str],
    color: tuple[str, str],
) -> _DirectionVariant:
    return _DirectionVariant(
        framing=_option("framing", framing[0], framing[1], medium="drawn"),
        lighting=_option("lighting", lighting[0], lighting[1], medium="drawn"),
        capture=_option("capture", capture[0], capture[1], medium="drawn"),
        color=_option("color", color[0], color[1], medium="drawn"),
    )


# Subject-matter hints broaden the scene without replacing the agent's request.
SUBJECT_OPTIONS: tuple[DiversityOption, ...] = (
    DiversityOption(
        "subject.urban_night",
        "an urban-night setting with neon, windows, or transit after dark",
    ),
    DiversityOption("subject.wilderness", "a wilderness setting far from buildings"),
    DiversityOption(
        "subject.mechanical_industrial",
        "mechanical or industrial details such as gears, engines, or tools",
    ),
    DiversityOption(
        "subject.historical_archival",
        "historical or archival material showing visible age",
    ),
    DiversityOption(
        "subject.abstract_texture",
        "an abstract close-up of texture, pattern, or surface",
    ),
    DiversityOption(
        "subject.candid_portrait", "a candid portrait of someone absorbed in a task"
    ),
    DiversityOption(
        "subject.architectural_detail",
        "architectural details such as stairwells, facades, or doorways",
    ),
    DiversityOption(
        "subject.weather", "weather as the main event: fog, rain, or storm light"
    ),
    DiversityOption(
        "subject.water_scene", "a water scene such as a harbour, lakeshore, or pool"
    ),
    DiversityOption(
        "subject.crowd_street", "a crowd or street scene with varied movement"
    ),
    DiversityOption(
        "subject.macro_nature",
        "macro nature such as insects, moss, seed heads, or bark",
    ),
    DiversityOption(
        "subject.vintage_retro", "a vintage or retro setting with period details"
    ),
)


def _sample_subject(rng: random.Random) -> DiversityOption:
    return rng.choices(
        SUBJECT_OPTIONS,
        weights=[option.weight for option in SUBJECT_OPTIONS],
        k=1,
    )[0]


# Direction descriptions deliberately describe *how* to approach the request;
# none names a replacement subject, place, or setting.
_DIRECTION_SPECS: tuple[_DirectionSpec, ...] = (
    _DirectionSpec(
        DiversityOption(
            "image.candid_snapshot",
            "Candid snapshot: observe the requested subject in an unposed, immediate moment.",
        ),
        _photo(
            ("eye_level", "Use an eye-level view with a natural, unforced crop."),
            (
                "available_light",
                "Use the available light with soft, believable falloff.",
            ),
            (
                "handheld_phone",
                "Make it a handheld phone photograph with slight natural imperfection.",
            ),
            ("true_to_life", "Keep color true to life with restrained contrast."),
        ),
        _drawn(
            (
                "observational_crop",
                "Use an unposed observational crop with a clear gesture.",
            ),
            (
                "soft_tonal",
                "Use soft drawn tonal modeling rather than cast-light simulation.",
            ),
            (
                "line_and_wash",
                "Use an observational line-and-wash drawing with varied line weight.",
            ),
            (
                "muted_paper",
                "Use a muted paper-ground palette with a few intentional accents.",
            ),
        ),
    ),
    _DirectionSpec(
        DiversityOption(
            "image.object_closeup",
            "Object close-up: prioritize the requested subject's tactile detail at close range.",
        ),
        _photo(
            (
                "tight_detail",
                "Use a tight close view that makes surface detail legible.",
            ),
            (
                "raking_side",
                "Use raking side light to reveal texture without theatrical effects.",
            ),
            (
                "camera_closeup",
                "Make it a real camera close-up with believable lens falloff.",
            ),
            (
                "material_neutral",
                "Use a neutral material palette with accurate surface color.",
            ),
        ),
        _drawn(
            (
                "detail_panel",
                "Use a tightly framed detail panel with deliberate contour emphasis.",
            ),
            ("modeled_tone", "Use layered drawn tones to describe volume and texture."),
            (
                "technical_study",
                "Use a detailed ink or graphite study with controlled hatching.",
            ),
            (
                "earth_and_ink",
                "Use earth pigments and dark ink with the support surface visible.",
            ),
        ),
    ),
    _DirectionSpec(
        DiversityOption(
            "image.place_observation",
            "Place observation: let the requested place or context remain legible in an observational view.",
        ),
        _photo(
            (
                "wide_context",
                "Use a wide environmental view that preserves spatial context.",
            ),
            ("overcast_day", "Use broad overcast daylight for honest, even detail."),
            (
                "documentary_camera",
                "Make it a documentary camera photograph from a plausible vantage point.",
            ),
            (
                "atmospheric_neutral",
                "Keep atmospheric color subtle and faithful to the observed conditions.",
            ),
        ),
        _drawn(
            (
                "panoramic_field",
                "Use a broad composed field with clear spatial relationships.",
            ),
            (
                "flat_even_tone",
                "Use even graphic tones and a restrained sense of depth.",
            ),
            (
                "location_sketch",
                "Use a location sketch with selective detail and visible construction marks.",
            ),
            ("quiet_earth", "Use quiet earth and sky colors with measured separation."),
        ),
    ),
    _DirectionSpec(
        DiversityOption(
            "image.process_documentation",
            "Process documentation: show the requested activity through an attentive record of its stages.",
        ),
        _photo(
            (
                "sequence_ready",
                "Use a purposeful frame that makes the action and its stage easy to read.",
            ),
            (
                "work_light",
                "Use practical work light with shadows that remain informative.",
            ),
            (
                "documentary_handheld",
                "Make it a handheld documentary photograph, not a staged advertisement.",
            ),
            (
                "functional_color",
                "Use functional, honest color with modest saturation.",
            ),
        ),
        _drawn(
            (
                "stepwise_layout",
                "Use a stepwise layout with clear progression across the frame.",
            ),
            (
                "graphic_shadow",
                "Use graphic shadow shapes to separate the stages cleanly.",
            ),
            (
                "instructional_ink",
                "Use an instructional ink drawing with economical annotation-like marks.",
            ),
            (
                "workshop_ochre",
                "Use ochre, graphite, and one restrained accent for visual continuity.",
            ),
        ),
    ),
    _DirectionSpec(
        DiversityOption(
            "image.finished_result",
            "Finished result: present the requested completed work with calm, legible emphasis.",
        ),
        _photo(
            (
                "three_quarter_result",
                "Use a three-quarter view that makes the completed form immediately legible.",
            ),
            (
                "controlled_softbox",
                "Use controlled soft light with gentle edge definition.",
            ),
            (
                "product_camera",
                "Make it a realistic product photograph with useful, non-glossy detail.",
            ),
            (
                "clean_balanced",
                "Use clean balanced color without a synthetic showroom cast.",
            ),
        ),
        _drawn(
            (
                "hero_composition",
                "Use a composed hero view with a strong silhouette and clean margins.",
            ),
            (
                "design_highlight",
                "Use deliberate highlight and shadow shapes to clarify the result.",
            ),
            (
                "poster_design",
                "Use a polished poster or print design with disciplined shape language.",
            ),
            (
                "limited_accent",
                "Use a limited palette with one confident accent color.",
            ),
        ),
    ),
    _DirectionSpec(
        DiversityOption(
            "image.before_after",
            "Before and after: make the requested change readable through a direct visual comparison.",
        ),
        _photo(
            (
                "matched_pair",
                "Use matched framing and scale so the comparison reads immediately.",
            ),
            ("consistent_daylight", "Use consistent daylight across the paired views."),
            (
                "documentary_pair",
                "Make it a realistic documentary photo pair with ordinary imperfections.",
            ),
            (
                "truthful_comparison",
                "Keep color and contrast consistent so the change is not exaggerated.",
            ),
        ),
        _drawn(
            (
                "split_comparison",
                "Use a clear split comparison with aligned visual anchors.",
            ),
            (
                "graphic_difference",
                "Use graphic tonal contrast only where it clarifies the change.",
            ),
            (
                "annotated_plate",
                "Use a designed comparison plate with restrained labels or marks.",
            ),
            (
                "paired_neutrals",
                "Use paired neutral tones with one controlled difference accent.",
            ),
        ),
    ),
    _DirectionSpec(
        DiversityOption(
            "image.archival_artifact",
            "Archival artifact: preserve the requested item's evidence of age, use, and provenance.",
        ),
        _photo(
            (
                "flat_record",
                "Use a careful record view that keeps edges and identifying details visible.",
            ),
            (
                "museum_soft",
                "Use soft diffuse illumination that avoids distracting glare.",
            ),
            (
                "archive_documentary",
                "Make it a faithful archival photograph with natural surface wear.",
            ),
            (
                "faded_material",
                "Use gently faded, material-accurate color with restrained contrast.",
            ),
        ),
        _drawn(
            (
                "catalog_plate",
                "Use a catalog-like plate with measured margins and visible evidence.",
            ),
            ("paper_tone", "Use flat paper tones and controlled value changes."),
            (
                "engraved_record",
                "Use an engraved or lithographic record with careful fine marks.",
            ),
            (
                "sepia_ink",
                "Use sepia, charcoal, and paper white without glossy color effects.",
            ),
        ),
    ),
    _DirectionSpec(
        DiversityOption(
            "image.food_photo",
            "Food photo: make the requested food immediately appetizing through texture and arrangement.",
        ),
        _photo(
            (
                "tabletop_three_quarter",
                "Use a tabletop three-quarter view with enough context to read the serving.",
            ),
            (
                "window_soft",
                "Use soft window light that preserves texture and natural highlights.",
            ),
            (
                "phone_food",
                "Make it a believable phone food photograph, not a sterile advertisement.",
            ),
            (
                "appetizing_natural",
                "Use appetizing but natural color; avoid exaggerated saturation.",
            ),
        ),
        _drawn(
            (
                "menu_composition",
                "Use a clear menu-like composition that makes the arrangement readable.",
            ),
            ("warm_flat_tone", "Use warm flat tones with selective value for texture."),
            (
                "culinary_illustration",
                "Use a culinary illustration with confident contour and material marks.",
            ),
            (
                "spice_palette",
                "Use a warm spice palette balanced by paper and ink neutrals.",
            ),
        ),
    ),
    _DirectionSpec(
        DiversityOption(
            "image.pet_wildlife",
            "Pet and wildlife: attend to the requested animal's behavior without forcing a pose.",
        ),
        _photo(
            (
                "behavioral_eye_level",
                "Use an eye-level behavioral frame without forcing a pose.",
            ),
            (
                "natural_outdoor",
                "Use natural outdoor light with readable detail in the shadows.",
            ),
            (
                "telephoto_observation",
                "Make it a realistic observational photograph with plausible lens distance.",
            ),
            (
                "habitat_true",
                "Keep habitat color true and avoid fantasy color grading.",
            ),
        ),
        _drawn(
            (
                "gesture_study",
                "Use a gesture-focused study that keeps the animal's movement readable.",
            ),
            (
                "natural_marking",
                "Use drawn marks to imply light while preserving clear anatomy.",
            ),
            (
                "field_plate",
                "Use a natural-history field plate with precise contour and selective detail.",
            ),
            (
                "habitat_earth",
                "Use habitat greens, browns, and paper neutrals with small accents.",
            ),
        ),
    ),
    _DirectionSpec(
        DiversityOption(
            "image.macro_detail",
            "Macro detail: magnify the requested subject's small structure, texture, or transition.",
        ),
        _photo(
            (
                "macro_focus",
                "Use a true macro frame with a narrow but intentional focus plane.",
            ),
            (
                "diffused_close",
                "Use diffused close light that reveals tiny relief without harsh glare.",
            ),
            (
                "macro_lens",
                "Make it a realistic macro photograph with optical depth-of-field behavior.",
            ),
            (
                "microscopic_true",
                "Keep magnified color truthful to the material rather than surreal.",
            ),
        ),
        _drawn(
            (
                "magnified_detail",
                "Use an enlarged detail view with clear structural hierarchy.",
            ),
            (
                "crosshatch_relief",
                "Use crosshatching and layered marks to describe small relief.",
            ),
            (
                "scientific_study",
                "Use a precise scientific drawing with disciplined line variation.",
            ),
            (
                "mineral_paper",
                "Use mineral, moss, or paper hues only as the requested material supports.",
            ),
        ),
    ),
    _DirectionSpec(
        DiversityOption(
            "image.diagram_infographic",
            "Diagram and infographic: organize the requested information so relationships are immediately legible.",
        ),
        _photo(
            (
                "flat_document",
                "Use a flat, square-on record view that keeps all requested information legible.",
            ),
            (
                "even_document_light",
                "Use even document light without glare or dramatic falloff.",
            ),
            (
                "scanner_or_camera",
                "Make it a realistic photograph or scan of the requested diagram.",
            ),
            ("paper_true", "Keep ink and paper color accurate and restrained."),
        ),
        _drawn(
            (
                "structured_grid",
                "Use a structured grid with clear hierarchy and unambiguous relationships.",
            ),
            ("flat_design_tone", "Use flat design tones and consistent visual weight."),
            (
                "vector_infographic",
                "Use a clean vector infographic or technical diagram with deliberate symbols.",
            ),
            (
                "signal_palette",
                "Use a limited signal palette with strong contrast for labels and paths.",
            ),
        ),
    ),
    _DirectionSpec(
        DiversityOption(
            "image.artwork_craft",
            "Artwork and craft: foreground the requested work's material decisions and hand-made evidence.",
        ),
        _photo(
            (
                "artwork_record",
                "Use a faithful record view that shows the requested work without altering it.",
            ),
            (
                "gallery_diffuse",
                "Use diffuse illumination that preserves surface texture and edges.",
            ),
            (
                "artwork_documentary",
                "Make it a realistic photograph documenting the requested work.",
            ),
            (
                "material_faithful",
                "Keep pigment, fiber, wood, or metal color faithful to the requested material.",
            ),
        ),
        _drawn(
            (
                "crafted_composition",
                "Use a composed view that makes the hand-made construction easy to inspect.",
            ),
            (
                "tactile_marks",
                "Use tactile marks and layered value to show material decisions.",
            ),
            (
                "mixed_media",
                "Use a compatible craft medium such as ink, collage, print, or paint as requested.",
            ),
            (
                "workshop_palette",
                "Use a material-led palette with paper, pigment, and one deliberate accent.",
            ),
        ),
    ),
)

_DIRECTION_BY_ID = {spec.direction.id: spec for spec in _DIRECTION_SPECS}

# The first ten are photographic by default.  A source prompt can still make
# any direction drawn, or make either of the final two photographic, because
# an explicit source medium is authoritative.
_PHOTOGRAPHIC_DIRECTION_IDS = frozenset(
    spec.direction.id for spec in _DIRECTION_SPECS[:-2]
)

_PHOTOGRAPHIC_TERMS = re.compile(
    r"\b(?:photo(?:graph(?:y|ic)?|realistic)?|snapshot|selfie|camera|dslr|"
    r"phone\s+(?:photo|shot)|film\s+photo|documentary\s+(?:photo|image|shot)|"
    r"product\s+(?:photo|shot)|macro\s+(?:photo|shot))\b",
    re.IGNORECASE,
)
_DRAWN_TERMS = re.compile(
    r"\b(?:illustrat(?:ion|ed|e)|painting?|painted|oil\s*paint(?:ing)?|"
    r"watercolou?r|engraving?|etching?|woodcut|lithograph(?:y)?|sketch(?:ed)?|"
    r"drawing?|diagram(?:s)?|infographic(?:s)?|render(?:ed|ing)?|3d|three[- ]d|"
    r"concept\s*art|pixel\s*art|comic|collage|screenprint|poster|artwork|craft)\b",
    re.IGNORECASE,
)


def _source_medium(source_prompt: str) -> bool | None:
    """Return source medium (``True`` photo, ``False`` drawn), if explicit."""
    if not source_prompt:
        return None
    photo = _PHOTOGRAPHIC_TERMS.search(source_prompt)
    drawn = _DRAWN_TERMS.search(source_prompt)
    if photo is None and drawn is None:
        return None
    if drawn is None:
        return True
    if photo is None:
        return False
    # The first explicit medium phrase is the closest representation of the
    # persona's request (e.g. “a photo of an engraving” is photographic).
    return photo.start() < drawn.start()


def sample_image_diversity(
    rng: random.Random,
    *,
    direction_id: str | None = None,
    source_prompt: str = "",
) -> ImageDiversity:
    """Choose one coherent direction using only the caller's random stream.

    ``direction_id`` is used by the prompt planner to carry one locally chosen
    direction through the image prompt and downstream samplers.  When absent,
    one of the twelve stable directions is selected.  An explicit photographic
    or drawn medium in ``source_prompt`` overrides the direction's default
    medium without changing its intent.
    """
    if direction_id is None:
        spec = _DIRECTION_SPECS[rng.randrange(len(_DIRECTION_SPECS))]
    else:
        try:
            spec = _DIRECTION_BY_ID[direction_id]
        except KeyError as exc:
            valid = ", ".join(item.direction.id for item in _DIRECTION_SPECS)
            raise ValueError(
                f"unknown image diversity direction ID {direction_id!r}; expected one of: {valid}"
            ) from exc

    subject = _sample_subject(rng)
    source_medium = _source_medium(source_prompt)
    is_photographic = (
        source_medium
        if source_medium is not None
        else spec.direction.id in _PHOTOGRAPHIC_DIRECTION_IDS
    )
    variant = spec.photographic if is_photographic else spec.drawn
    return ImageDiversity(
        direction=spec.direction,
        subject=subject,
        framing=variant.framing,
        lighting=variant.lighting,
        capture=variant.capture,
        color=variant.color,
        is_photographic=is_photographic,
    )


def render_image_diversity(matrix: ImageDiversity) -> str:
    """Render exactly the selected direction and its compatible choices."""
    medium_priority = (
        "Photographic priority: render a realistic photograph captured by a real "
        "camera or phone, with slightly imperfect, off-center framing and no "
        "staged golden raking light, floating dust, or staged steam unless the "
        "requested subject genuinely requires it. Reject illustration, painting, "
        "engraving, diagram, 3D-render, and concept-art appearance."
        if matrix.is_photographic
        else "Drawn/design priority: render a deliberate illustration, painting, print, or diagram treatment; do not make it photorealistic."
    )
    return "\n".join(
        (
            "Image art direction (one selected direction; preserve the persona request):",
            f"- Direction: {matrix.direction.text}",
            f"- Subject hint (optional): {matrix.subject.text}",
            f"- Framing: {matrix.framing.text}",
            f"- Lighting: {matrix.lighting.text}",
            f"- Capture or medium: {matrix.capture.text}",
            f"- Color: {matrix.color.text}",
            medium_priority,
            "Keep the requested subject and location; use the subject hint only when compatible. Explicit source wording wins for medium.",
        )
    )


def diversity_ids(matrix: ImageDiversity) -> dict[str, tuple[str, ...]]:
    """Return stable selected IDs in prompt and provenance order."""
    return {
        "direction": (matrix.direction.id,),
        "subject": (matrix.subject.id,),
        "framing": (matrix.framing.id,),
        "lighting": (matrix.lighting.id,),
        "capture": (matrix.capture.id,),
        "color": (matrix.color.id,),
    }


__all__ = [
    "DiversityOption",
    "ImageDiversity",
    "SUBJECT_OPTIONS",
    "diversity_ids",
    "render_image_diversity",
    "sample_image_diversity",
]
