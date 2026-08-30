"""Sample and render varied art direction for generated images.

The module is pure data and consumes only a caller-provided random stream.
"""

from __future__ import annotations

import random
import textwrap
from dataclasses import dataclass


@dataclass(frozen=True)
class DiversityOption:
    """One stable, weighted art-direction option."""

    id: str
    text: str
    weight: float


@dataclass(frozen=True)
class ImageDiversity:
    """The selected art-direction options for one image generation."""

    framings: tuple[DiversityOption, ...]
    subjects: tuple[DiversityOption, ...]
    lighting: tuple[DiversityOption, ...]
    palettes: tuple[DiversityOption, ...]
    styles: tuple[DiversityOption, ...]
    settings: tuple[DiversityOption, ...]


_FRAMING_POOL: tuple[DiversityOption, ...] = (
    DiversityOption(
        "framing.overhead_flat_lay",
        "Use a straight-down overhead view with the subject arranged as a flat-lay composition.",
        0.45,
    ),
    DiversityOption(
        "framing.eye_level_close",
        "Use an eye-level close view with shallow depth and attention on one meaningful detail.",
        1.0,
    ),
    DiversityOption(
        "framing.low_angle",
        "Use a low camera angle that gives the subject weight against its surroundings.",
        1.0,
    ),
    DiversityOption(
        "framing.wide_environmental",
        "Use a wide environmental view where the subject shares visual importance with its surroundings.",
        1.0,
    ),
    DiversityOption(
        "framing.profile",
        "Use a side profile view with layered depth and a clear silhouette.",
        1.0,
    ),
    DiversityOption(
        "framing.rear_follow",
        "Use a from-behind viewpoint that suggests movement toward a visible destination.",
        1.0,
    ),
    DiversityOption(
        "framing.three_quarter",
        "Use a three-quarter view with dimensional form and an off-center subject.",
        1.0,
    ),
    DiversityOption(
        "framing.ground_level",
        "Use a ground-level perspective that exaggerates foreground texture and receding space.",
        1.0,
    ),
    DiversityOption(
        "framing.reflection",
        "Use a reflection-based composition with the primary subject seen indirectly.",
        1.0,
    ),
    DiversityOption(
        "framing.macro",
        "Use an intimate macro perspective that magnifies texture, material, and small transitions.",
        1.0,
    ),
    DiversityOption(
        "framing.balanced_pair",
        "Use a balanced two-subject composition with visual tension across the frame.",
        1.0,
    ),
    DiversityOption(
        "framing.motion",
        "Use a dynamic perspective with directional movement and intentional motion blur.",
        1.0,
    ),
)

_SUBJECT_POOL: tuple[DiversityOption, ...] = (
    DiversityOption(
        "subject.paper_artifact",
        "Make a paper artifact the central subject, with its marks and edges carrying the detail.",
        0.45,
    ),
    DiversityOption(
        "subject.person_gesture",
        "Center a person's gesture or partial figure; communicate action without requiring a posed portrait.",
        1.0,
    ),
    DiversityOption(
        "subject.botanical",
        "Center organic plant forms with varied scale, texture, and asymmetrical growth.",
        1.0,
    ),
    DiversityOption(
        "subject.architecture",
        "Center architectural form, geometry, and the way built structure meets open space.",
        1.0,
    ),
    DiversityOption(
        "subject.mechanical",
        "Center a mechanical object or system with visible joints, wear, and functional detail.",
        1.0,
    ),
    DiversityOption(
        "subject.food_texture",
        "Center a prepared dish or ingredient where color, texture, and arrangement tell the story.",
        1.0,
    ),
    DiversityOption(
        "subject.animal_encounter",
        "Center an animal in a natural moment, prioritizing behavior and attentive observation.",
        1.0,
    ),
    DiversityOption(
        "subject.landscape",
        "Center a broad landscape with a strong horizon, atmospheric depth, and changing terrain.",
        1.0,
    ),
    DiversityOption(
        "subject.craft",
        "Center a handmade object or craft process with material evidence and imperfect detail.",
        1.0,
    ),
    DiversityOption(
        "subject.transit",
        "Center a vehicle or transit moment with directional lines and lived-in context.",
        1.0,
    ),
    DiversityOption(
        "subject.found_object",
        "Center a found object whose age and use are legible through wear.",
        1.0,
    ),
    DiversityOption(
        "subject.abstract",
        "Center an abstract arrangement of shape, color, and texture without a literal narrative.",
        1.0,
    ),
)

