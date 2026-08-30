"""Sample and render varied art direction for generated websites.

The pools in this module are deliberately plain data. Sampling is local and
side-effect-free so callers can provide the random stream used for a run.
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
class WebsiteDiversity:
    """The selected art-direction options for one website generation."""

    genres: tuple[DiversityOption, ...]
    layouts: tuple[DiversityOption, ...]
    moods: tuple[DiversityOption, ...]
    typography: tuple[DiversityOption, ...]
    rhythms: tuple[DiversityOption, ...]


_GENRE_POOL: tuple[DiversityOption, ...] = (
    DiversityOption(
        "genre.newsroom",
        "A publication-like site organized around timely stories, headlines, "
        "categories, and editorial prominence.",
        1.0,
    ),
    DiversityOption(
        "genre.marketplace",
        "A commerce-like site organized around offerings, comparison, "
        "discovery, and clear calls to explore.",
        1.0,
    ),
    DiversityOption(
        "genre.portfolio",
        "A portfolio-like site that foregrounds selected work, case studies, "
        "and a distinctive creator identity.",
        1.0,
    ),
    DiversityOption(
        "genre.community",
        "A community-like site centered on member voices, participation, "
        "reactions, and social discovery.",
        1.0,
    ),
    DiversityOption(
        "genre.event_hub",
        "An event-like site for schedules, speakers or performers, locations, "
        "and planning a visit.",
        1.0,
    ),
    DiversityOption(
        "genre.campaign",
        "A campaign-like site with a strong point of view, persuasive "
        "storytelling, and a clear way to learn or participate.",
        1.0,
    ),
    DiversityOption(
        "genre.restaurant",
        "A hospitality-like site that makes a place feel tangible through "
        "offerings, atmosphere, hours, and invitations.",
        1.0,
    ),
    DiversityOption(
        "genre.travel_guide",
        "A destination-like site combining orientation, recommendations, "
        "routes, and a sense of place.",
        1.0,
    ),
    DiversityOption(
        "genre.clubhouse",
        "A membership-like site with an identity, recurring activities, "
        "announcements, and ways to belong.",
        1.0,
    ),
    DiversityOption(
        "genre.product_launch",
        "A launch-like site presenting a new product or idea through benefits, "
        "proof, visual emphasis, and calls to discover more.",
        1.0,
    ),
    DiversityOption(
        "genre.docs_reference",
        "A reference-like site organized for lookup with hierarchy, "
        "cross-references, and quick scanning.",
        0.45,
    ),
    DiversityOption(
        "genre.tutorial",
        "A tutorial-like site that guides a reader through concepts or steps "
        "with progressive explanation.",
        0.45,
    ),
)

_LAYOUT_POOL: tuple[DiversityOption, ...] = (
    DiversityOption(
        "layout.editorial_grid",
        "Use a wide editorial grid with a prominent lead area, secondary cards, "
        "and visibly varied column spans.",
        1.0,
    ),
    DiversityOption(
        "layout.split_hero",
        "Use a split composition pairing an expressive visual or statement with "
        "supporting content and controls.",
        1.0,
    ),
    DiversityOption(
        "layout.dashboard",
        "Use a dashboard-like arrangement of panels, status areas, filters, and "
        "compact summaries.",
        1.0,
    ),
    DiversityOption(
        "layout.sidebar",
        "Use a persistent sidebar or rail beside the main content, with clear "
        "active and secondary destinations.",
        1.0,
    ),
    DiversityOption(
        "layout.catalog",
        "Use a browseable catalog of repeated cards or tiles with grouping, "
        "sorting, and comparison cues.",
        1.0,
    ),
    DiversityOption(
        "layout.timeline",
        "Use a chronological or process-oriented flow with connected milestones "
        "and varied detail blocks.",
        1.0,
    ),
    DiversityOption(
        "layout.mosaic",
        "Use an asymmetrical mosaic of differently sized visual and text regions "
        "rather than one centered column.",
        1.0,
    ),
    DiversityOption(
        "layout.longform",
        "Use a longform storytelling composition with sectional breaks, pull "
        "quotes, media moments, and progress cues.",
        1.0,
    ),
    DiversityOption(
        "layout.portal",
        "Use a layered portal with a strong masthead, navigation band, featured "
        "destinations, and a substantial footer.",
        1.0,
    ),
    DiversityOption(
        "layout.conversation",
        "Use a conversation-oriented layout with thread hierarchy, participant "
        "identity, and response affordances.",
        1.0,
    ),
    DiversityOption(
        "layout.stepper",
        "Use a guided sequence with a visible progression indicator, focused "
        "current step, and supporting navigation.",
        1.0,
    ),
    DiversityOption(
        "layout.compare",
        "Use side-by-side comparison regions with aligned labels, distinctions, "
        "and a clear conclusion area.",
        1.0,
    ),
)

_MOOD_POOL: tuple[DiversityOption, ...] = (
    DiversityOption(
        "mood.neon_night",
        "A high-contrast nocturnal mood: near-black ground, electric accents, "
        "luminous edges, and purposeful glow.",
        1.0,
    ),
    DiversityOption(
        "mood.sunlit_citrus",
        "A bright optimistic mood: warm daylight neutrals, citrus highlights, "
        "generous whitespace, and crisp energy.",
        1.0,
    ),
    DiversityOption(
        "mood.deep_ocean",
        "A calm immersive mood: layered blue-green tones, cool highlights, "
        "depth, and restrained contrast.",
        1.0,
    ),
    DiversityOption(
        "mood.earth_clay",
        "A tactile grounded mood: clay, terracotta, moss, and charcoal with "
        "organic warmth.",
        1.0,
    ),
    DiversityOption(
        "mood.candy_pop",
        "A playful saturated mood: confident blocks of color, soft contrast, "
        "and lively accent collisions.",
        1.0,
    ),
    DiversityOption(
        "mood.monochrome_studio",
        "A disciplined monochrome mood with one sharply controlled accent and "
        "gallery-like restraint.",
        1.0,
    ),
    DiversityOption(
        "mood.pastel_dusk",
        "A gentle evening mood: desaturated lavender, blue, and peach with soft "
        "transitions and quiet depth.",
        1.0,
    ),
    DiversityOption(
        "mood.high_contrast_ink",
        "A graphic poster mood built from stark light and dark fields, decisive "
        "color, and bold visual hierarchy.",
        1.0,
    ),
    DiversityOption(
        "mood.forest_canopy",
        "A layered natural mood: deep greens, filtered light, bark-like darks, "
        "and small bioluminescent accents.",
        1.0,
    ),
    DiversityOption(
        "mood.metallic_signal",
        "A technical futuristic mood: graphite, silver, cool light, and one "
        "signal color used as a system cue.",
        1.0,
    ),
    DiversityOption(
        "mood.coastal_fog",
        "A spacious atmospheric mood: misty neutrals, faded blue, diffuse "
        "borders, and quiet horizon-like separation.",
        1.0,
    ),
    DiversityOption(
        "mood.warm_sunset",
        "A dramatic welcoming mood: coral, amber, plum, and dark violet with "
        "strong late-day contrast.",
        1.0,
    ),
)

_TYPOGRAPHY_POOL: tuple[DiversityOption, ...] = (
    DiversityOption(
        "type.modern_grotesk",
        "Use a clean contemporary sans voice with large confident headings, "
        "compact labels, and measured tracking.",
        1.0,
    ),
    DiversityOption(
        "type.humanist_sans",
        "Use a friendly humanist sans voice with open shapes, approachable "
        "hierarchy, and conversational captions.",
        1.0,
    ),
    DiversityOption(
        "type.display_serif",
        "Use an expressive display serif for major statements paired with a "
        "restrained readable companion face.",
        1.0,
    ),
    DiversityOption(
        "type.condensed_poster",
        "Use condensed high-impact lettering for headlines, compact navigation "
        "labels, and poster-like rhythm.",
        1.0,
    ),
    DiversityOption(
        "type.rounded_soft",
        "Use rounded letterforms, generous counters, and a warm informal "
        "hierarchy without becoming childish.",
        1.0,
    ),
    DiversityOption(
        "type.monospaced_signal",
        "Use monospaced type selectively as a deliberate interface signal, "
        "contrasted with a more expressive reading face.",
        1.0,
    ),
    DiversityOption(
        "type.editorial_contrast",
        "Use strong contrast between display and body roles, with clear scale "
        "jumps and magazine-like pacing.",
        1.0,
    ),
    DiversityOption(
        "type.kinetic_oversized",
        "Use oversized type as a compositional shape, allowing headings to "
        "overlap or break the usual column width.",
        1.0,
    ),
    DiversityOption(
        "type.minimalist_neutral",
        "Use quiet neutral typography, precise spacing, and hierarchy carried "
        "by scale and alignment rather than decoration.",
        1.0,
    ),
    DiversityOption(
        "type.handmade_accent",
        "Use a legible primary face plus occasional handmade-looking accent "
        "lettering for personality and emphasis.",
        1.0,
    ),
    DiversityOption(
        "type.pixel_digital",
        "Use a digital display character for labels or highlights, balanced by a "
        "highly readable text face.",
        1.0,
    ),
    DiversityOption(
        "type.classical_formal",
        "Use formal proportioned lettering and disciplined hierarchy to create a "
        "ceremonial, established voice.",
        1.0,
    ),
)

_RHYTHM_POOL: tuple[DiversityOption, ...] = (
    DiversityOption(
        "rhythm.discovery",
        "Favor scanning and discovery: teasers, filters, related destinations, "
        "and multiple inviting entry points.",
        1.0,
    ),
    DiversityOption(
        "rhythm.narrative",
        "Favor a paced narrative: reveal context progressively with transitions "
        "between major sections.",
        1.0,
    ),
    DiversityOption(
        "rhythm.reference",
        "Favor fast lookup: concise summaries, anchors, search-like controls, "
        "and predictable repeated structure.",
        1.0,
    ),
    DiversityOption(
        "rhythm.comparison",
        "Favor evaluation: surface distinctions, pros and cons, metrics, and "
        "explicit decision support.",
        1.0,
    ),
    DiversityOption(
        "rhythm.participatory",
        "Favor participation: reactions, tabs, toggles, voting-like controls, "
        "and visible community response.",
        1.0,
    ),
    DiversityOption(
        "rhythm.serendipity",
        "Favor playful wandering: unexpected links, rotating highlights, "
        "easter-egg-like details, and varied scale.",
        1.0,
    ),
    DiversityOption(
        "rhythm.guided",
        "Favor a guided path: clear next actions, progress cues, and a "
        "beginning-to-end sequence.",
        1.0,
    ),
    DiversityOption(
        "rhythm.showcase",
        "Favor visual showcase: large focal moments, captions, selected "
        "highlights, and deliberate breathing room.",
        1.0,
    ),
    DiversityOption(
        "rhythm.utility",
        "Favor practical use: dense but legible controls, status feedback, and "
        "quick completion of small tasks.",
        1.0,
    ),
    DiversityOption(
        "rhythm.quiet",
        "Favor contemplative reading: fewer controls, generous spacing, and "
        "carefully chosen moments of emphasis.",
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


def sample_website_diversity(rng: random.Random) -> WebsiteDiversity:
    """Sample the five art-direction axes using the caller's random stream."""
    return WebsiteDiversity(
        genres=_sample_weighted_without_replacement(_GENRE_POOL, 2, rng),
        layouts=_sample_weighted_without_replacement(_LAYOUT_POOL, 2, rng),
        moods=_sample_weighted_without_replacement(_MOOD_POOL, 2, rng),
        typography=_sample_weighted_without_replacement(_TYPOGRAPHY_POOL, 2, rng),
        rhythms=_sample_weighted_without_replacement(_RHYTHM_POOL, 1, rng),
    )


