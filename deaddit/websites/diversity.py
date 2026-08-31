"""Sample and render one coherent art direction for generated websites.

The pools in this module are deliberately plain data. Sampling is local and
side-effect-free so callers can provide the random stream used for a run. A
website direction chooses its archetype first; the layout is then selected
from that archetype's compatible allowlist, while the remaining axes each
receive exactly one explicit value.
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
    imagery: tuple[DiversityOption, ...]

    @property
    def direction_id(self) -> str:
        """Return the archetype's stable direction ID."""
        if len(self.genres) != 1:
            raise ValueError("website archetype must have exactly one value")
        return self.genres[0].id


# These are both the public provenance vocabulary (through ``diversity_ids``)
# and the complete set of directions accepted by ``direction_id``.
_GENRE_POOL: tuple[DiversityOption, ...] = (
    DiversityOption(
        "website.news_report",
        "A timely news report with a strong masthead, headline hierarchy, bylines, and clearly dated stories.",
        1.0,
    ),
    DiversityOption(
        "website.magazine_feature",
        "A richly paced magazine feature with an authored point of view, visual pull quotes, and substantial sections.",
        1.0,
    ),
    DiversityOption(
        "website.personal_blog",
        "A personal blog with an unmistakable author voice, dated entries, related posts, and a readable stream.",
        1.0,
    ),
    DiversityOption(
        "website.community_portal",
        "A community portal organized around member voices, discussions, announcements, and participation.",
        1.0,
    ),
    DiversityOption(
        "website.event_program",
        "An event program that helps visitors understand sessions, people, place, and timing.",
        1.0,
    ),
    DiversityOption(
        "website.local_business",
        "A local business presence focused on offerings, practical details, trust, and an invitation to visit.",
        1.0,
    ),
    DiversityOption(
        "website.nonprofit_campaign",
        "A nonprofit campaign with a clear cause, evidence, human stakes, and meaningful ways to participate.",
        1.0,
    ),
    DiversityOption(
        "website.product_page",
        "A product page that explains a focused offering through benefits, proof, details, and confident calls to action.",
        1.0,
    ),
    DiversityOption(
        "website.catalog",
        "A catalog for browsing and comparing a collection of items with useful labels and predictable discovery.",
        1.0,
    ),
    DiversityOption(
        "website.reference",
        "A reference site optimized for lookup, hierarchy, cross-references, and fast scanning.",
        1.0,
    ),
    DiversityOption(
        "website.data_dashboard",
        "A data dashboard that makes changing measurements, status, and comparisons legible at a glance.",
        1.0,
    ),
    DiversityOption(
        "website.interactive_utility",
        "An interactive utility designed for completing a small practical task with clear state and feedback.",
        1.0,
    ),
    DiversityOption(
        "website.fan_archive",
        "A fan archive preserving a subject's history through indexes, chronology, collections, and provenance.",
        1.0,
    ),
    DiversityOption(
        "website.travel_guide",
        "A travel guide combining orientation, recommendations, routes, and a vivid sense of place.",
        1.0,
    ),
    DiversityOption(
        "website.portfolio",
        "A creator portfolio foregrounding selected work, case studies, and a distinctive professional identity.",
        1.0,
    ),
    DiversityOption(
        "website.experimental_microsite",
        "An experimental microsite using an unusual but legible visual system to make one idea memorable.",
        1.0,
    ),
)