_LIGHTING_POOL: tuple[DiversityOption, ...] = (
    DiversityOption(
        "lighting.warm_local",
        "Use warm local lamp light with soft falloff and intimate shadows.",
        0.45,
    ),
    DiversityOption(
        "lighting.hard_noon",
        "Use hard overhead daylight with crisp shadows and decisive highlights.",
        1.0,
    ),
    DiversityOption(
        "lighting.overcast",
        "Use broad overcast illumination with soft edges and even, truthful color.",
        1.0,
    ),
    DiversityOption(
        "lighting.cool_twilight",
        "Use cool twilight light with restrained contrast and a fading ambient sky.",
        1.0,
    ),
    DiversityOption(
        "lighting.backlit_silhouette",
        "Use strong backlight with a readable rim and selectively obscured detail.",
        1.0,
    ),
    DiversityOption(
        "lighting.colored_gels",
        "Use controlled colored light with distinct hues separated across the scene.",
        1.0,
    ),
    DiversityOption(
        "lighting.flash_freeze",
        "Use direct flash that freezes detail and creates sharp, immediate shadows.",
        1.0,
    ),
    DiversityOption(
        "lighting.flickering_source",
        "Use a small flickering light source with deep falloff and sculpted darkness.",
        1.0,
    ),
    DiversityOption(
        "lighting.clear_side_light",
        "Use clear side light entering from one direction with long gentle shadows.",
        1.0,
    ),
    DiversityOption(
        "lighting.mist_diffusion",
        "Use diffuse light through atmospheric haze, preserving shape while reducing contrast.",
        1.0,
    ),
    DiversityOption(
        "lighting.mixed_sources",
        "Use visibly mixed color temperatures so different light sources create spatial layers.",
        1.0,
    ),
    DiversityOption(
        "lighting.reflective_bounce",
        "Use reflected fill light that reveals surfaces without flattening their form.",
        1.0,
    ),
)

_PALETTE_POOL: tuple[DiversityOption, ...] = (
    DiversityOption(
        "palette.warm_neutral",
        "Use a restrained warm-neutral palette with modest tonal variation and low saturation.",
        0.45,
    ),
    DiversityOption(
        "palette.cobalt_coral",
        "Use deep cobalt and coral accents against a balanced neutral ground.",
        1.0,
    ),
    DiversityOption(
        "palette.moss_rust",
        "Use moss green, rust red, and charcoal with earthy separation.",
        1.0,
    ),
    DiversityOption(
        "palette.ice_lilac",
        "Use icy blue, pale lilac, and silver with cool atmospheric restraint.",
        1.0,
    ),
    DiversityOption(
        "palette.black_white_accent",
        "Use near-black and off-white with one sharply controlled accent color.",
        1.0,
    ),
    DiversityOption(
        "palette.citrus_teal",
        "Use citrus yellow and teal with lively, clean contrast.",
        1.0,
    ),
    DiversityOption(
        "palette.ochre_violet",
        "Use ochre and violet balanced by dark plum and muted stone.",
        1.0,
    ),
    DiversityOption(
        "palette.pastel_multicolor",
        "Use softened rose, mint, sky, and apricot in gentle layered transitions.",
        1.0,
    ),
    DiversityOption(
        "palette.verdant",
        "Use dense greens with small flashes of bright color and deep natural shadow.",
        1.0,
    ),
    DiversityOption(
        "palette.metallic_signal",
        "Use graphite, silver, and one vivid signal hue with precise technical contrast.",
        1.0,
    ),
    DiversityOption(
        "palette.sunset",
        "Use coral, amber, plum, and violet with dramatic late-day tonal range.",
        1.0,
    ),
    DiversityOption(
        "palette.desaturated_blue",
        "Use desaturated blue, fog gray, and faded tan for spacious quiet separation.",
        1.0,
    ),
)

_STYLE_POOL: tuple[DiversityOption, ...] = (
    DiversityOption(
        "style.documentary_photo",
        "Use a photorealistic documentary photograph with candid detail and natural imperfections.",
        0.45,
    ),
    DiversityOption(
        "style.editorial_illustration",
        "Use a crisp editorial illustration with deliberate shape language and selective detail.",
        1.0,
    ),
    DiversityOption(
        "style.screenprint",
        "Use a layered screen-print aesthetic with visible ink texture and simplified forms.",
        1.0,
    ),
    DiversityOption(
        "style.watercolor",
        "Use translucent watercolor washes, soft edges, and paper grain as visual structure.",
        1.0,
    ),
    DiversityOption(
        "style.oil_paint",
        "Use tactile oil-paint brushwork with layered pigment and expressive surface variation.",
        1.0,
    ),
    DiversityOption(
        "style.graphic_poster",
        "Use bold graphic-poster forms, strong silhouettes, and economical visual detail.",
        1.0,
    ),
    DiversityOption(
        "style.collage",
        "Use a mixed collage of cut shapes, found textures, and carefully aligned fragments.",
        1.0,
    ),
    DiversityOption(
        "style.ink_drawing",
        "Use an ink drawing with varied line weight, hatching, and intentional negative space.",
        1.0,
    ),
    DiversityOption(
        "style.three_d_render",
        "Use a polished 3D render with physically coherent materials and designed lighting.",
        1.0,
    ),
    DiversityOption(
        "style.pixel_art",
        "Use a pixel-art treatment with deliberate block scale, limited shading, and clear silhouettes.",
        1.0,
    ),
    DiversityOption(
        "style.film_still",
        "Use a cinematic film-still look with grain, composed color, and observational framing.",
        1.0,
    ),
    DiversityOption(
        "style.natural_history_plate",
        "Use a precise natural-history plate style with labeled visual structure and restrained ornament.",
        1.0,
    ),
)

