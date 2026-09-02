"""
Utility functions for the Deaddit application.
"""

import hashlib
import hmac
import html
import re
from functools import wraps

from flask import abort, current_app, request
from sqlalchemy import func

from deaddit.config import Config
from deaddit.extensions import cache, db

from .models import Comment, GeneratedWebsite, Vote

# Name of the long-lived cookie that anonymously identifies a voting browser.
VOTER_COOKIE = "deaddit_voter"
VOTER_COOKIE_MAX_AGE = 365 * 24 * 3600


def production_disabled(f):
    """Decorator that returns 404 for endpoints that should be disabled in production.

    This decorator checks the PRODUCTION deploy flag (environment-only, see
    Config.DEPLOY_KEYS) and returns a 404 error if the application is running in
    production mode. This is used to disable every admin
    routes in production deployments.

    Usage:
        @production_disabled
        def admin_endpoint():
            # This endpoint will return 404 when PRODUCTION=true
            pass
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if Config.get("PRODUCTION", "false").lower() == "true":
            abort(404)
        return f(*args, **kwargs)

    return decorated_function


def get_comment_counts_bulk(post_ids: list[int]) -> dict[int, int]:
    """
    Efficiently get comment counts for multiple posts using a single query with caching.

    Args:
        post_ids: List of post IDs to get comment counts for

    Returns:
        Dictionary mapping post_id to comment count
    """
    if not post_ids:
        return {}

    try:
        # Try to get cached counts first
        cache_key = f"comment_counts_{sorted(post_ids)}"
        cached_result = cache.get(cache_key)
        if cached_result:
            return cached_result

        comment_count_results = (
            db.session.query(Comment.post_id, func.count(Comment.id).label("count"))
            .filter(Comment.post_id.in_(post_ids))
            .group_by(Comment.post_id)
            .all()
        )

        comment_counts = {
            result.post_id: result.count for result in comment_count_results
        }

        # Ensure all posts have a count (even if 0)
        for post_id in post_ids:
            if post_id not in comment_counts:
                comment_counts[post_id] = 0

        # Cache the result for 5 minutes
        cache.set(cache_key, comment_counts, timeout=300)

        return comment_counts
    except Exception as e:
        # Log error but return default counts to prevent page crashes
        print(f"Error getting comment counts: {str(e)}")
        return dict.fromkeys(post_ids, 0)


def get_websites_bulk(post_ids: list[int]) -> dict[int, GeneratedWebsite]:
    """
    Efficiently get generated websites for multiple posts using a single query.

    Args:
        post_ids: List of post IDs to get generated websites for

    Returns:
        Dictionary mapping post_id to generated website
    """
    if not post_ids:
        return {}

    websites = GeneratedWebsite.query.filter(
        GeneratedWebsite.post_id.in_(post_ids)
    ).all()
    return {website.post_id: website for website in websites}


def visitor_hash_for(token: str) -> str:
    """Keyed hash of a voter cookie token: the only identity we persist.

    HMAC over the app secret, so the stored value is unlinkable without the
    key (never an IP or user agent). Rotating SECRET_KEY invalidates dedup
    against old rows — same failure mode as session invalidation.
    """
    key = (current_app.config["SECRET_KEY"] or "").encode()
    return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()


def visitor_vote_map(target_ids: list[int], *, target: str = "post") -> dict[int, int]:
    """{target_id: value} for the current browser's visitor votes, {} if none.

    Server-rendered voted-state source for feed/detail templates: one bulk
    query keyed on the hashed voter cookie. ``target`` is "post" or
    "comment" and selects the Vote column to key on.
    """
    token = request.cookies.get(VOTER_COOKIE)
    if not token or not target_ids:
        return {}
    column = Vote.post_id if target == "post" else Vote.comment_id
    rows = (
        db.session.query(column, Vote.value)
        .filter(
            Vote.visitor_hash == visitor_hash_for(token),
            column.in_(target_ids),
        )
        .all()
    )
    return dict(rows)


def process_post_title(title: str) -> str:
    """
    Process post titles by removing HTML tags and replacing Reddit references.

    Args:
        title: Original post title

    Returns:
        Processed title
    """
    import re

    # Remove <br>, <p>, and </p> tags
    title = re.sub(r"<br>|<p>|</p>", "", title)

    # Replace "reddit" with "deaddit" (case-insensitive)
    title = re.sub(r"reddit", "deaddit", title, flags=re.IGNORECASE)

    return title


# Allowed output tags for format_content_html:
#   <p> <br> <blockquote> <a href="http://…|https://…">
# Everything else is escaped; this function is the only sanctioned source of
# HTML rendered via |safe on post bodies and comment content.
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_URL_TRAILING_PUNCT = ".,;:!?'\""


def _linkify(escaped_line: str) -> str:
    """Turn bare http(s) URLs in already-escaped text into safe anchors."""

    def _anchor(match: re.Match) -> str:
        url = match.group(0)
        trail = ""
        while url:
            last = url[-1]
            if last in _URL_TRAILING_PUNCT or (
                last == ")" and url.count("(") < url.count(")")
            ):
                trail = last + trail
                url = url[:-1]
            else:
                break
        return f'<a href="{url}" rel="nofollow noopener noreferrer">{url}</a>{trail}'

    return _URL_RE.sub(_anchor, escaped_line)


def format_content_html(text: str | None) -> str:
    """Render user/LLM comment text as a minimal, safe HTML subset.

    stdlib only (html, re). Allowed output tags: ``<p> <br> <blockquote>
    <a href="http(s)://…">``. No other tag is ever emitted.

    Algorithm:
      1. html.escape() everything first (XSS kill).
      2. Split into blocks on blank lines -> each becomes <p>…</p>;
         single newlines inside a block become <br>.
      3. Lines starting with (repeated) "> " prefixes become <blockquote>
         content; nested quotes flatten to a single level.
      4. Bare http(s) URLs are linkified with rel="nofollow noopener
         noreferrer"; trailing punctuation and unbalanced parens stay outside
         the link.
    """
    if not text:
        return ""

    escaped = html.escape(text)
    blocks = re.split(r"\n[ \t]*\n", escaped.strip())
    parts: list[str] = []

    for block in blocks:
        # Flatten quote prefixes: strip every leading "&gt; " so nested quotes
        # collapse to one blockquote level.
        lines = []
        for line in block.split("\n"):
            stripped = line.lstrip()
            quote = stripped.startswith("&gt;")
            if quote:
                stripped = re.sub(r"^(?:&gt;\s?)+", "", stripped)
            lines.append((quote, stripped.rstrip()))

        # Group consecutive lines into runs of quote / normal text.
        runs: list[tuple[bool, list[str]]] = []
        for quote, line in lines:
            if runs and runs[-1][0] == quote:
                runs[-1][1].append(line)
            else:
                runs.append((quote, [line]))

        for quote, run_lines in runs:
            body = "<br>".join(_linkify(line) for line in run_lines if line)
            if not body:
                continue
            if quote:
                parts.append(f"<blockquote><p>{body}</p></blockquote>")
            else:
                parts.append(f"<p>{body}</p>")

    return "".join(parts)
