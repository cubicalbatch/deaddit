"""Guarded public media serving for generated post images (plan 6A).

Generated image files live under the instance-local media root
(``deaddit/images/storage.py``), not the source-controlled static tree, so
they survive container replacement and moderation can suppress them. Every
request here resolves a concrete :class:`~deaddit.models.PostImage` row
first; an opaque filename with no matching, non-removed row is a 404 - the
file is never opened based on the request path alone.

Cache headers are bounded (not the permanent/immutable caching normally
appropriate for content-addressed filenames) because a soft-removed post
must stop being served within a bounded window: moderation needs to take
effect, not wait out a "forever" client or proxy cache.
"""

from __future__ import annotations

from flask import Blueprint, abort, current_app, send_file

from deaddit.images.storage import MediaStorageError, media_root, resolve_media_path
from deaddit.models import Post, PostImage

bp = Blueprint("media", __name__, url_prefix="/media/images")

# Bounded public caching: short enough that a moderation removal becomes
# effective for caches/CDNs promptly, long enough to avoid re-fetching a
# thumbnail on every feed scroll within one browsing session.
CACHE_MAX_AGE_SECONDS = 300

_VARIANTS = {
    "original": (PostImage.original_path, "original_path"),
    "thumbnail": (PostImage.thumbnail_path, "thumbnail_path"),
}


def _serve_variant(kind: str, filename: str):
    column, path_attr = _VARIANTS[kind]
    relpath = f"{'originals' if kind == 'original' else 'thumbnails'}/{filename}"

    image = (
        PostImage.query.join(Post, Post.id == PostImage.post_id)
        .filter(column == relpath, Post.removed.is_(False))
        .first()
    )
    if image is None:
        abort(404)

    root = media_root(current_app)
    try:
        # Resolve from the row's own stored path, not the raw request
        # filename - resolve_media_path is the single sanctioned join point
        # and rejects traversal/escape even though the DB lookup above
        # already pins this to a real, non-removed post's own file.
        path = resolve_media_path(root, getattr(image, path_attr))
    except MediaStorageError:
        abort(404)
    if not path.is_file():
        abort(404)

    return send_file(
        path,
        mimetype=image.mime_type,
        max_age=CACHE_MAX_AGE_SECONDS,
    )


@bp.route("/original/<filename>")
def original(filename):
    return _serve_variant("original", filename)


@bp.route("/thumbnail/<filename>")
def thumbnail(filename):
    return _serve_variant("thumbnail", filename)