_LAYOUT_POOL: tuple[DiversityOption, ...] = (
    DiversityOption(
        "layout.newsroom",
        "Use a newsroom layout with a masthead, section navigation, lead story, and compact story rows.",
        1.0,
    ),
    DiversityOption(
        "layout.magazine_spread",
        "Use a magazine spread with an asymmetric lead, pull quotes, captions, and editorial section breaks.",
        1.0,
    ),
    DiversityOption(
        "layout.blog_stream",
        "Use a readable blog stream with an entry list, dates, tags, and a modest author rail.",
        1.0,
    ),
    DiversityOption(
        "layout.community_portal",
        "Use a portal layout with a navigation band, featured destinations, member activity, and a substantial footer.",
        1.0,
    ),
    DiversityOption(
        "layout.event_schedule",
        "Use a schedule layout with a visible timeline, session cards, venue details, and practical wayfinding.",
        1.0,
    ),
    DiversityOption(
        "layout.service_landing",
        "Use a service layout with a compact value statement, offering blocks, proof, and practical contact details.",
        1.0,
    ),
    DiversityOption(
        "layout.campaign_story",
        "Use a campaign story layout that moves from an urgent premise through evidence to participation.",
        1.0,
    ),
    DiversityOption(
        "layout.product_landing",
        "Use a product landing layout with a focused hero, benefit sequence, proof points, and an action area.",
        1.0,
    ),
    DiversityOption(
        "layout.catalog_grid",
        "Use a browseable catalog grid with repeated cards, grouping cues, filters, and comparison-friendly labels.",
        1.0,
    ),
    DiversityOption(
        "layout.reference_sidebar",
        "Use a persistent reference sidebar beside the main article with anchors, breadcrumbs, and cross-links.",
        1.0,
    ),
    DiversityOption(
        "layout.dashboard_panels",
        "Use a dashboard arrangement of panels, status summaries, filters, and compact comparisons.",
        1.0,
    ),
    DiversityOption(
        "layout.utility_workspace",
        "Use a practical workspace with one prominent control, concise instructions, result state, and reset affordance.",
        1.0,
    ),
    DiversityOption(
        "layout.archive_index",
        "Use an archive index with chronological navigation, collection groupings, and provenance details.",
        1.0,
    ),
    DiversityOption(
        "layout.travel_map",
        "Use a travel guide layout pairing orientation or route cues with recommendations and place notes.",
        1.0,
    ),
    DiversityOption(
        "layout.portfolio_masonry",
        "Use a portfolio layout with an asymmetric project grid, selected case study, and restrained creator details.",
        1.0,
    ),
    DiversityOption(
        "layout.experimental_canvas",
        "Use an experimental canvas with an intentional visual gesture, modular sections, and clear fallback reading order.",
        1.0,
    ),
)

# A direction can have multiple compatible structures, but never receives a
# layout from another archetype's family. The tuples contain stable IDs only so
# the option copy remains defined once in _LAYOUT_POOL.
_LAYOUT_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "website.news_report": ("layout.newsroom", "layout.magazine_spread"),
    "website.magazine_feature": ("layout.magazine_spread", "layout.newsroom"),
    "website.personal_blog": ("layout.blog_stream", "layout.magazine_spread"),
    "website.community_portal": ("layout.community_portal", "layout.blog_stream"),
    "website.event_program": ("layout.event_schedule", "layout.community_portal"),
    "website.local_business": ("layout.service_landing", "layout.catalog_grid"),
    "website.nonprofit_campaign": ("layout.campaign_story", "layout.magazine_spread"),
    "website.product_page": ("layout.product_landing", "layout.service_landing"),
    "website.catalog": ("layout.catalog_grid", "layout.archive_index"),
    "website.reference": ("layout.reference_sidebar", "layout.archive_index"),
    "website.data_dashboard": ("layout.dashboard_panels", "layout.reference_sidebar"),
    "website.interactive_utility": (
        "layout.utility_workspace",
        "layout.dashboard_panels",
    ),
    "website.fan_archive": ("layout.archive_index", "layout.reference_sidebar"),
    "website.travel_guide": ("layout.travel_map", "layout.magazine_spread"),
    "website.portfolio": ("layout.portfolio_masonry", "layout.magazine_spread"),
    "website.experimental_microsite": (
        "layout.experimental_canvas",
        "layout.portfolio_masonry",
    ),
}

_MOOD_POOL: tuple[DiversityOption, ...] = (
    DiversityOption(
        "mood.light_daylight",
        "Use an explicit light mode: clear daylight neutrals, crisp ink, and one calm accent with generous but purposeful contrast.",
        1.0,
    ),
    DiversityOption(
        "mood.light_plain",
        "Use an explicit light mode: plain white and soft gray surfaces, restrained borders, and minimal decoration.",
        1.0,
    ),
    DiversityOption(
        "mood.light_citrus",
        "Use an explicit light mode: bright warm ground, confident citrus accents, and clean high-contrast type.",
        1.0,
    ),
    DiversityOption(
        "mood.light_poster",
        "Use an explicit light mode: an off-white ground with graphic color blocks and decisive poster-like contrast.",
        1.0,
    ),
    DiversityOption(
        "mood.dark_ink",
        "Use an explicit dark mode: near-black ink surfaces, warm white type, and one controlled signal accent.",
        1.0,
    ),
    DiversityOption(
        "mood.dark_ocean",
        "Use an explicit dark mode: deep blue-green surfaces, cool highlights, and layered readable contrast.",
        1.0,
    ),
    DiversityOption(
        "mood.dark_signal",
        "Use an explicit dark mode: graphite surfaces, silver dividers, and a bright signal color used sparingly.",
        1.0,
    ),
    DiversityOption(
        "mood.dark_forest",
        "Use an explicit dark mode: deep green-black surfaces, filtered accents, and quiet luminous details.",
        1.0,
    ),
)

