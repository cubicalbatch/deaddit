"""Guarded public serving for generated website HTML.

Generated pages are stored under the instance-local website root rather than
in the source-controlled static tree. Every request resolves a concrete
:class:`~deaddit.models.GeneratedWebsite` row first; an unknown public path is
a 404 before any file is opened. The bounded cache limits stale responses,
while the CSP sandbox is the security boundary for untrusted generated HTML -
validation is not a sanitizer.

The trusted context bar is injected at serve time (never baked at generation):
it links back to the post's discussion and names the post and its model. All
styling is inline because the generated document's own CSS is untrusted.
"""

from __future__ import annotations

import html
import io
import re

from flask import Blueprint, abort, current_app, send_file

from deaddit.models import GeneratedWebsite, Post
from deaddit.websites.storage import (
    WebsiteStorageError,
    resolve_website_path,
    website_root,
)

bp = Blueprint("websites", __name__, url_prefix="/out")

# Bounded public caching: limits stale responses while avoiding an unbounded
# cache.
CACHE_MAX_AGE_SECONDS = 300

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; base-uri 'none'; connect-src 'none'; "
    "form-action 'none'; frame-ancestors 'none'; img-src data:; "
    "font-src data:; media-src data:; object-src 'none'; worker-src 'none'; "
    "style-src 'unsafe-inline'; script-src 'unsafe-inline'; sandbox allow-scripts"
)
X_CONTENT_TYPE_OPTIONS = "nosniff"
REFERRER_POLICY = "no-referrer"
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"

#: Matches a <body ...> open tag, tolerating quoted attributes.
_BODY_OPEN_TAG_RE = re.compile(
    r"<body\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*>", re.IGNORECASE
)

#: Older rows carry the old home-link bar baked into the stored file; the
#: serve-time bar replaces it. The baked bar is a flat <div> with no nested
#: div, so the first </div> closes it.
_LEGACY_NAVIGATION_BAR_RE = re.compile(
    r'<div data-deaddit-navigation="true".*?</div>\s*', re.DOTALL
)


def _context_bar(post: Post) -> str:
    """Sticky top bar linking back to the post's discussion."""
    discussion = f"/d/{post.subdeaddit_name}/{post.id}"
    title = html.escape(post.title, quote=True)
    model = html.escape(post.llm_model or "", quote=True)
    model_chip = (
        f'<span style="flex:none;color:#d1d5db;">{model}</span>' if model else ""
    )
    return (
        '<div data-deaddit-navigation="true" '
        'style="position:sticky;top:0;z-index:2147483647;display:flex;'
        "align-items:center;gap:0.75rem;width:100%;box-sizing:border-box;"
        "margin:0;padding:0.5rem 1rem;background:#1f2937;color:#f9fafb;"
        'font:500 0.875rem/1.4 system-ui,sans-serif;">'
        f'<a href="{discussion}" style="flex:none;color:#93c5fd;'
        'text-decoration:none;font-weight:600;white-space:nowrap;">'
        "&larr; Back to post</a>"
        f'<span style="flex:1;min-width:0;overflow:hidden;text-overflow:'
        f'ellipsis;white-space:nowrap;">{title}</span>'
        f"{model_chip}"
        "</div>"
    )


def _inject_context_bar(html_text: str, post: Post) -> str:
    """Insert the trusted bar at the start of the document body."""
    match = _BODY_OPEN_TAG_RE.search(html_text)
    if match is None:
        return html_text
    html_text = _LEGACY_NAVIGATION_BAR_RE.sub("", html_text)
    return html_text[: match.end()] + _context_bar(post) + html_text[match.end() :]


@bp.route("/<hostname>/<page_name>")
def page(hostname: str, page_name: str):
    """Serve one generated page owned by a post, with the trusted bar."""
    website = (
        GeneratedWebsite.query.join(Post, Post.id == GeneratedWebsite.post_id)
        .filter(GeneratedWebsite.public_path == f"{hostname}/{page_name}")
        .first()
    )
    if website is None:
        abort(404)

    try:
        path = resolve_website_path(website_root(current_app), website.storage_path)
    except WebsiteStorageError:
        abort(404)
    if not path.is_file():
        abort(404)

    try:
        document = _inject_context_bar(path.read_text("utf-8"), website.post)
    except UnicodeDecodeError:
        abort(404)

    response = send_file(
        io.BytesIO(document.encode("utf-8")),
        mimetype="text/html",
        max_age=CACHE_MAX_AGE_SECONDS,
    )
    # Be explicit about the charset: generated documents are UTF-8 bytes and
    # this header is part of the public serving contract.
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    response.headers["X-Content-Type-Options"] = X_CONTENT_TYPE_OPTIONS
    response.headers["Referrer-Policy"] = REFERRER_POLICY
    response.headers["Permissions-Policy"] = PERMISSIONS_POLICY
    return response
