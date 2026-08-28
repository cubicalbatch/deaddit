"""Wire generated-website storage into hard-delete lifecycle paths."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from deaddit.models import GeneratedWebsite
from deaddit.websites.storage import WebsiteStorageError, delete_website, website_root

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebsitePaths:
    """The stored file path owned by one post, captured before deletion."""

    post_id: int
    storage_path: str


def website_paths_for_posts(post_ids: Iterable[int]) -> list[WebsitePaths]:
    """Snapshot website paths owned by ``post_ids`` before deleting rows."""
    ids = list(post_ids)
    if not ids:
        return []
    rows = GeneratedWebsite.query.filter(GeneratedWebsite.post_id.in_(ids)).all()
    return [WebsitePaths(row.post_id, row.storage_path) for row in rows]


def delete_website_files(app: Any, paths: Iterable[WebsitePaths]) -> None:
    """Idempotently remove stored website files after database deletion."""
    root = website_root(app)
    for entry in paths:
        try:
            delete_website(root, entry.storage_path)
        except WebsiteStorageError:
            logger.warning(
                "website cleanup could not remove file for post %s",
                entry.post_id,
                exc_info=True,
            )


def delete_websites_for_posts(app: Any, post_ids: Iterable[int]) -> None:
    """Collect then delete website files for hard-deleted posts."""
    delete_website_files(app, website_paths_for_posts(post_ids))


__all__ = [
    "WebsitePaths",
    "delete_website_files",
    "delete_websites_for_posts",
    "website_paths_for_posts",
]
