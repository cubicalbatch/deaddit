"""
Utility functions for the Deaddit application.
"""

from functools import wraps
from typing import Any

from flask import abort
from sqlalchemy import func

from deaddit import cache, db
from deaddit.config import Config

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


def paginate_posts_with_model_cycling(
    all_posts: list[Any], all_models: list[str], page: int, posts_per_page: int
) -> tuple[list[Any], int, bool]:
    """
    Paginate posts while cycling through models for balanced representation.

    Args:
        all_posts: List of all posts to paginate
        all_models: List of model names to cycle through
        page: Current page number (1-indexed)
        posts_per_page: Number of posts per page

    Returns:
        Tuple of (paginated_posts, total_posts, has_more)
    """
    import random
    from itertools import cycle

    # Randomize the order of models
    models_copy = all_models.copy()
    random.shuffle(models_copy)
    model_cycle = cycle(models_copy)

    # Create a dictionary to store posts by model
    posts_by_model = {model: [] for model in models_copy}

    # Populate the posts_by_model dictionary
    for post in all_posts:
        if post.model in posts_by_model:
            posts_by_model[post.model].append(post)

    # Create the final ordered list of posts
    ordered_posts = []
    while len(ordered_posts) < len(all_posts):
        current_model = next(model_cycle)
        if posts_by_model[current_model]:
            ordered_posts.append(posts_by_model[current_model].pop(0))

    # Apply pagination
    offset = (page - 1) * posts_per_page
    paginated_posts = ordered_posts[offset : offset + posts_per_page]
    total_posts = len(all_posts)
    has_more = total_posts > page * posts_per_page

    return paginated_posts, total_posts, has_more


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
