"""Guarded public serving for generated website HTML.

Generated pages are stored under the instance-local website root rather than
in the source-controlled static tree. Every request resolves a concrete
:class:`~deaddit.models.GeneratedWebsite` row first; an unknown public path is
a 404 before any file is opened. The bounded cache limits stale responses,
while the CSP sandbox is the security boundary for untrusted generated HTML -
validation is not a sanitizer.
"""

from __future__ import annotations

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


@bp.route("/<hostname>/<page_name>")
def page(hostname: str, page_name: str):
    """Serve one generated page owned by a post."""
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

    response = send_file(
        path,
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
