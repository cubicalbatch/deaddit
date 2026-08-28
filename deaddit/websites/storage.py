"""Secure local storage for generated single-page websites.

Agent-supplied hostname and page-name hints are normalized into a safe,
DNS-like ASCII hostname and a cosmetic page filename; neither is ever used
to name a file on disk. Stored HTML is written to a scratch ``tmp/``
directory, flushed, and atomically replaced into ``pages/`` under an opaque
UUID name that is never derived from request input. Every stored path is
re-resolved through a single traversal-proof function - which never follows
a symlink out of the configured root - before any filesystem operation
touches it, mirroring :mod:`deaddit.images.storage`'s ``resolve_media_path``
shape.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import secrets
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any


class WebsiteStorageError(Exception):
    """Base class for local generated-website storage failures."""


class InvalidHostnameHintError(WebsiteStorageError):
    """A hostname hint cannot be normalized into a safe fictional hostname."""


class InvalidPageNameHintError(WebsiteStorageError):
    """A page-name hint cannot be normalized into a safe page filename."""


class WebsitePathTraversalError(WebsiteStorageError):
    """A website storage path escapes, or attempts to escape, the website root."""


@dataclass(frozen=True)
class AllocatedWebsitePath:
    """A normalized, collision-free public path for a generated website."""

    hostname: str
    page_name: str
    public_path: str


@dataclass(frozen=True)
class StoredWebsite:
    """Metadata for an atomically stored generated-website HTML file."""

    storage_path: str
    byte_size: int
    sha256: str


@dataclass
class ReconcileReport:
    """Results of comparing stored website rows against ``pages/`` on disk."""

    orphaned_files: list[str]
    missing_rows: list[dict[str, Any]]
    mismatched_rows: list[dict[str, Any]]


@dataclass(frozen=True)
class WebsiteGenerationSettings:
    """Parsed, floor-enforced settings for nested website generation."""

    max_output_tokens: int
    generation_timeout_seconds: float
    max_html_bytes: int


# ---------------------------------------------------------------------------
# Root and tree management
# ---------------------------------------------------------------------------


def website_root(app: Any) -> Path:
    """Return the configured generated-website root as a :class:`Path`."""

    return Path(app.config["GENERATED_WEBSITES_ROOT"])


def ensure_website_tree(root: Path) -> None:
    """Create the website directories used for atomic storage."""

    root = Path(root)
    for directory in (root / "pages", root / "tmp"):
        directory.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

#: Spec invariant: the default output allowance is 32,768 tokens, and any
#: configured value below that floor is raised rather than honored - a real
#: all-in-one HTML/CSS/JS document, plus a reasoning model's hidden
#: "thinking" budget, routinely needs the full allowance.
WEBSITE_MAX_OUTPUT_TOKENS_FLOOR = 32768

_WEBSITE_GENERATION_TIMEOUT_DEFAULT = 300.0

# 32,768 tokens of English prose average roughly 4 bytes/token (~131 KB),
# but generated HTML/CSS/JS is token-dense in a different way: inline SVG
# path data, hex colors, and repeated attribute quoting push the
# bytes-per-token ratio well above prose without changing the token count
# the provider bills for. 1 MiB gives roughly 8x headroom over the naive
# prose estimate while still bounding one stored page - and the in-memory
# buffer used to hash it - to a small, fixed size rather than letting the
# pages/ directory become open-ended blob storage.
_WEBSITE_MAX_HTML_BYTES_DEFAULT = 1_048_576


def _parse_positive_int(raw: Any, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _parse_positive_float(raw: Any, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def resolve_website_settings(
    get: Callable[[str, str], Any],
) -> WebsiteGenerationSettings:
    """Parse the three website-generation settings from a ``Config``-like getter.

    ``get`` is called as ``get(key, default_str)`` - pass
    :meth:`deaddit.config.Config.get` in application code; tests can pass any
    callable with that shape (for example a plain ``dict``'s ``.get``).
    Centralizing parsing here, instead of at each call site, is what
    enforces the spec's 32,768-token floor in exactly one place: a
    configured ``WEBSITE_MAX_OUTPUT_TOKENS`` below the floor is raised to
    it, never honored.
    """

    max_output_tokens = _parse_positive_int(
        get("WEBSITE_MAX_OUTPUT_TOKENS", str(WEBSITE_MAX_OUTPUT_TOKENS_FLOOR)),
        WEBSITE_MAX_OUTPUT_TOKENS_FLOOR,
    )
    max_output_tokens = max(max_output_tokens, WEBSITE_MAX_OUTPUT_TOKENS_FLOOR)

    generation_timeout_seconds = _parse_positive_float(
        get(
            "WEBSITE_GENERATION_TIMEOUT_SECONDS",
            str(_WEBSITE_GENERATION_TIMEOUT_DEFAULT),
        ),
        _WEBSITE_GENERATION_TIMEOUT_DEFAULT,
    )

    max_html_bytes = _parse_positive_int(
        get("WEBSITE_MAX_HTML_BYTES", str(_WEBSITE_MAX_HTML_BYTES_DEFAULT)),
        _WEBSITE_MAX_HTML_BYTES_DEFAULT,
    )

    return WebsiteGenerationSettings(
        max_output_tokens=max_output_tokens,
        generation_timeout_seconds=generation_timeout_seconds,
        max_html_bytes=max_html_bytes,
    )


# ---------------------------------------------------------------------------
# URL hint normalization
# ---------------------------------------------------------------------------

_HOSTNAME_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_MAX_LABEL_LENGTH = 63
_MAX_HOSTNAME_LENGTH = 253
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://")
_WORD_RE = re.compile(r"[a-z0-9]+")


def normalize_hostname_hint(hint: str) -> str:
    """Normalize an agent-supplied hostname hint into a safe fictional host.

    Lowercases, strips a leading scheme and any trailing path/query/
    fragment or ``:port``, then requires what remains to be nothing but
    DNS-like ASCII labels: letters, digits, and interior hyphens only.
    Raises :class:`InvalidHostnameHintError` for embedded credentials, IPv4
    or IPv6 literals, empty/overlong labels, an overlong hostname, or any
    control, non-ASCII, or separator character. Unlike the page-name hint,
    this never silently drops unsafe input - a rejected hint means the
    agent supplied something that cannot become a fictional hostname.
    """

    if not isinstance(hint, str) or not hint.strip():
        raise InvalidHostnameHintError("hostname hint must be non-empty text")

    # Reject anything outside printable ASCII up front: no control bytes,
    # no Unicode homograph tricks, before any further parsing.
    if not hint.isascii() or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in hint):
        raise InvalidHostnameHintError("hostname hint must be printable ASCII")

    candidate = hint.strip().lower()

    scheme_match = _SCHEME_RE.match(candidate)
    if scheme_match:
        candidate = candidate[scheme_match.end() :]
    elif candidate.startswith("//"):
        candidate = candidate[2:]

    # Embedded credentials are unsafe input, not a normalization target:
    # reject rather than silently discarding "user:pass@".
    if "@" in candidate:
        raise InvalidHostnameHintError("hostname hint must not include credentials")

    if "\\" in candidate:
        raise InvalidHostnameHintError("hostname hint must not include backslashes")

    for separator in ("/", "?", "#"):
        index = candidate.find(separator)
        if index != -1:
            candidate = candidate[:index]

    if not candidate:
        raise InvalidHostnameHintError("hostname hint has no host portion")

    # Bracketed IPv6 literals are never valid on either side of the
    # brackets, so reject outright rather than trying to parse them.
    if "[" in candidate or "]" in candidate:
        raise InvalidHostnameHintError("hostname hint must not be an IP literal")

    if ":" in candidate:
        head, _, tail = candidate.rpartition(":")
        if tail.isdigit() and head:
            candidate = head
        else:
            raise InvalidHostnameHintError("hostname hint must not include a port")

    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        raise InvalidHostnameHintError("hostname hint must not be an IP literal")

    labels = candidate.split(".")
    if any(not label for label in labels):
        raise InvalidHostnameHintError("hostname hint has an empty label")
    for label in labels:
        if len(label) > _MAX_LABEL_LENGTH:
            raise InvalidHostnameHintError("hostname hint has an overlong label")
        if not _HOSTNAME_LABEL_RE.match(label):
            raise InvalidHostnameHintError("hostname hint has an invalid label")

    if len(candidate) > _MAX_HOSTNAME_LENGTH:
        raise InvalidHostnameHintError("hostname hint is too long")

    return candidate


def normalize_page_name_hint(hint: str) -> str:
    """Slugify an agent-supplied page-name hint into a safe ``.html`` filename.

    Lowercases, strips one optional trailing ``.html``, then extracts
    lowercase ASCII word runs and joins them with ``-``. Every other
    character - path separators, dots, Unicode, control bytes - is simply
    discarded rather than rejected: the result is always constrained to
    ``[a-z0-9-]+.html`` regardless of input, and (unlike the hostname hint)
    is never used to resolve a filesystem path, so neutralizing unsafe
    characters is sufficient. Raises :class:`InvalidPageNameHintError` only
    when nothing usable remains, for example a hint that is pure
    punctuation or path separators.
    """

    if not isinstance(hint, str):
        raise InvalidPageNameHintError("page name hint must be text")

    candidate = hint.strip().lower()
    if candidate.endswith(".html"):
        candidate = candidate[: -len(".html")]

    slug = "-".join(_WORD_RE.findall(candidate))
    if not slug:
        raise InvalidPageNameHintError("page name hint has no usable characters")

    return f"{slug}.html"


# ---------------------------------------------------------------------------
# Path allocation
# ---------------------------------------------------------------------------


def allocate_public_path(
    hostname: str,
    page_name: str,
    *,
    is_public_path_taken: Callable[[str], bool],
    max_attempts: int = 20,
) -> AllocatedWebsitePath:
    """Allocate a collision-free ``public_path`` for a normalized host/page.

    ``is_public_path_taken`` is the uniqueness-check seam: this module has
    no knowledge of the (not-yet-existing) ``GeneratedWebsite`` table, so
    callers pass a lookup - a real query against
    ``GeneratedWebsite.public_path`` once phase 2.1 adds that model, or a
    plain set/dict-backed callable in tests. Only path allocation is
    retried on collision, matching the spec: never the already-spent LLM
    generation that produced the HTML.
    """

    base_path = f"{hostname}/{page_name}"
    if not is_public_path_taken(base_path):
        return AllocatedWebsitePath(hostname, page_name, base_path)

    stem = page_name[: -len(".html")] if page_name.endswith(".html") else page_name
    for _ in range(max_attempts):
        candidate_page_name = f"{stem}-{secrets.token_hex(3)}.html"
        candidate_path = f"{hostname}/{candidate_page_name}"
        if not is_public_path_taken(candidate_path):
            return AllocatedWebsitePath(hostname, candidate_page_name, candidate_path)

    raise WebsiteStorageError(
        "could not allocate a unique public path after repeated collisions"
    )


# ---------------------------------------------------------------------------
# Atomic write, resolution, deletion
# ---------------------------------------------------------------------------


def store_website(html: str | bytes, root: Path) -> StoredWebsite:
    """Atomically store one generated HTML document under an opaque name.

    Writes to ``tmp/``, flushes and fsyncs, then ``os.replace()``s into
    ``pages/`` under a fresh ``uuid4`` filename that is never derived from
    any request input. Returns metadata only once the final file exists at
    its permanent path.
    """

    root = Path(root)
    ensure_website_tree(root)
    data = html.encode("utf-8") if isinstance(html, str) else bytes(html)

    storage_path = Path("pages") / f"{uuid.uuid4().hex}.html"
    final_path = root / storage_path
    temporary_path = root / "tmp" / f"{storage_path.name}.tmp"
    try:
        with open(temporary_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, final_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    return StoredWebsite(
        storage_path=str(storage_path),
        byte_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def resolve_website_path(root: Path, relpath: str) -> Path:
    """Resolve a root-relative website storage path, rejecting escapes.

    Mirrors :func:`deaddit.images.storage.resolve_media_path`: refuses an
    empty, absolute, NUL-containing, or ``..``-containing path, refuses a
    Windows drive/root component, and only returns a path once resolving
    symlinks and checking containment has proven it sits inside the
    configured root - so a symlink under the root that points outside it is
    never followed.
    """

    root = Path(root)
    try:
        raw = os.fspath(relpath)
    except TypeError as exc:
        raise WebsitePathTraversalError("website path must be path-like") from exc
    if not isinstance(raw, str):
        raise WebsitePathTraversalError("website path must be text")

    windows_path = PureWindowsPath(raw)
    if (
        not raw
        or "\x00" in raw
        or Path(raw).is_absolute()
        or ".." in raw
        or windows_path.drive
        or windows_path.root
    ):
        raise WebsitePathTraversalError(
            "website path is empty, absolute, or traversing"
        )

    candidate = (root / raw).resolve()
    root_resolved = root.resolve()
    if candidate == root_resolved:
        raise WebsitePathTraversalError("website path must identify a file")
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise WebsitePathTraversalError("website path escapes website root") from exc
    return candidate


def delete_website(root: Path, storage_path: str) -> None:
    """Idempotently delete one stored website file."""

    resolve_website_path(root, storage_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def _website_file_path(root: Path, relpath: str) -> Path | None:
    try:
        candidate = resolve_website_path(root, relpath)
    except WebsitePathTraversalError:
        return None
    return candidate if candidate.is_file() else None


def reconcile_websites(
    root: Path,
    rows: Iterable[Any],
    *,
    apply: bool = False,
) -> ReconcileReport:
    """Compare stored-website rows against ``pages/`` on disk.

    ``rows`` are duck-typed: each must expose ``post_id``, ``storage_path``,
    ``byte_size``, and ``sha256`` attributes - the shape phase 2.1's
    ``GeneratedWebsite`` model will have. This primitive is written against
    that shape without importing the model, which does not exist yet;
    plain objects (for example :class:`types.SimpleNamespace`) work fine in
    tests.

    A row whose ``storage_path`` cannot be safely resolved (traversal
    input) or whose file is absent is reported in ``missing_rows`` - from
    reconciliation's point of view a path this module refuses to look
    outside the root for is indistinguishable from a missing file. A row
    whose file exists but whose size or sha256 no longer matches what was
    recorded is reported in ``mismatched_rows``.

    Dry-run by default (``apply=False``): nothing on disk changes, and
    ``orphaned_files`` lists the ``pages/`` files no row references - what
    an apply run would remove. Pass ``apply=True`` to actually delete them.
    Never follows a symlink out of the root (see
    :func:`resolve_website_path`).
    """

    root = Path(root).resolve()
    referenced: set[str] = set()
    missing_rows: list[dict[str, Any]] = []
    mismatched_rows: list[dict[str, Any]] = []

    for row in rows:
        storage_path = row.storage_path
        referenced.add(storage_path)
        file_path = _website_file_path(root, storage_path)
        if file_path is None:
            missing_rows.append({"post_id": row.post_id, "storage_path": storage_path})
            continue

        data = file_path.read_bytes()
        actual_size = len(data)
        actual_sha256 = hashlib.sha256(data).hexdigest()
        if actual_size != row.byte_size or actual_sha256 != row.sha256:
            mismatched_rows.append(
                {
                    "post_id": row.post_id,
                    "storage_path": storage_path,
                    "expected_size": row.byte_size,
                    "actual_size": actual_size,
                    "expected_sha256": row.sha256,
                    "actual_sha256": actual_sha256,
                }
            )

    orphaned_files: list[str] = []
    pages_dir = root / "pages"
    if pages_dir.is_dir():
        for path in pages_dir.rglob("*"):
            if not (path.is_file() or path.is_symlink()):
                continue
            relative = path.relative_to(root).as_posix()
            if relative in referenced:
                continue
            orphaned_files.append(relative)
    orphaned_files.sort()

    if apply:
        for relative in orphaned_files:
            resolve_website_path(root, relative).unlink(missing_ok=True)

    return ReconcileReport(
        orphaned_files=orphaned_files,
        missing_rows=missing_rows,
        mismatched_rows=mismatched_rows,
    )