def render_website_diversity(matrix: WebsiteDiversity) -> str:
    """Render a sampled matrix in the generator's exact prompt format."""
    return textwrap.dedent(
        f"""\
        Website art direction matrix (sampled for this generation; treat it as a coherent constraint, not a list to copy verbatim):
        - Site archetypes: {"; ".join(option.text for option in matrix.genres)}
        - Layout structures: {"; ".join(option.text for option in matrix.layouts)}
        - Visual moods and palettes: {"; ".join(option.text for option in matrix.moods)}
        - Typographic character: {"; ".join(option.text for option in matrix.typography)}
        - Content rhythm and interaction: {"; ".join(option.text for option in matrix.rhythms)}
        Interpret the matrix through the persona brief, invent concrete content, and make the result feel like a complete independent website. Prefer visible navigation, section links, and a footer when the selected structure calls for them; in-page links may be inert anchors.
        """
    ).strip()


def diversity_ids(matrix: WebsiteDiversity) -> dict[str, tuple[str, ...]]:
    """Return selected IDs in their sampling order for provenance."""
    return {
        "genres": tuple(option.id for option in matrix.genres),
        "layouts": tuple(option.id for option in matrix.layouts),
        "moods": tuple(option.id for option in matrix.moods),
        "typography": tuple(option.id for option in matrix.typography),
        "rhythms": tuple(option.id for option in matrix.rhythms),
    }


__all__ = [
    "DiversityOption",
    "WebsiteDiversity",
    "diversity_ids",
    "render_website_diversity",
    "sample_website_diversity",
]
