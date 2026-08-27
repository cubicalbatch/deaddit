"""Wires the storage primitives (plan 1B) into every hard-delete path (7A).

:mod:`deaddit.images.storage` owns the mechanics of deleting and
reconciling files on disk; it knows nothing about ``Post``, ``User``, or
``Subdeaddit``. This module is the seam that connects the two: callers in
:mod:`deaddit.admin` collect the media a doomed set of posts owns *before*
issuing any DELETE (a bulk ``Query.delete()`` bypasses ORM cascades and
would otherwise leave the files as the only remaining trace of the
association), then hand those paths here once the database transaction
has committed.

Soft removal never calls anything in this module: :class:`~deaddit.models.
Post`'s ``removed`` flag keeps the row and its files in place for audit
purposes (:mod:`deaddit.media` already refuses to serve them). Only a real
row deletion - single, bulk, or cascaded through a user/subdeaddit - is a
hard delete and reaches this module.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deaddit.images.storage import (
    MediaStorageError,
    ReconcileReport,
    delete_variants,
    media_root,
    resolve_media_path,
)
from deaddit.models import PostImage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaPaths:
    """The stored file paths owned by one post, captured before deletion."""

    post_id: int
    original_path: str
    thumbnail_path: str


def media_paths_for_posts(post_ids: Iterable[int]) -> list[MediaPaths]:
    """Snapshot the image paths owned by ``post_ids`` right now.

    Call this before any DELETE touches ``post`` or ``post_image`` for
    these ids - a bulk ``Post.query.filter(...).delete()`` does not cascade
    to ``PostImage`` at the database level (there is no ``ON DELETE
    CASCADE`` on ``post_image.post_id``), so this snapshot is the only
    record of which files belonged to which post once the rows are gone.
    """
    ids = list(post_ids)
    if not ids:
        return []
    rows = PostImage.query.filter(PostImage.post_id.in_(ids)).all()
    return [
        MediaPaths(row.post_id, row.original_path, row.thumbnail_path) for row in rows
    ]


def delete_media_files(app: Any, paths: Iterable[MediaPaths]) -> None:
    """Idempotently remove the on-disk variants for already-deleted rows.

    Only ever call this after the owning database rows are committed gone
    (soft removal keeps files, so a hard delete is the only legitimate
    caller). Missing files are not an error - :func:`~deaddit.images.
    storage.delete_variants` tolerates repeated or partial deletion. A
    single post's storage error is logged and does not stop cleanup of the
    rest, so one bad row can never leave every other file behind.
    """
    root = media_root(app)
    for entry in paths:
        try:
            delete_variants(root, entry.original_path, entry.thumbnail_path)
        except MediaStorageError:
            logger.warning(
                "media cleanup could not remove files for post %s",
                entry.post_id,
                exc_info=True,
            )


def delete_media_for_posts(app: Any, post_ids: Iterable[int]) -> None:
    """Collect then delete: the common case for a hard-delete route.

    Must be called with ``post_ids`` snapshotted (see
    :func:`media_paths_for_posts`) *before* the corresponding rows are
    removed from the database - by the time this runs the rows may already
    be gone, so it cannot look the paths up itself.
    """
    delete_media_files(app, media_paths_for_posts(post_ids))


def _file_exists(root: Path, relpath: str) -> bool:
    try:
        return resolve_media_path(root, relpath).is_file()
    except MediaStorageError:
        return False


def preview_orphaned_media(
    root: Path, image_rows: Iterable[PostImage]
) -> ReconcileReport:
    """Report what :func:`~deaddit.images.storage.reconcile_media` would do
    without deleting anything.

    Used by the operator CLI's dry-run (default) mode. Mirrors
    ``reconcile_media``'s scan exactly - same referenced set, same
    incomplete-row detection - but never unlinks a file, so it is safe to
    run against a production database at any time.
    """
    root = Path(root).resolve()
    rows = list(image_rows)
    referenced: set[str] = set()
    incomplete_rows: list[dict[str, Any]] = []
    for row in rows:
        referenced.update((row.original_path, row.thumbnail_path))
        missing = [
            path
            for path in (row.original_path, row.thumbnail_path)
            if not _file_exists(root, path)
        ]
        if missing:
            incomplete_rows.append({"post_id": row.post_id, "missing": missing})

    orphaned_files: list[str] = []
    for directory_name in ("originals", "thumbnails"):
        try:
            directory = resolve_media_path(root, directory_name)
        except MediaStorageError:
            continue
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if not (path.is_file() or path.is_symlink()):
                continue
            relative = path.relative_to(root).as_posix()
            if relative not in referenced:
                orphaned_files.append(relative)

    orphaned_files.sort()
    return ReconcileReport(
        removed_files=orphaned_files, incomplete_rows=incomplete_rows
    )


__all__ = [
    "MediaPaths",
    "media_paths_for_posts",
    "delete_media_files",
    "delete_media_for_posts",
    "preview_orphaned_media",
]
