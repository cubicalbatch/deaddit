"""Image generation and secure local-media helpers.

Imports from this package only expose pure contracts and storage functions; no
application setup, filesystem access, or network work occurs at import time.
"""

from .storage import (
    DownloadedImage,
    ImageTooLargeError,
    MalformedImageError,
    MediaPathTraversalError,
    MediaStorageError,
    ReconcileReport,
    StoredImage,
    UnsafeImageURLError,
    UnsupportedImageMIMEError,
    delete_variants,
    download_image,
    ensure_media_tree,
    media_root,
    reconcile_media,
    resolve_media_path,
    store_variants,
)

__all__ = [
    "DownloadedImage",
    "ImageTooLargeError",
    "MalformedImageError",
    "MediaPathTraversalError",
    "MediaStorageError",
    "ReconcileReport",
    "StoredImage",
    "UnsafeImageURLError",
    "UnsupportedImageMIMEError",
    "delete_variants",
    "download_image",
    "ensure_media_tree",
    "media_root",
    "reconcile_media",
    "resolve_media_path",
    "store_variants",
]
