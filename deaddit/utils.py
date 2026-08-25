"""
Utility functions for the Deaddit application.
"""

import html
import re
from functools import wraps

from flask import abort
from sqlalchemy import func

from deaddit.config import Config
from deaddit.extensions import cache, db

from .models import Comment


def production_disabled(f):
    """Decorator that returns 404 for endpoints that should be disabled in production.

    This decorator checks the PRODUCTION configuration setting and returns a 404 error
    if the application is running in production mode. This is used to disable admin
    and ingestion endpoints in production deployments.

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


@cache.memoize(timeout=300)
def get_single_comment_count(post_id: int) -> int:
    """
    Get comment count for a single post with caching.

    Args:
        post_id: Post ID to get comment count for

    Returns:
        Comment count for the post
    """
    return Comment.query.filter_by(post_id=post_id).count()


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
        return (
            f'<a href="{url}" rel="nofollow noopener noreferrer">{url}</a>{trail}'
        )

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
