"""Dedicated no-tools HTML generation for the ``create_website`` agent tool.

This module owns exactly one job: turn an agent-supplied site brief into one
complete, storable HTML document, or fail cleanly. It builds a plain
completion request (no agent tools) against the caller's own already-resolved
LLM endpoint/model/key - never a website-specific provider or model override
- and validates the response against the spec's "Generated HTML contract"
before any caller is allowed to hand the result to
:func:`deaddit.websites.storage.store_website`.

Nothing here calls ``store_website`` or touches a database. Every failure
path - a length-truncated response, a transport timeout, a provider error, an
empty/fenced/oversized response, or a document that fails the HTML contract
checks - raises before a :class:`WebsiteGenerationResult` is ever
constructed. Since callers can only reach storage through a value this module
returns, "no file is written on failure" is a structural property of the
call graph, not something each caller has to remember to enforce.

Never logs or returns the API key: :class:`WebsiteGenerationResult` and every
exception message here are built from fixed strings plus the caller's
endpoint/model/request id/finish reason - never from ``api_key`` and never
from the raw generated HTML (which may be malformed, oversized, or otherwise
unsafe to echo back).
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from html.parser import HTMLParser

from deaddit.llm.client import ChatRequest, LLMClient, Sampling
from deaddit.llm.errors import LLMError
from deaddit.websites.diversity import (
    diversity_ids,
    render_website_diversity,
    sample_website_diversity,
)
from deaddit.websites.storage import WebsiteGenerationSettings

__all__ = [
    "WebsiteGenerationResult",
    "WebsiteGenerationError",
    "WebsiteGenerationTruncatedError",
    "WebsiteGenerationInvalidHTMLError",
    "generate_website_html",
]


@dataclass(frozen=True)
class WebsiteGenerationResult:
    """Provider-neutral result of one successful website-generation call.

    Every field is safe to persist on ``GeneratedWebsite`` and safe to log:
    ``api_url``/``model`` are the effective endpoint/model actually used
    (never the key), and token counts/``finish_reason`` come straight from
    the provider's ``usage``/``choices[0]`` shape.
    """

    html: str
    request_id: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    finish_reason: str | None
    api_url: str
    model: str
    diversity_ids: dict[str, tuple[str, ...]]


class WebsiteGenerationError(Exception):
    """A nested website-generation request did not produce a publishable page.

    The message is always a fixed, human-readable string - safe to surface
    in a tool result or log line as-is. It never contains the API key and
    never contains the raw generated HTML.
    """


class WebsiteGenerationTruncatedError(WebsiteGenerationError):
    """The provider stopped at the output-token limit (``finish_reason ==
    "length"``). Per spec invariant, this is never retried automatically at
    a larger limit - that would silently double generation cost."""


class WebsiteGenerationInvalidHTMLError(WebsiteGenerationError):
    """The response text failed the Generated HTML contract checks: empty,
    fenced, oversized, missing document markers, non-UTF-8/control bytes,
    or containing an element/attribute the contract disallows."""


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

#: Encodes the spec's "Generated HTML contract" (CREATE_WEBSITE_TOOL_PLAN.md)
#: as directly as possible - this is the only thing standing between an
#: agent's site brief and a document validated well enough to store.
_SYSTEM_PROMPT = """\
You generate exactly one complete, self-contained HTML document for a \
fictional website, based on a brief describing a site an autonomous \
persona plausibly found while browsing.

Output contract - follow every rule:
- Output raw HTML only: one complete document starting with
  "<!doctype html>" and ending with "</html>". No markdown code fence, no
  explanation, no commentary before or after the document.
- All CSS lives inline in a <style> element in <head>. All JavaScript lives
  inline in <script> elements with no "src" attribute. Do not reference any
  external stylesheet, script, font, image, video, audio, or iframe, and do
  not reference any network API, analytics, or CDN of any kind.
- Any imagery or illustration must be built from inline SVG markup, plain
  CSS (gradients, shapes, shadows), or a small bounded data: URL. Never use
  an http(s) URL as an image, media, or script source.
- Do not include a <form> element, authentication or login UI, payment or
  checkout UI, file download links, popups/new windows, background workers,
  or anything that requests a browser permission (location, camera,
  microphone, notifications, clipboard, storage).
- Do not use <iframe>, <object>, <embed>, <video>, <audio>, <link>, <base>,
  or a <meta http-equiv> tag.
- Keep the page compact: a focused site with a few well-crafted sections is
  better than an exhaustive one, and the response is cut off at a hard
  output-token limit. Do not pad with repetitive items, long placeholder
  lists, or sprawling stylesheets; once the page reads complete, stop and
  close the document.