_SETTING_POOL: tuple[DiversityOption, ...] = (
    DiversityOption(
        "setting.wooden_workbench",
        "Place the scene on a wooden workbench or desk with visible grain and practical wear.",
        0.45,
    ),
    DiversityOption(
        "setting.concrete_floor",
        "Place the scene on a cool concrete floor with broad negative space and industrial texture.",
        1.0,
    ),
    DiversityOption(
        "setting.grass_field",
        "Place the scene in an open grass field with layered distance and changing ground cover.",
        1.0,
    ),
    DiversityOption(
        "setting.patterned_tile",
        "Place the scene against patterned tile with repeating geometry and clean divisions.",
        1.0,
    ),
    DiversityOption(
        "setting.dark_studio",
        "Place the scene in a controlled dark studio with a seamless ground and isolated focus.",
        1.0,
    ),
    DiversityOption(
        "setting.coastal_edge",
        "Place the scene near an exposed shoreline with wind, horizon, and tactile natural surfaces.",
        1.0,
    ),
    DiversityOption(
        "setting.greenhouse",
        "Place the scene in a dense greenhouse with translucent structure and layered leaves.",
        1.0,
    ),
    DiversityOption(
        "setting.city_street",
        "Place the scene in an active city street with architectural context and incidental movement.",
        1.0,
    ),
    DiversityOption(
        "setting.rock_shelf",
        "Place the scene on a natural rock shelf with weathered texture and uneven footing.",
        1.0,
    ),
    DiversityOption(
        "setting.metal_workshop",
        "Place the scene in a metalworking space with durable surfaces and functional clutter.",
        1.0,
    ),
    DiversityOption(
        "setting.domestic_textile",
        "Place the scene against a soft textile surface with folds, seams, and gentle irregularity.",
        1.0,
    ),
    DiversityOption(
        "setting.gallery_wall",
        "Place the scene against a spare gallery wall with deliberate negative space and controlled context.",
        1.0,
    ),
)


def _sample_weighted_without_replacement(
    pool: tuple[DiversityOption, ...], count: int, rng: random.Random
) -> tuple[DiversityOption, ...]:
    """Draw ``count`` options proportionally to weight, without replacement."""
    remaining = list(pool)
    selected: list[DiversityOption] = []
    for _ in range(count):
        total_weight = sum(option.weight for option in remaining)
        threshold = rng.random() * total_weight
        cumulative_weight = 0.0
        for index, option in enumerate(remaining):
            cumulative_weight += option.weight
            if threshold < cumulative_weight:
                selected.append(remaining.pop(index))
                break
        else:  # pragma: no cover - random.Random.random() is always below 1.0
            selected.append(remaining.pop())
    return tuple(selected)


def sample_image_diversity(rng: random.Random) -> ImageDiversity:
    """Sample the six art-direction axes using the caller's random stream."""
    return ImageDiversity(
        framings=_sample_weighted_without_replacement(_FRAMING_POOL, 1, rng),
        subjects=_sample_weighted_without_replacement(_SUBJECT_POOL, 1, rng),
        lighting=_sample_weighted_without_replacement(_LIGHTING_POOL, 1, rng),
        palettes=_sample_weighted_without_replacement(_PALETTE_POOL, 1, rng),
        styles=_sample_weighted_without_replacement(_STYLE_POOL, 1, rng),
        settings=_sample_weighted_without_replacement(_SETTING_POOL, 1, rng),
    )


def render_image_diversity(matrix: ImageDiversity) -> str:
    """Render sampled directions in the generator's exact prompt format."""
    return textwrap.dedent(
        f"""\
        Image art direction (sampled for this generation; blend it with the persona request rather than treating it as a replacement):
        - Framing and camera: {"; ".join(option.text for option in matrix.framings)}
        - Subject focus: {"; ".join(option.text for option in matrix.subjects)}
        - Lighting situation: {"; ".join(option.text for option in matrix.lighting)}
        - Palette and mood: {"; ".join(option.text for option in matrix.palettes)}
        - Visual medium and style: {"; ".join(option.text for option in matrix.styles)}
        - Setting and surface: {"; ".join(option.text for option in matrix.settings)}
        Keep the requested subject and scene. Apply each sampled direction where the prompt leaves that axis unspecified. If the prompt explicitly contradicts a sampled direction, the prompt wins for that axis.
        """
    ).strip()


def diversity_ids(matrix: ImageDiversity) -> dict[str, tuple[str, ...]]:
    """Return selected IDs in their sampling order for provenance."""
    return {
        "framings": tuple(option.id for option in matrix.framings),
        "subjects": tuple(option.id for option in matrix.subjects),
        "lighting": tuple(option.id for option in matrix.lighting),
        "palettes": tuple(option.id for option in matrix.palettes),
        "styles": tuple(option.id for option in matrix.styles),
        "settings": tuple(option.id for option in matrix.settings),
    }


__all__ = [
    "DiversityOption",
    "ImageDiversity",
    "diversity_ids",
    "render_image_diversity",
    "sample_image_diversity",
]
