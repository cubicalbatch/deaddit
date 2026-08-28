"""Generated single-page website storage.

Imports from this package only expose pure contracts and storage functions;
no application setup, filesystem access, or network work occurs at import
time.
"""

from .storage import (
    WEBSITE_MAX_OUTPUT_TOKENS_FLOOR,
    AllocatedWebsitePath,
    InvalidHostnameHintError,
    InvalidPageNameHintError,
    ReconcileReport,
    StoredWebsite,
    WebsiteGenerationSettings,
    WebsitePathTraversalError,
    WebsiteStorageError,
    allocate_public_path,
    delete_website,
    ensure_website_tree,
    normalize_hostname_hint,
    normalize_page_name_hint,
    reconcile_websites,
    resolve_website_path,
    resolve_website_settings,
    store_website,
    website_root,
)

__all__ = [
    "WEBSITE_MAX_OUTPUT_TOKENS_FLOOR",
    "AllocatedWebsitePath",
    "InvalidHostnameHintError",
    "InvalidPageNameHintError",
    "ReconcileReport",
    "StoredWebsite",
    "WebsiteGenerationSettings",
    "WebsitePathTraversalError",
    "WebsiteStorageError",
    "allocate_public_path",
    "delete_website",
    "ensure_website_tree",
    "normalize_hostname_hint",
    "normalize_page_name_hint",
    "reconcile_websites",
    "resolve_website_path",
    "resolve_website_settings",
    "store_website",
    "website_root",
]