- Write responsive, semantic, accessible markup: proper heading structure,
  labelled interactive controls that work from the keyboard, visible focus
  states, and sufficient color contrast. If you include animation or
  motion, also add a `prefers-reduced-motion` CSS rule that disables or
  reduces it.
- Any interactive behavior (tabs, toggles, filters, small games, etc.) must
  work entirely within this one self-contained HTML file using inline
  JavaScript. One file is a technical implementation constraint, not a visual
  layout constraint: the site may look like a complete multi-section or
  multi-page web presence, including navigation bars, menus, section link
  lists, breadcrumbs, multiple columns, and footers. Those controls may use
  inert in-page anchors such as `href="#section"`; do not require another
  document or network request.
- Write the page as if it is a real, independent website with its own
  voice, content, and design - not as a description of an AI prompt. Do not
  mention that the page was generated, prompted, or written by an AI, an
  agent, or a language model, unless the fictional site's own premise is
  specifically about such a thing.

Produce only the HTML document. Nothing else.
"""


def _build_user_prompt(
    website_description: str,
    hostname_hint: str,
    page_name_hint: str,
    diversity_text: str,
    max_output_tokens: int,
) -> str:
    return (
        "Generate the single HTML page described below.\n\n"
        f"Fictional site hostname (for context/branding only): {hostname_hint}\n"
        f"Fictional page name (for context only): {page_name_hint}\n\n"
        f"Output budget: your response is hard-truncated at {max_output_tokens} "
        "tokens. A truncated document is discarded in full, so size the site to "
        "finish comfortably within the budget and write the closing </html>.\n\n"
        "Art direction matrix for this generation (use it to steer the visual and "
        "structural result; do not mention the matrix in the page):\n"
        f"{diversity_text}\n\n"
        "Site and page brief, written by the persona who found this site:\n"
        f"{website_description}\n\n"
        "Write the complete HTML document for this exact page now."
    )


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------

_MARKDOWN_FENCE = "```"
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Elements that are either inherently active/embedding, or that the
#: contract requires to never appear because their whole purpose is an
#: external/network dependency this module cannot make safe.
_BANNED_TAGS = frozenset(
    {
        "iframe",
        "object",
        "embed",
        "form",
        "video",
        "audio",
        "source",
        "track",
        "link",
        "base",
        "applet",
        "frame",
        "frameset",
        "portal",
    }
)

#: Attributes checked for a disallowed (non-inline) resource reference.
#: ``href`` stays in this list because it still applies to non-anchor
#: elements (e.g. a stray ``<area href>``); the anchor exception below is
#: narrower than dropping ``href`` from this list entirely. ``srcset`` holds
#: a comma-separated list of candidate URLs (e.g. ``"a.jpg 1x, b.jpg 2x"``),
#: not a single URI, so it is checked separately in ``_check`` rather than
#: through the single-URI loop below (carried defect C1,
#: WEBSITE_TOOL_EXECUTION.md "Carried defects").
_RESOURCE_ATTRS = ("src", "href", "poster", "data", "action", "formaction")
_SRCSET_ATTR = "srcset"
_SRCSET_SPLIT_RE = re.compile(r",\s+")


def _local_attr_name(name: str) -> str:
    """Strip an XML namespace prefix (e.g. ``"xlink:href"`` -> ``"href"``).

    Carried defect C2 (WEBSITE_TOOL_EXECUTION.md "Carried defects"):
    ``html.parser`` reports attribute names verbatim, so a namespaced SVG
    attribute like ``xlink:href`` never matched the plain ``"href"`` check.
    Matching on the local part closes that gap without touching the ``<a
    href>`` exception, which still only special-cases ``tag == "a"``.
    """
    return name.rsplit(":", 1)[-1]


#: URI prefixes that stay on the page or are otherwise not a network
#: dependency. Everything else (http(s)://, //, ftp://, a bare relative
#: path, ...) is rejected wherever a resource attribute is checked.
_ALLOWED_URI_PREFIXES = ("data:", "#", "javascript:", "mailto:", "tel:")


class _ContractViolation(WebsiteGenerationInvalidHTMLError):
    """Internal signal used to unwind HTMLParser.feed() on the first hit."""


class _WebsiteHTMLValidator(HTMLParser):
    """Rejects elements/attributes that contradict the generation contract.

    Deliberately shallow: this is a reliability check ("did the model
    obviously break the contract"), not a sanitizer. CSP plus the ``/out/``
    sandbox (Phase 4, not this module) is the real security boundary.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._check(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._check(tag, attrs)

    def _check(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        # Match attribute names by their local part (strip any "ns:"
        # prefix) so a namespaced variant such as SVG's "xlink:href" is
        # still caught by the plain "href" check (carried defect C2).
        #
        # Deliberately kept as a list of (name, value) pairs, not a dict
        # keyed by local name: a document can legally repeat an attribute
        # under two spellings that collapse to the same local name (e.g.
        # both "xlink:href" and "href" on one <image>), and a naive
        # last-write-wins dict would let a trailing safe value silently
        # mask an earlier malicious one. Checking every occurrence
        # individually means a violation on *any* spelling still raises,
        # regardless of attribute order.
        normalized = [
            (_local_attr_name(name.lower()), (value or "")) for name, value in attrs
        ]
        attr_names = {name for name, _ in normalized}

        if tag in _BANNED_TAGS:
            raise _ContractViolation(f"generated HTML uses a disallowed <{tag}> tag")

        if tag == "meta" and "http-equiv" in attr_names:
            raise _ContractViolation(
                "generated HTML uses a disallowed <meta http-equiv> tag"
            )

        if tag == "script" and "src" in attr_names:
            raise _ContractViolation(
                "generated HTML references an external <script src>"
            )

        for attr_name, raw_value in normalized:
            if attr_name in _RESOURCE_ATTRS:
                if tag == "a" and attr_name == "href":
                    # Resolved spec interpretation (WEBSITE_TOOL_EXECUTION.md):
                    # an <a href> loads nothing - it only navigates - and the
                    # Phase 4 CSP sandbox grants neither allow-top-navigation
                    # nor allow-popups, so following one is already inert.
                    # Rejecting a whole 32K-token document over a plain nav
                    # link buys no security. This exception is deliberately
                    # narrow: every other resource attribute (including href
                    # on non-anchor elements such as <area>) is still checked
                    # below.
                    continue
                value = raw_value.strip().lower()
                if not value or value.startswith(_ALLOWED_URI_PREFIXES):
                    continue
                raise _ContractViolation(
                    f"generated HTML has a <{tag} {attr_name}> referencing an "
                    "external resource"
                )

            if attr_name == _SRCSET_ATTR:
                # srcset (carried defect C1) is a comma-separated candidate
                # list, e.g. "a.jpg 1x, b.jpg 2x" - each candidate is "<url>
                # [descriptor]". Split on ", " (comma followed by
                # whitespace), not a bare comma: a data: URL's base64
                # payload routinely contains commas with no following
                # whitespace (e.g. "base64,AAAA"), and naively splitting on
                # every comma would shear a legitimate data: candidate in
                # two and misread its trailing fragment as a second,
                # URL-less candidate.
                for candidate in _SRCSET_SPLIT_RE.split(raw_value):
                    candidate = candidate.strip()
                    if not candidate:
                        continue
                    url = candidate.split()[0]
                    value = url.strip().lower()
                    if value.startswith(_ALLOWED_URI_PREFIXES):
                        continue
                    raise _ContractViolation(
                        f"generated HTML has a <{tag} srcset> referencing an "
                        "external resource"
                    )


_BODY_OPEN_TAG_RE = re.compile(
    r"<body\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*>", re.IGNORECASE
)
_DEADDIT_NAVIGATION_BAR = (
    '<div data-deaddit-navigation="true" style="display:block;'
    "width:100%;box-sizing:border-box;margin:0 0 1rem;padding:0.5rem 1rem;"
    "background:#1f2937;color:#f9fafb;font:500 0.875rem/1.4 "
    'system-ui,sans-serif;">'
    '<a href="/" style="color:#f9fafb;text-decoration:underline;">'
    "← Back to deaddit</a></div>"
)


def _inject_navigation_bar(html: str) -> str:
    """Insert the trusted Deaddit link at the start of the document body."""
    match = _BODY_OPEN_TAG_RE.search(html)
    if match is None:
        raise WebsiteGenerationInvalidHTMLError(
            "website generation did not return a document with a body element"
        )
    return html[: match.end()] + _DEADDIT_NAVIGATION_BAR + html[match.end() :]


def _validate_html(content: str, settings: WebsiteGenerationSettings) -> str:
    """Validate *content* against the Generated HTML contract or raise.

    Returns the validated (whitespace-trimmed) HTML on success. Every
    rejection raises :class:`WebsiteGenerationInvalidHTMLError` with a fixed,
    safe message - never the offending content itself.
    """

    if not isinstance(content, str):
        raise WebsiteGenerationInvalidHTMLError(
            "website generation returned a non-text response"
        )

    stripped = content.strip()
    if not stripped:
        raise WebsiteGenerationInvalidHTMLError(
            "website generation returned an empty response"
        )

    if _MARKDOWN_FENCE in stripped:
        raise WebsiteGenerationInvalidHTMLError(
            "website generation returned a markdown-fenced response instead "
            "of a raw HTML document"
        )

    try:
        encoded = stripped.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise WebsiteGenerationInvalidHTMLError(
            "website generation returned text that is not valid UTF-8"
        ) from exc

    if _CONTROL_CHAR_RE.search(stripped):
        raise WebsiteGenerationInvalidHTMLError(
            "website generation returned text containing control characters"
        )

    if len(encoded) > settings.max_html_bytes:
        raise WebsiteGenerationInvalidHTMLError(
            "website generation returned a document larger than the "
            f"configured {settings.max_html_bytes}-byte limit"
        )

    lowered = stripped.lower()
    if not lowered.startswith("<!doctype html"):
        raise WebsiteGenerationInvalidHTMLError(
            "website generation did not return a document starting with <!doctype html>"
        )
    if not lowered.endswith("</html>"):
        raise WebsiteGenerationInvalidHTMLError(
            "website generation did not return a complete document ending "
            "with </html> - it may have been cut off"
        )

    parser = _WebsiteHTMLValidator()
    try:
        parser.feed(stripped)
        parser.close()
    except _ContractViolation:
        raise
    except Exception as exc:
        raise WebsiteGenerationInvalidHTMLError(
            "website generation returned HTML that could not be parsed"
        ) from exc

    return stripped


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def generate_website_html(
    *,
    website_description: str,
    hostname_hint: str,
    page_name_hint: str,
    api_url: str,
    api_key: str | None,
    model: str,
    agent: str | None,
    settings: WebsiteGenerationSettings,
    run_deadline_remaining: float | None = None,
    rng: random.Random | None = None,
) -> WebsiteGenerationResult:
    """Generate and validate one HTML document for ``create_website``.

    Builds a plain (no-tools) completion request against exactly *api_url*/
    *api_key*/*model* - the caller's already-resolved effective LLM
    configuration, never a website-specific override - labeled
    ``action="create_website"`` and ``agent=agent`` so the request is billed
    and audited through the normal :mod:`deaddit.llm.accounting` ledger like
    any other LLM call. ``max_tokens`` comes from *settings*
    (``resolve_website_settings()``'s 32,768-token floor already applied).

    ``read_timeout`` is the smaller of ``settings.generation_timeout_seconds``
    and *run_deadline_remaining* (seconds left in the caller's agent-run
    budget; pass ``None`` outside a real run, e.g. ``ToolContext.deadline`` is
    ``None``, and this falls back to the website timeout alone).

    Raises :class:`WebsiteGenerationTruncatedError` on a ``length`` finish
    reason, :class:`WebsiteGenerationInvalidHTMLError` on any Generated HTML
    contract violation (empty/fenced/oversized/malformed output), or the
    generic :class:`WebsiteGenerationError` for a transport timeout or
    provider error. Only returns a :class:`WebsiteGenerationResult` - the
    caller's one and only path to :func:`deaddit.websites.storage.store_website`
    - once every check has passed; there is no partial/intermediate value a
    caller could accidentally store first.
    """

    read_timeout = settings.generation_timeout_seconds
    if run_deadline_remaining is not None:
        read_timeout = min(read_timeout, run_deadline_remaining)
    if read_timeout <= 0:
        raise WebsiteGenerationError(
            "not enough run time remaining to generate a website"
        )
    diversity_matrix = sample_website_diversity(
        rng if rng is not None else random.Random()
    )
    diversity_text = render_website_diversity(diversity_matrix)
    selected_diversity_ids = diversity_ids(diversity_matrix)

    request = ChatRequest(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=_build_user_prompt(
            website_description,
            hostname_hint,
            page_name_hint,
            diversity_text,
            settings.max_output_tokens,
        ),
        model=model,
        api_url=api_url,
        api_key=api_key,
        sampling=Sampling(max_tokens=settings.max_output_tokens),
        read_timeout=read_timeout,
        action="create_website",
        agent=agent,
    )

    try:
        result = LLMClient().complete(request)
    except LLMError as exc:
        raise WebsiteGenerationError(
            f"website generation request failed: {type(exc).__name__}"
        ) from exc

    if result.finish_reason == "length":
        raise WebsiteGenerationTruncatedError(
            "website generation stopped at the output-token limit before "
            "completing the document"
        )

    html = _validate_html(result.content, settings)
    html = _inject_navigation_bar(html)

    usage = result.usage or {}
    return WebsiteGenerationResult(
        html=html,
        request_id=result.request_id,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
        finish_reason=result.finish_reason,
        api_url=api_url,
        model=model,
        diversity_ids=selected_diversity_ids,
    )