_TYPOGRAPHY_POOL: tuple[DiversityOption, ...] = (
    DiversityOption(
        "type.modern_grotesk",
        "Use a clean contemporary sans system with confident headings, compact labels, and measured tracking.",
        1.0,
    ),
    DiversityOption(
        "type.humanist_sans",
        "Use a friendly humanist sans system with open shapes, approachable hierarchy, and conversational captions.",
        1.0,
    ),
    DiversityOption(
        "type.editorial_serif",
        "Use an expressive serif for major statements paired with a restrained, highly readable companion face.",
        1.0,
    ),
    DiversityOption(
        "type.condensed_poster",
        "Use condensed high-impact lettering for headlines and compact navigation labels with poster-like rhythm.",
        1.0,
    ),
    DiversityOption(
        "type.monospaced_signal",
        "Use monospaced type for labels and data signals, contrasted with a comfortable reading face.",
        1.0,
    ),
    DiversityOption(
        "type.minimal_neutral",
        "Use quiet neutral typography with precise spacing; let scale and alignment carry hierarchy.",
        1.0,
    ),
    DiversityOption(
        "type.handmade_accent",
        "Use a legible primary face with occasional handmade-looking accent lettering for personality.",
        1.0,
    ),
    DiversityOption(
        "type.classical_formal",
        "Use formal proportioned lettering and disciplined hierarchy for an established, ceremonial voice.",
        1.0,
    ),
)

_RHYTHM_POOL: tuple[DiversityOption, ...] = (
    DiversityOption(
        "rhythm.discovery",
        "Favor scanning and discovery through teasers, filters, related destinations, and several entry points.",
        1.0,
    ),
    DiversityOption(
        "rhythm.narrative",
        "Favor a paced narrative that reveals context progressively through transitions between sections.",
        1.0,
    ),
    DiversityOption(
        "rhythm.reference",
        "Favor fast lookup with concise summaries, anchors, search-like controls, and predictable repetition.",
        1.0,
    ),
    DiversityOption(
        "rhythm.participatory",
        "Favor participation through reactions, tabs, toggles, and visible community response.",
        1.0,
    ),
    DiversityOption(
        "rhythm.guided",
        "Favor a guided path with clear next actions, progress cues, and a beginning-to-end sequence.",
        1.0,
    ),
    DiversityOption(
        "rhythm.showcase",
        "Favor visual showcase with focal moments, captions, selected highlights, and breathing room.",
        1.0,
    ),
    DiversityOption(
        "rhythm.utilitarian_dense",
        "Favor a utilitarian dense rhythm: compact controls, status feedback, and quick completion of practical tasks.",
        1.0,
    ),
    DiversityOption(
        "rhythm.plain_minimal",
        "Favor a plain minimal rhythm: few controls, simple repeated blocks, and generous quiet space.",
        1.0,
    ),
)

_IMAGERY_POOL: tuple[DiversityOption, ...] = (
    DiversityOption(
        "imagery.inline_illustration",
        "Use small inline SVG or CSS illustrations where they clarify the subject; do not require a decorative hero.",
        1.0,
    ),
    DiversityOption(
        "imagery.photo_cards",
        "Use restrained CSS-built photo-card placeholders and captions; keep the content hierarchy ahead of imagery.",
        1.0,
    ),
    DiversityOption(
        "imagery.diagrammatic",
        "Use diagrams, rules, and simple CSS shapes to explain relationships rather than a hero picture.",
        1.0,
    ),
    DiversityOption(
        "imagery.data_visual",
        "Use compact charts, sparklines, or metric marks built from HTML and CSS where useful.",
        1.0,
    ),
    DiversityOption(
        "imagery.pattern_texture",
        "Use a small repeated pattern or texture as decoration, with content remaining the dominant signal.",
        1.0,
    ),
    DiversityOption(
        "imagery.screenshot_mockup",
        "Use CSS-framed interface mockups or document fragments as evidence of the site's subject.",
        1.0,
    ),
    DiversityOption(
        "imagery.none_minimal",
        "Use no decorative imagery: rely on typography, spacing, borders, and purposeful color fields.",
        1.0,
    ),
    DiversityOption(
        "imagery.artifact_collection",
        "Use an orderly collection of small artifacts, badges, or marks with captions and provenance.",
        1.0,
    ),
)

_OPTION_BY_ID = {
    option.id: option
    for pool in (
        _GENRE_POOL,
        _LAYOUT_POOL,
        _MOOD_POOL,
        _TYPOGRAPHY_POOL,
        _RHYTHM_POOL,
        _IMAGERY_POOL,
    )
    for option in pool
}


def _sample_weighted_without_replacement(
    pool: tuple[DiversityOption, ...], count: int, rng: random.Random
) -> tuple[DiversityOption, ...]:
    """Draw ``count`` options proportionally to weight, without replacement."""
    if count < 0 or count > len(pool):
        raise ValueError("sample count must fit within the option pool")
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


def sample_website_diversity(
    rng: random.Random, *, direction_id: str | None = None
) -> WebsiteDiversity:
    """Sample one coherent website direction using the caller's random stream.

    ``direction_id`` pins the archetype while still sampling the compatible
    layout and visual axes. With no direction, the archetype itself is sampled
    from the same caller-provided RNG, preserving deterministic seeded runs.
    """
    if direction_id is None:
        archetype = _sample_weighted_without_replacement(_GENRE_POOL, 1, rng)[0]
        direction_id = archetype.id
    else:
        archetype = _OPTION_BY_ID.get(direction_id)
        if archetype is None or not direction_id.startswith("website."):
            raise ValueError(f"unknown website direction ID: {direction_id!r}")

    layout_ids = _LAYOUT_ALLOWLIST.get(direction_id)
    if layout_ids is None:
        raise ValueError(f"unknown website direction ID: {direction_id!r}")
    layouts = tuple(_OPTION_BY_ID[layout_id] for layout_id in layout_ids)

    return WebsiteDiversity(
        genres=(archetype,),
        layouts=_sample_weighted_without_replacement(layouts, 1, rng),
        moods=_sample_weighted_without_replacement(_MOOD_POOL, 1, rng),
        typography=_sample_weighted_without_replacement(_TYPOGRAPHY_POOL, 1, rng),
        rhythms=_sample_weighted_without_replacement(_RHYTHM_POOL, 1, rng),
        imagery=_sample_weighted_without_replacement(_IMAGERY_POOL, 1, rng),
    )


def _selected(axis: tuple[DiversityOption, ...], axis_name: str) -> DiversityOption:
    """Return the sole selected option, rejecting contradictory matrices."""
    if len(axis) != 1:
        raise ValueError(
            f"website direction axis {axis_name!r} must have exactly one value"
        )
    return axis[0]


def render_website_diversity(matrix: WebsiteDiversity) -> str:
    """Render one decisive, coherent brief for the website generator."""
    archetype = _selected(matrix.genres, "archetype")
    layout = _selected(matrix.layouts, "layout")
    mood = _selected(matrix.moods, "color mode")
    typography = _selected(matrix.typography, "typography")
    rhythm = _selected(matrix.rhythms, "density and rhythm")
    imagery = _selected(matrix.imagery, "imagery and decoration")
    if matrix.direction_id != archetype.id:
        raise ValueError("website direction ID must match the selected archetype")
    if layout.id not in _LAYOUT_ALLOWLIST.get(matrix.direction_id, ()):
        raise ValueError(
            f"layout {layout.id!r} is incompatible with {matrix.direction_id!r}"
        )
    return textwrap.dedent(
        f"""\
        Website art direction (one authoritative direction; follow every selected axis):
        - Direction and site archetype [{matrix.direction_id}]: {archetype.text}
        - Compatible layout: {layout.text}
        - Explicit color mode and mood: {mood.text}
        - Typography system: {typography.text}
        - Density and content rhythm: {rhythm.text}
        - Imagery and decoration strategy: {imagery.text}
        Preserve the persona's site subject and content. Commit to these selected archetype, layout, mode, type, rhythm, and imagery choices as visually decisive; do not describe the art direction in the page. Build a complete independent website with visible navigation, useful section links, and a footer when they fit the selected structure.\
        """
    ).strip()


def diversity_ids(matrix: WebsiteDiversity) -> dict[str, tuple[str, ...]]:
    """Return selected IDs in stable axis order for JSON-safe provenance."""
    return {
        "genres": tuple(option.id for option in matrix.genres),
        "layouts": tuple(option.id for option in matrix.layouts),
        "moods": tuple(option.id for option in matrix.moods),
        "typography": tuple(option.id for option in matrix.typography),
        "rhythms": tuple(option.id for option in matrix.rhythms),
        "imagery": tuple(option.id for option in matrix.imagery),
    }


__all__ = [
    "DiversityOption",
    "WebsiteDiversity",
    "diversity_ids",
    "render_website_diversity",
    "sample_website_diversity",
]
