"""
Admin interface for Deaddit content management.
Provides web-based UI for job management and content generation.
"""

import base64
import logging
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import desc, func
from sqlalchemy.exc import SQLAlchemyError

from deaddit import db
from deaddit.config import Config
from deaddit.llm import routing
from deaddit.llm.capabilities import probe_endpoint, set_manual_override
from deaddit.models import (
    Agent,
    AgentRun,
    ApiEndpointConfig,
    ApiModel,
    Comment,
    DegeneracyFlag,
    EndpointCapability,
    Job,
    LLMProvider,
    LLMUsage,
    ModelRoute,
    Post,
    PromptPin,
    PromptRenderAudit,
    PromptTemplate,
    PromptTemplateVersion,
    Report,
    Subdeaddit,
    User,
)
from deaddit.services.content import (
    ContentValidationError,
    create_subdeaddit,
    create_user,
)
from deaddit.settings import SecretNotPersistable
from deaddit.utils import production_disabled

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def fetch_all_models_from_api(api_url, api_key, timeout=30):
    """
    Fetch all models from an AI API with comprehensive pagination support.
    Tries multiple pagination methods and fallbacks to ensure we get all models.
    """
    import requests

    headers = {"Authorization": f"Bearer {api_key}"}
    all_models = []

    # Strategy 1: Try single request first (most APIs return all models this way)
    try:
        logger.info("Attempting to fetch all models in single request...")
        response = requests.get(
            f"{api_url.rstrip('/')}/models", headers=headers, timeout=timeout
        )

        if response.status_code == 200:
            models_data = response.json()
            models = extract_models_from_response(models_data)

            if models:
                logger.info(
                    f"Successfully fetched {len(models)} models in single request"
                )
                return models, f"Fetched {len(models)} models"
    except Exception as e:
        logger.debug(f"Single request failed: {e}")

    # Strategy 2: Try pagination if single request failed or returned no models
    logger.info("Single request failed or returned no models, trying pagination...")

    pagination_styles = [
        # OpenAI-style limit/after
        lambda page, limit: {
            "limit": limit,
            "after": all_models[-1] if page > 1 and all_models else None,
        },
        # Standard offset/limit
        lambda page, limit: {"limit": limit, "offset": (page - 1) * limit},
        # Page-based pagination
        lambda page, limit: {"page": page, "per_page": limit},
        # Alternative page-based
        lambda page, limit: {"page": page, "limit": limit},
        # Just limit (some APIs auto-paginate)
        lambda page, limit: {"limit": limit} if page == 1 else None,
    ]

    per_page = 100
    max_pages = 50  # Safety limit

    for style_idx, param_generator in enumerate(pagination_styles):
        if all_models:  # Already found models with a previous style
            break

        logger.debug(f"Trying pagination style {style_idx + 1}")
        page_models_found = True
        page = 1

        while page <= max_pages and page_models_found:
            try:
                params = param_generator(page, per_page)
                if params is None:  # Some styles don't support multi-page
                    break

                # Remove None values from params
                params = {k: v for k, v in params.items() if v is not None}

                response = requests.get(
                    f"{api_url.rstrip('/')}/models",
                    headers=headers,
                    params=params,
                    timeout=timeout,
                )

                if response.status_code == 200:
                    models_data = response.json()
                    page_models = extract_models_from_response(models_data)

                    if page_models:
                        all_models.extend(page_models)
                        logger.debug(f"Page {page}: found {len(page_models)} models")

                        # Check if there are more pages
                        has_more = check_has_more_pages(
                            models_data, page_models, per_page
                        )
                        if not has_more:
                            logger.info(
                                f"Pagination complete - fetched {len(all_models)} total models"
                            )
                            break
                    else:
                        page_models_found = False

                    page += 1
                else:
                    logger.debug(
                        f"Pagination request failed with status {response.status_code}"
                    )
                    break

            except Exception as e:
                logger.debug(f"Pagination request failed: {e}")
                break

        if all_models:
            message = f"Fetched {len(all_models)} models using pagination (style {style_idx + 1})"
            logger.info(message)
            return all_models, message

    # If we get here, all methods failed
    logger.warning("All model fetching methods failed")
    return [], "Failed to fetch models from API"


def extract_models_from_response(models_data):
    """Extract model names from API response, handling different response formats."""
    models = []

    if "data" in models_data:
        # OpenAI-style response
        models = [
            model.get("id", "Unknown")
            for model in models_data["data"]
            if model.get("id")
        ]
    elif "models" in models_data:
        # Alternative format
        models = [
            model.get("id", model.get("name", "Unknown"))
            for model in models_data["models"]
            if model.get("id") or model.get("name")
        ]
    elif isinstance(models_data, list):
        # Direct list of model objects
        models = [
            model.get("id", model.get("name", "Unknown"))
            for model in models_data
            if isinstance(model, dict) and (model.get("id") or model.get("name"))
        ]

    # Filter out "Unknown" models and deduplicate
    models = list({m for m in models if m != "Unknown"})
    return models


def check_has_more_pages(models_data, page_models, per_page):
    """Check if there are more pages of models to fetch."""
    # Explicit pagination indicators
    if "has_more" in models_data:
        return models_data.get("has_more", False)
    if "next" in models_data:
        return models_data.get("next") is not None
    if "pagination" in models_data:
        pagination = models_data["pagination"]
        return pagination.get("has_more", False) or pagination.get("next") is not None

    # Heuristic: if we got exactly per_page models, there might be more
    return len(page_models) == per_page


def admin_required(f):
    """Decorator to check admin authentication when API_TOKEN is set."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Get API_TOKEN from database first, then environment
        from deaddit.config import Config

        api_token = Config.get("API_TOKEN")

        # If no API_TOKEN is set, allow access
        if not api_token:
            return f(*args, **kwargs)

        # Check if user is authenticated
        if not session.get("admin_authenticated"):
            return redirect(url_for("admin.login"))

        return f(*args, **kwargs)

    return decorated_function


@admin_bp.route("/login", methods=["GET", "POST"])
@production_disabled
def login():
    """Admin login page."""
    from deaddit.config import Config

    api_token = Config.get("API_TOKEN")

    # If no API_TOKEN is set, redirect to dashboard
    if not api_token:
        return redirect(url_for("admin.dashboard"))

    if request.method == "POST":
        provided_token = request.form.get("api_token")

        if provided_token == api_token:
            session["admin_authenticated"] = True
            session.permanent = True
            flash("Successfully authenticated!", "success")
            return redirect(url_for("admin.dashboard"))
        else:
            flash("Invalid API token.", "error")

    return render_template("admin/login.html")


@admin_bp.route("/logout")
@production_disabled
def logout():
    """Admin logout."""
    session.pop("admin_authenticated", None)
    flash("You have been logged out.", "info")
    return redirect(url_for("admin.login"))


@admin_bp.route("/")
@admin_bp.route("/dashboard")
@production_disabled
@admin_required
def dashboard():
    """Agent-first admin dashboard: agents, platform pulse, LLM spend."""

    now = datetime.utcnow()
    since = now - timedelta(days=1)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # --- Agents overview ---
    # One grouped scan for run tallies (indexed on started_at); next wake is a
    # single MIN over enabled agents.
    run_rows = (
        db.session.query(AgentRun.status, func.count(AgentRun.id))
        .filter(AgentRun.started_at >= since)
        .group_by(AgentRun.status)
        .all()
    )
    runs_by_status = dict(run_rows)
    enabled_count = Agent.query.filter_by(is_enabled=True).count()
    failing_agents = Agent.query.filter(
        Agent.is_enabled.is_(True), Agent.consecutive_failures > 0
    ).count()
    next_wake = (
        Agent.query.filter(Agent.is_enabled.is_(True), Agent.next_run_at.isnot(None))
        .order_by(Agent.next_run_at.asc())
        .first()
    )
    agents_overview = {
        "enabled": enabled_count,
        "total": Agent.query.count(),
        "runs_24h": sum(runs_by_status.values()),
        "completed_24h": runs_by_status.get("completed", 0),
        "failed_24h": runs_by_status.get("failed", 0),
        "failing_agents": failing_agents,
        "next_wake": next_wake.next_run_at if next_wake else None,
    }

    # --- Platform pulse ---
    # Provenance buckets follow Resolution 9: model marker 'agent:*' vs
    # 'seed' vs anything else. Two indexed GROUP BYs + two tiny counters.
    post_rows = (
        db.session.query(Post.model, func.count(Post.id))
        .filter(Post.created_at >= today_start)
        .group_by(Post.model)
        .all()
    )
    comment_rows = (
        db.session.query(Comment.model, func.count(Comment.id))
        .filter(Comment.created_at >= today_start)
        .group_by(Comment.model)
        .all()
    )

    def _bucket(rows):
        out = {"agent": 0, "seed": 0}
        for marker, n in rows:
            if marker and str(marker).startswith("agent:"):
                out["agent"] += n
            elif marker == "seed":
                out["seed"] += n
        return out

    degeneracy_active = DegeneracyFlag.query.filter(
        DegeneracyFlag.created_at >= since
    ).count()
    reports_pending = Report.query.filter(Report.status == "open").count()
    pulse = {
        "posts_today": _bucket(post_rows),
        "comments_today": _bucket(comment_rows),
        "degeneracy_flags_24h": degeneracy_active,
        "reports_open": reports_pending,
    }

    # --- LLM spend today ---
    spend_row = (
        db.session.query(
            func.coalesce(func.sum(LLMUsage.total_tokens), 0),
            func.sum(LLMUsage.estimated_cost),
            func.count(LLMUsage.id),
        )
        .filter(LLMUsage.created_at >= today_start)
        .one()
    )
    llm_spend = {
        "tokens": spend_row[0],
        # None stays None when nothing priced was spent today (never fake $0).
        "cost": spend_row[1],
        "calls": spend_row[2],
    }

    return render_template(
        "admin/dashboard.html",
        agents=agents_overview,
        pulse=pulse,
        llm_spend=llm_spend,
    )


@admin_bp.route("/content")
@production_disabled
@admin_required
def content():
    """Content management page."""

    # Get content statistics
    content_stats = {
        "posts": Post.query.count(),
        "comments": Comment.query.count(),
        "users": User.query.count(),
        "subdeaddits": Subdeaddit.query.count(),
    }

    # Get recent content
    recent_posts = Post.query.order_by(desc(Post.created_at)).limit(10).all()
    recent_comments = Comment.query.order_by(desc(Comment.created_at)).limit(10).all()

    # Subdeaddit filter options rendered server-side in the template
    # (replaces the old per_page=1000 client-side fetch).
    subdeaddit_names = [
        name for (name,) in db.session.query(Subdeaddit.name).order_by(Subdeaddit.name)
    ]

    return render_template(
        "admin/content.html",
        content_stats=content_stats,
        recent_posts=recent_posts,
        recent_comments=recent_comments,
        subdeaddit_names=subdeaddit_names,
    )


# CRUD API endpoints for content management


def _user_payload(user):
    """Serialize a user row for the admin content API."""
    return {
        "username": user.username,
        "age": user.age,
        "gender": user.gender,
        "occupation": user.occupation,
        "education": user.education,
        "bio": user.bio,
        "interests": user.interests or "",
        "personality_traits": user.personality_traits or "",
        "writing_style": user.writing_style or "",
        "posts_count": Post.query.filter_by(user=user.username).count(),
        "comments_count": Comment.query.filter_by(user=user.username).count(),
    }


@admin_bp.route("/api/users")
@production_disabled
@admin_required
def api_users():
    """Get users with pagination and search."""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 25, type=int)
    search = request.args.get("search", "")

    query = User.query
    if search:
        query = query.filter(
            User.username.contains(search)
            | User.occupation.contains(search)
            | User.bio.contains(search)
        )

    users = query.order_by(User.username).paginate(
        page=page, per_page=per_page, error_out=False
    )

    return jsonify(
        {
            "users": [_user_payload(user) for user in users.items],
            "total": users.total,
            "pages": users.pages,
            "current_page": page,
        }
    )


@admin_bp.route("/api/users/<username>", methods=["PUT"])
@production_disabled
@admin_required
def api_update_user(username):
    """Update a user."""
    user = User.query.get_or_404(username)
    data = request.json

    try:
        user.age = data.get("age", user.age)
        user.gender = data.get("gender", user.gender)
        user.occupation = data.get("occupation", user.occupation)
        user.education = data.get("education", user.education)
        user.bio = data.get("bio", user.bio)
        user.interests = data.get("interests", user.interests)
        user.personality_traits = data.get(
            "personality_traits", user.personality_traits
        )
        user.writing_style = data.get("writing_style", user.writing_style)

        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating user {username}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@admin_bp.route("/api/users/<username>", methods=["GET"])
@production_disabled
@admin_required
def api_get_user(username):
    """Fetch one user by username (targeted single-row read for edit flows)."""
    user = User.query.get_or_404(username)
    return jsonify(_user_payload(user))


@admin_bp.route("/api/users/<username>", methods=["DELETE"])
@production_disabled
@admin_required
def api_delete_user(username):
    """Delete a user and all associated content."""
    user = User.query.get_or_404(username)

    try:
        posts_count = Post.query.filter_by(user=username).count()
        comments_count = Comment.query.filter_by(user=username).count()

        # Delete associated content (cascade should handle this, but being explicit)
        Comment.query.filter_by(user=username).delete()
        Post.query.filter_by(user=username).delete()

        db.session.delete(user)
        db.session.commit()

        return jsonify(
            {
                "success": True,
                "deleted": {
                    "user": username,
                    "posts": posts_count,
                    "comments": comments_count,
                },
            }
        )
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting user {username}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@admin_bp.route("/api/users/bulk-delete", methods=["POST"])
@production_disabled
@admin_required
def api_bulk_delete_users():
    """Delete multiple users."""
    usernames = request.json.get("usernames", [])
    if not usernames:
        return jsonify({"success": False, "error": "No usernames provided"}), 400

    try:
        deleted_count = 0
        total_posts = 0
        total_comments = 0

        for username in usernames:
            user = User.query.get(username)
            if user:
                posts_count = Post.query.filter_by(user=username).count()
                comments_count = Comment.query.filter_by(user=username).count()

                Comment.query.filter_by(user=username).delete()
                Post.query.filter_by(user=username).delete()
                db.session.delete(user)

                deleted_count += 1
                total_posts += posts_count
                total_comments += comments_count

        db.session.commit()

        return jsonify(
            {
                "success": True,
                "deleted": {
                    "users": deleted_count,
                    "posts": total_posts,
                    "comments": total_comments,
                },
            }
        )
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error bulk deleting users: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


def _subdeaddit_payload(sub):
    """Serialize a subdeaddit row for the admin content API."""
    return {
        "name": sub.name,
        "description": sub.description or "",
        "post_types": sub.post_types or "",
        "posts_count": Post.query.filter_by(subdeaddit_name=sub.name).count(),
    }


@admin_bp.route("/api/subdeaddits")
@production_disabled
@admin_required
def api_subdeaddits():
    """Get subdeaddits with pagination and search."""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 25, type=int)
    search = request.args.get("search", "")

    query = Subdeaddit.query
    if search:
        query = query.filter(
            Subdeaddit.name.contains(search) | Subdeaddit.description.contains(search)
        )

    subdeaddits = query.order_by(Subdeaddit.name).paginate(
        page=page, per_page=per_page, error_out=False
    )

    return jsonify(
        {
            "subdeaddits": [_subdeaddit_payload(sub) for sub in subdeaddits.items],
            "total": subdeaddits.total,
            "pages": subdeaddits.pages,
            "current_page": page,
        }
    )


@admin_bp.route("/api/subdeaddits/<name>", methods=["PUT"])
@production_disabled
@admin_required
def api_update_subdeaddit(name):
    """Update a subdeaddit."""
    subdeaddit = Subdeaddit.query.get_or_404(name)
    data = request.json

    try:
        subdeaddit.description = data.get("description", subdeaddit.description)

        # Handle post_types - it should be a JSON string
        post_types = data.get("post_types")
        if post_types is not None:
            if isinstance(post_types, str):
                # Validate JSON
                import json

                json.loads(post_types)  # This will raise an exception if invalid
                subdeaddit.post_types = post_types
            else:
                # Convert to JSON string if it's a dict/list
                import json

                subdeaddit.post_types = json.dumps(post_types)

        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating subdeaddit {name}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@admin_bp.route("/api/subdeaddits/<name>", methods=["GET"])
@production_disabled
@admin_required
def api_get_subdeaddit(name):
    """Fetch one subdeaddit by name (targeted single-row read for edit flows)."""
    subdeaddit = Subdeaddit.query.get_or_404(name)
    return jsonify(_subdeaddit_payload(subdeaddit))


@admin_bp.route("/api/subdeaddits/<name>", methods=["DELETE"])
@production_disabled
@admin_required
def api_delete_subdeaddit(name):
    """Delete a subdeaddit and all associated posts."""
    subdeaddit = Subdeaddit.query.get_or_404(name)

    try:
        # Get impact stats before deletion
        posts_count = Post.query.filter_by(subdeaddit_name=name).count()
        comments_count = (
            Comment.query.join(Post).filter(Post.subdeaddit_name == name).count()
        )

        # Delete associated content
        # First get comment IDs to delete (can't use join().delete())
        comment_ids = [
            c.id
            for c in Comment.query.join(Post).filter(Post.subdeaddit_name == name).all()
        ]
        for comment_id in comment_ids:
            Comment.query.filter_by(id=comment_id).delete()

        Post.query.filter_by(subdeaddit_name=name).delete()

        db.session.delete(subdeaddit)
        db.session.commit()

        return jsonify(
            {
                "success": True,
                "deleted": {
                    "subdeaddit": name,
                    "posts": posts_count,
                    "comments": comments_count,
                },
            }
        )
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting subdeaddit {name}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@admin_bp.route("/api/subdeaddits/bulk-delete", methods=["POST"])
@production_disabled
@admin_required
def api_bulk_delete_subdeaddits():
    """Delete multiple subdeaddits."""
    names = request.json.get("names", [])
    if not names:
        return jsonify({"success": False, "error": "No names provided"}), 400

    try:
        deleted_count = 0
        total_posts = 0
        total_comments = 0

        for name in names:
            subdeaddit = Subdeaddit.query.get(name)
            if subdeaddit:
                posts_count = Post.query.filter_by(subdeaddit_name=name).count()
                comments_count = (
                    Comment.query.join(Post)
                    .filter(Post.subdeaddit_name == name)
                    .count()
                )

                # First get comment IDs to delete (can't use join().delete())
                comment_ids = [
                    c.id
                    for c in Comment.query.join(Post)
                    .filter(Post.subdeaddit_name == name)
                    .all()
                ]
                for comment_id in comment_ids:
                    Comment.query.filter_by(id=comment_id).delete()

                Post.query.filter_by(subdeaddit_name=name).delete()
                db.session.delete(subdeaddit)

                deleted_count += 1
                total_posts += posts_count
                total_comments += comments_count

        db.session.commit()

        return jsonify(
            {
                "success": True,
                "deleted": {
                    "subdeaddits": deleted_count,
                    "posts": total_posts,
                    "comments": total_comments,
                },
            }
        )
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error bulk deleting subdeaddits: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


def _post_payload(post):
    """Serialize a post row for the admin content API."""
    return {
        "id": post.id,
        "title": post.title or "",
        "content": (
            post.content[:200] + "..."
            if post.content and len(post.content) > 200
            else post.content
        )
        or "",
        "username": post.user,
        "subdeaddit_name": post.subdeaddit_name,
        "score": post.score or 0,
        "post_type": post.post_type or "",
        "comments_count": Comment.query.filter_by(post_id=post.id).count(),
        "created_at": post.created_at.isoformat() if post.created_at else "",
        "model": post.model or "",
    }


@admin_bp.route("/api/posts")
@production_disabled
@admin_required
def api_posts():
    """Get posts with pagination, search, and filtering."""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 25, type=int)
    search = request.args.get("search", "")
    subdeaddit_filter = request.args.get("subdeaddit", "")

    query = Post.query
    if search:
        query = query.filter(
            Post.title.contains(search) | Post.content.contains(search)
        )
    if subdeaddit_filter:
        query = query.filter(Post.subdeaddit_name == subdeaddit_filter)

    posts = query.order_by(desc(Post.created_at)).paginate(
        page=page, per_page=per_page, error_out=False
    )

    return jsonify(
        {
            "posts": [_post_payload(post) for post in posts.items],
            "total": posts.total,
            "pages": posts.pages,
            "current_page": page,
        }
    )


@admin_bp.route("/api/posts/<int:post_id>", methods=["PUT"])
@production_disabled
@admin_required
def api_update_post(post_id):
    """Update a post."""
    post = Post.query.get_or_404(post_id)
    data = request.json

    try:
        post.title = data.get("title", post.title)
        post.content = data.get("content", post.content)
        post.score = data.get("score", post.score)
        post.post_type = data.get("post_type", post.post_type)

        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating post {post_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@admin_bp.route("/api/posts/<int:post_id>", methods=["GET"])
@production_disabled
@admin_required
def api_get_post(post_id):
    """Fetch one post by id (targeted single-row read for edit flows)."""
    post = Post.query.get_or_404(post_id)
    return jsonify(_post_payload(post))


@admin_bp.route("/api/posts/<int:post_id>", methods=["DELETE"])
@production_disabled
@admin_required
def api_delete_post(post_id):
    """Delete a post and all associated comments."""
    post = Post.query.get_or_404(post_id)

    try:
        comments_count = Comment.query.filter_by(post_id=post_id).count()

        # Delete associated comments
        Comment.query.filter_by(post_id=post_id).delete()

        db.session.delete(post)
        db.session.commit()

        return jsonify(
            {"success": True, "deleted": {"post": post_id, "comments": comments_count}}
        )
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting post {post_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@admin_bp.route("/api/posts/bulk-delete", methods=["POST"])
@production_disabled
@admin_required
def api_bulk_delete_posts():
    """Delete multiple posts."""
    post_ids = request.json.get("post_ids", [])
    if not post_ids:
        return jsonify({"success": False, "error": "No post IDs provided"}), 400

    try:
        deleted_count = 0
        total_comments = 0

        for post_id in post_ids:
            post = Post.query.get(post_id)
            if post:
                comments_count = Comment.query.filter_by(post_id=post_id).count()

                Comment.query.filter_by(post_id=post_id).delete()
                db.session.delete(post)

                deleted_count += 1
                total_comments += comments_count

        db.session.commit()

        return jsonify(
            {
                "success": True,
                "deleted": {"posts": deleted_count, "comments": total_comments},
            }
        )
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error bulk deleting posts: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


def _comment_payload(comment):
    """Serialize a comment row for the admin content API."""
    return {
        "id": comment.id,
        "content": (
            comment.content[:150] + "..."
            if comment.content and len(comment.content) > 150
            else comment.content
        )
        or "",
        "username": comment.user,
        "post_id": comment.post_id,
        "post_title": comment.post.title if comment.post else "Unknown",
        "parent_id": comment.parent_id,
        "score": comment.score or 0,
        "created_at": comment.created_at.isoformat() if comment.created_at else "",
        "model": comment.model or "",
    }


@admin_bp.route("/api/comments")
@production_disabled
@admin_required
def api_comments():
    """Get comments with pagination and search."""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 25, type=int)
    search = request.args.get("search", "")

    query = Comment.query
    if search:
        query = query.filter(Comment.content.contains(search))

    comments = query.order_by(desc(Comment.created_at)).paginate(
        page=page, per_page=per_page, error_out=False
    )

    return jsonify(
        {
            "comments": [_comment_payload(c) for c in comments.items],
            "total": comments.total,
            "pages": comments.pages,
            "current_page": page,
        }
    )


@admin_bp.route("/api/comments/<int:comment_id>", methods=["PUT"])
@production_disabled
@admin_required
def api_update_comment(comment_id):
    """Update a comment."""
    comment = Comment.query.get_or_404(comment_id)
    data = request.json

    try:
        comment.content = data.get("content", comment.content)
        comment.score = data.get("score", comment.score)

        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating comment {comment_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@admin_bp.route("/api/comments/<int:comment_id>", methods=["GET"])
@production_disabled
@admin_required
def api_get_comment(comment_id):
    """Fetch one comment by id (targeted single-row read for edit flows)."""
    comment = Comment.query.get_or_404(comment_id)
    return jsonify(_comment_payload(comment))


@admin_bp.route("/api/comments/<int:comment_id>", methods=["DELETE"])
@production_disabled
@admin_required
def api_delete_comment(comment_id):
    """Delete a comment and all child comments."""
    comment = Comment.query.get_or_404(comment_id)

    try:
        # Get all child comments recursively
        def get_child_comments(parent_id):
            children = Comment.query.filter_by(parent_id=parent_id).all()
            all_children = children.copy()
            for child in children:
                all_children.extend(get_child_comments(child.id))
            return all_children

        child_comments = get_child_comments(comment_id)
        child_count = len(child_comments)

        # Delete child comments first
        for child in child_comments:
            db.session.delete(child)

        # Delete the comment itself
        db.session.delete(comment)
        db.session.commit()

        return jsonify(
            {
                "success": True,
                "deleted": {"comment": comment_id, "child_comments": child_count},
            }
        )
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting comment {comment_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@admin_bp.route("/api/comments/bulk-delete", methods=["POST"])
@production_disabled
@admin_required
def api_bulk_delete_comments():
    """Delete multiple comments."""
    comment_ids = request.json.get("comment_ids", [])
    if not comment_ids:
        return jsonify({"success": False, "error": "No comment IDs provided"}), 400

    try:
        deleted_count = 0
        total_children = 0

        # Helper function to get child comments
        def get_child_comments(parent_id):
            children = Comment.query.filter_by(parent_id=parent_id).all()
            all_children = children.copy()
            for child in children:
                all_children.extend(get_child_comments(child.id))
            return all_children

        for comment_id in comment_ids:
            comment = Comment.query.get(comment_id)
            if comment:
                child_comments = get_child_comments(comment_id)
                child_count = len(child_comments)

                # Delete child comments first
                for child in child_comments:
                    db.session.delete(child)

                # Delete the comment itself
                db.session.delete(comment)

                deleted_count += 1
                total_children += child_count

        db.session.commit()

        return jsonify(
            {
                "success": True,
                "deleted": {
                    "comments": deleted_count,
                    "child_comments": total_children,
                },
            }
        )
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error bulk deleting comments: {e}")


def _sparkline(values: list[float | None], width: int = 120, height: int = 28) -> str:
    """SVG polyline points for a series; None values are skipped, not faked."""
    present = [v for v in values if v is not None]
    if not present:
        return ""
    lo, hi = min(present), max(present)
    span = (hi - lo) or 1.0
    step = width / max(len(values) - 1, 1)
    points = []
    for i, value in enumerate(values):
        if value is None:
            continue
        x = round(i * step, 2)
        y = round(height - ((value - lo) / span) * (height - 4) - 2, 2)
        points.append(f"{x},{y}")
    return " ".join(points)


@admin_bp.route("/analytics")
@production_disabled
@admin_required
def analytics():
    """Platform-dynamics analytics tab (Phase D6): daily rollups + watchlist.

    The previous placeholder body rendered a template that no longer existed
    (TemplateNotFound since the UX-5 rebuild); this revives the page on
    PlatformDaily rollups written by the nightly metrics job.
    """
    from deaddit.dynamics.degeneracy import flagged_hot_authors
    from deaddit.dynamics.metrics import daily_series

    series = daily_series(30)
    watchlist = (
        DegeneracyFlag.query.order_by(DegeneracyFlag.created_at.desc()).limit(50).all()
    )

    def _column(name: str) -> list[float | None]:
        return [getattr(row, name) for row in series]

    latest = series[-1] if series else None
    return render_template(
        "admin/analytics.html",
        series=series,
        latest=latest,
        watchlist=watchlist,
        demoted_authors=flagged_hot_authors(),
        spark_cost=_sparkline(_column("llm_cost_usd")),
        spark_cpe=_sparkline(_column("cost_per_engagement")),
        spark_actions=_sparkline(_column("actions_per_active")),
    )


@admin_bp.route("/settings")
@production_disabled
@admin_required
def settings():
    """Settings and configuration page."""

    # Get current configuration from database
    all_settings = Config.get_all_settings()
    providers = LLMProvider.query.order_by(
        LLMProvider.is_default.desc(), LLMProvider.id.asc()
    ).all()
    default_provider = LLMProvider.get_default()

    config = {
        "openai_api_url": all_settings["OPENAI_API_URL"]["value"],
        "openai_model": all_settings["OPENAI_MODEL"]["value"],
        "api_base_url": all_settings["API_BASE_URL"]["value"],
        "models": all_settings["MODELS"]["value"],
        "api_token_set": all_settings["API_TOKEN"]["value"] == "***set***",
        "openai_key_set": all_settings["OPENAI_KEY"]["value"] != "***not set***",
        "all_settings": all_settings,
        "default_provider": default_provider.to_dict() if default_provider else None,
    }

    return render_template("admin/settings.html", config=config, providers=providers)


@admin_bp.route("/capabilities")
@production_disabled
@admin_required
def capabilities():
    """Capability verdicts per endpoint/model, with probe/override forms."""
    caps = EndpointCapability.query.order_by(
        EndpointCapability.api_url, EndpointCapability.model_name
    ).all()
    endpoints = ApiEndpointConfig.query.order_by(ApiEndpointConfig.api_url).all()
    return render_template(
        "admin/capabilities.html",
        capabilities=caps,
        endpoints=endpoints,
    )


@admin_bp.route("/capabilities/probe", methods=["POST"])
@production_disabled
@admin_required
def capabilities_probe():
    """Run a tools probe for one endpoint/model and flash the verdict."""
    api_url = request.form.get("api_url", "").strip()
    model_name = request.form.get("model_name", "").strip()
    api_key = request.form.get("api_key", "").strip() or None
    if not api_url or not model_name:
        flash("Both API URL and model name are required.", "error")
        return redirect(url_for("admin.capabilities"))
    try:
        cap = probe_endpoint(api_url, model_name, api_key=api_key)
    except Exception as exc:
        flash(f"Probe could not determine a verdict: {exc}", "error")
        return redirect(url_for("admin.capabilities"))
    verdict = "supported" if cap.supports_tools else "NOT supported"
    flash(
        f"Probe verdict for {model_name}: tool calling {verdict} "
        f"(probe_method={cap.probe_method}).",
        "success" if cap.supports_tools else "warning",
    )
    return redirect(url_for("admin.capabilities"))


@admin_bp.route("/capabilities/override", methods=["POST"])
@production_disabled
@admin_required
def capabilities_override():
    """Record a manual capability override for one endpoint/model."""
    api_url = request.form.get("api_url", "").strip()
    model_name = request.form.get("model_name", "").strip()
    supports_tools = request.form.get("supports_tools") == "true"
    if not api_url or not model_name:
        flash("Both API URL and model name are required.", "error")
        return redirect(url_for("admin.capabilities"))
    set_manual_override(api_url, model_name, supports_tools)
    flash(
        f"Manual override saved: {model_name} tool calling "
        f"{'supported' if supports_tools else 'disabled'}.",
        "success",
    )
    return redirect(url_for("admin.capabilities"))


@admin_bp.route("/api/system-info")
@production_disabled
@admin_required
def system_info_api():
    """API endpoint to get system information."""
    import sys

    import apscheduler
    import flask
    import sqlalchemy

    return jsonify(
        {
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "flask_version": flask.__version__,
            "sqlalchemy_version": sqlalchemy.__version__,
            "apscheduler_version": apscheduler.__version__,
        }
    )


@admin_bp.route("/api/save-config", methods=["POST"])
@production_disabled
@admin_required
def save_config_api():
    """API endpoint to save configuration to database."""
    try:
        data = request.get_json()

        # Save configuration values to database
        endpoint_url = None
        if data.get("openai_api_url"):
            endpoint_url = data["openai_api_url"].rstrip("/")
            Config.set("OPENAI_API_URL", endpoint_url)

        # Empty-means-unchanged: an absent or blank secret never overwrites the
        # stored value. Only a non-empty key is written. Since A6 secrets are
        # environment-only: a non-empty key is refused, other settings in the
        # same request still commit.
        openai_key = (data.get("openai_key") or "").strip()
        openai_key_refused = False
        if openai_key:
            try:
                if endpoint_url:
                    Config.set_api_key_for_endpoint(endpoint_url, openai_key)
                else:
                    # If no endpoint URL, use current endpoint
                    current_endpoint = Config.get("OPENAI_API_URL")
                    if current_endpoint:
                        Config.set_api_key_for_endpoint(current_endpoint, openai_key)
                    else:
                        Config.set("OPENAI_KEY", openai_key)
            except SecretNotPersistable:
                openai_key_refused = True

        if data.get("openai_model"):
            Config.set("OPENAI_MODEL", data["openai_model"])
        if data.get("api_base_url"):
            Config.set("API_BASE_URL", data["api_base_url"].rstrip("/"))
        if data.get("models"):
            Config.set("MODELS", data["models"])

        # Return updated config
        current_endpoint = Config.get("OPENAI_API_URL")
        config = {
            "openai_api_url": current_endpoint or "Not set",
            "openai_key_set": bool(Config.get_api_key_for_endpoint(current_endpoint)),
        }

        if openai_key_refused:
            return jsonify(
                {
                    "success": False,
                    "message": "OPENAI_KEY is environment-only since refactor A6 — set it in your environment/.env (other settings were saved).",
                    "config": config,
                }
            )

        return jsonify(
            {
                "success": True,
                "message": "Configuration saved to database successfully",
                "config": config,
            }
        )

    except Exception as e:
        return jsonify(
            {"success": False, "message": f"Failed to save configuration: {str(e)}"}
        )


@admin_bp.route("/api/save-deaddit-config", methods=["POST"])
@production_disabled
@admin_required
def save_deaddit_config_api():
    """API endpoint to save Deaddit configuration to database."""
    try:
        data = request.get_json()

        # Save configuration values to database
        if data.get("api_base_url"):
            Config.set("API_BASE_URL", data["api_base_url"].rstrip("/"))

        # Empty-means-unchanged: an absent or blank token never overwrites the
        # stored value. Only a non-empty token is validated and written. Since
        # A6 the token is environment-only: a non-empty token is refused.
        token = (data.get("api_token") or "").strip()
        api_token_refused = False
        if token:
            if len(token) < 3:
                return jsonify(
                    {
                        "success": False,
                        "message": "API Token must be at least 3 characters long",
                    }
                )
            try:
                Config.set("API_TOKEN", token)
            except SecretNotPersistable:
                api_token_refused = True

        if api_token_refused:
            return jsonify(
                {
                    "success": False,
                    "message": "API_TOKEN is environment-only since refactor A6 — set it in your environment/.env.",
                }
            )

        return jsonify(
            {"success": True, "message": "Deaddit configuration saved successfully"}
        )

    except Exception as e:
        return jsonify(
            {"success": False, "message": f"Failed to save configuration: {str(e)}"}
        )


@admin_bp.route("/api/test-connection", methods=["POST"])
@production_disabled
@admin_required
def test_connection_api():
    """API endpoint to test AI service connection with custom parameters."""

    import requests

    try:
        data = request.get_json(silent=True) or {}
        provider_id = data.get("provider_id")
        api_url = data.get("api_url")
        api_key = data.get("api_key")

        if provider_id:
            provider = db.session.get(LLMProvider, int(provider_id))
            if provider:
                api_url = api_url or provider.api_url
                if not api_key or api_key == "••••••••••••••••":
                    api_key = provider.api_key

        if not api_url:
            return jsonify(
                {
                    "success": False,
                    "message": "API URL is required",
                    "status_code": None,
                }
            )

        # If no API key provided or masked key, try to use saved key for this endpoint
        if not api_key or api_key == "••••••••••••••••":
            api_key = Config.get_api_key_for_endpoint(api_url)

        # Test connection to AI service
        headers = {}
        if api_key and api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"

        response = requests.get(
            f"{api_url.rstrip('/')}/models", headers=headers, timeout=10
        )

        if response.status_code == 200:
            return jsonify(
                {
                    "success": True,
                    "message": "Connection successful! AI service is reachable.",
                    "status_code": response.status_code,
                }
            )
        else:
            return jsonify(
                {
                    "success": False,
                    "message": f"AI service returned HTTP {response.status_code}",
                    "status_code": response.status_code,
                }
            )

    except requests.exceptions.ConnectionError:
        return jsonify(
            {
                "success": False,
                "message": "Cannot connect to AI service. Check API URL.",
                "status_code": None,
            }
        )
    except requests.exceptions.Timeout:
        return jsonify(
            {
                "success": False,
                "message": "Connection timeout. AI service may be slow or unreachable.",
                "status_code": None,
            }
        )
    except Exception as e:
        return jsonify(
            {
                "success": False,
                "message": f"Connection test failed: {str(e)}",
                "status_code": None,
            }
        )


@admin_bp.route("/api/load-models", methods=["POST"])
@production_disabled
@admin_required
def load_models_api():
    """API endpoint to load available models from AI service with comprehensive pagination support."""
    import requests

    try:
        data = request.get_json(silent=True) or {}
        provider_id = data.get("provider_id")
        api_url = data.get("api_url")
        api_key = data.get("api_key")

        if provider_id:
            provider = db.session.get(LLMProvider, int(provider_id))
            if provider:
                api_url = api_url or provider.api_url
                if not api_key or api_key == "••••••••••••••••":
                    api_key = provider.api_key

        if not api_url:
            return jsonify({"success": False, "message": "API URL is required"})

        # If no API key provided or masked key, try to use saved key for this endpoint
        if not api_key or api_key == "••••••••••••••••":
            api_key = Config.get_api_key_for_endpoint(api_url)

        models, fetch_message = fetch_all_models_from_api(api_url, api_key or "")

        if models:
            # Save models to database
            try:
                ApiModel.update_models_for_api(api_url, models)
                logger.info(f"Saved {len(models)} models for API endpoint: {api_url}")
            except Exception as e:
                logger.warning(f"Failed to save models to database: {str(e)}")
                # Continue execution - don't fail just because we can't save to DB

            return jsonify(
                {
                    "success": True,
                    "models": models,
                    "message": fetch_message,
                }
            )
        else:
            return jsonify(
                {
                    "success": False,
                    "message": "No models found - API may not support model listing",
                }
            )

    except requests.exceptions.ConnectionError:
        # Fallback to cached models
        cached_models = ApiModel.get_models_for_api(api_url)
        if cached_models:
            model_names = [model.model_name for model in cached_models]
            return jsonify(
                {
                    "success": True,
                    "models": model_names,
                    "message": f"Using {len(model_names)} cached models - API connection failed",
                    "cached": True,
                }
            )
        return jsonify(
            {
                "success": False,
                "message": "Cannot connect to AI service and no cached models available",
            }
        )
    except requests.exceptions.Timeout:
        # Fallback to cached models
        cached_models = ApiModel.get_models_for_api(api_url)
        if cached_models:
            model_names = [model.model_name for model in cached_models]
            return jsonify(
                {
                    "success": True,
                    "models": model_names,
                    "message": f"Using {len(model_names)} cached models - API connection timed out",
                    "cached": True,
                }
            )
        return jsonify(
            {
                "success": False,
                "message": "Connection timeout and no cached models available",
            }
        )
    except Exception as e:
        # Fallback to cached models
        cached_models = ApiModel.get_models_for_api(api_url)
        if cached_models:
            model_names = [model.model_name for model in cached_models]
            return jsonify(
                {
                    "success": True,
                    "models": model_names,
                    "message": f"Using {len(model_names)} cached models - Error: {str(e)}",
                    "cached": True,
                }
            )
        return jsonify({"success": False, "message": f"Error loading models: {str(e)}"})


@admin_bp.route("/api/models/<api_url_hash>", methods=["GET"])
@production_disabled
@admin_required
def get_cached_models_api(api_url_hash):
    """API endpoint to get cached models for a specific API endpoint."""
    try:
        # Decode the base64 encoded API URL
        api_url = base64.b64decode(api_url_hash.encode()).decode("utf-8")

        # Get cached models
        cached_models = ApiModel.get_models_for_api(api_url)
        model_names = [model.model_name for model in cached_models]

        if cached_models:
            last_fetched = (
                max(model.last_fetched for model in cached_models)
                if cached_models
                else None
            )
            return jsonify(
                {
                    "success": True,
                    "models": model_names,
                    "cached": True,
                    "last_fetched": last_fetched.isoformat() if last_fetched else None,
                    "count": len(model_names),
                }
            )
        else:
            return jsonify(
                {
                    "success": True,
                    "models": [],
                    "cached": True,
                    "message": "No cached models found for this API endpoint",
                }
            )

    except Exception as e:
        logger.error(f"Error getting cached models: {str(e)}")
        return jsonify(
            {"success": False, "message": f"Error retrieving cached models: {str(e)}"}
        )


@admin_bp.route("/api/endpoint-config/<api_url_hash>", methods=["GET"])
@production_disabled
@admin_required
def get_endpoint_config_api(api_url_hash):
    """Get configuration for a specific API endpoint including default model and cached models."""
    try:
        # Decode the base64 encoded API URL
        api_url = base64.b64decode(api_url_hash.encode()).decode("utf-8")

        # Get default model for this endpoint
        default_model = ApiEndpointConfig.get_default_model_for_endpoint(api_url)

        # Get cached models for this endpoint
        cached_models = ApiModel.get_models_for_api(api_url)
        model_names = [model.model_name for model in cached_models]

        last_fetched = None
        if cached_models:
            last_fetched = max(model.last_fetched for model in cached_models)

        return jsonify(
            {
                "success": True,
                "api_url": api_url,
                "default_model": default_model,
                "models": model_names,
                "last_fetched": last_fetched.isoformat() if last_fetched else None,
                "model_count": len(model_names),
            }
        )

    except Exception as e:
        logger.error(f"Error getting endpoint config: {str(e)}")
        return jsonify(
            {
                "success": False,
                "message": f"Error retrieving endpoint configuration: {str(e)}",
            }
        )


@admin_bp.route("/api/endpoint-config", methods=["POST"])
@production_disabled
@admin_required
def save_endpoint_default_model_api():
    """Save the default model for a specific API endpoint."""
    try:
        data = request.get_json()
        api_url = data.get("api_url")
        default_model = data.get("default_model")

        if not api_url:
            return jsonify({"success": False, "message": "API URL is required"})

        if not default_model:
            return jsonify({"success": False, "message": "Default model is required"})

        # Save the default model for this endpoint
        config = ApiEndpointConfig.set_default_model_for_endpoint(
            api_url, default_model
        )

        logger.info(f"Set default model '{default_model}' for API endpoint: {api_url}")

        return jsonify(
            {
                "success": True,
                "message": f"Default model '{default_model}' saved for this endpoint",
                "config": config.to_dict(),
            }
        )

    except Exception as e:
        logger.error(f"Error saving endpoint default model: {str(e)}")
        return jsonify(
            {"success": False, "message": f"Error saving default model: {str(e)}"}
        )


@admin_bp.route("/api/get-endpoint-key", methods=["POST"])
@production_disabled
@admin_required
def get_endpoint_key_api():
    """API endpoint to get the API key for a specific endpoint."""
    try:
        data = request.get_json()
        endpoint_url = data.get("endpoint_url")

        if not endpoint_url:
            return jsonify({"success": False, "message": "Endpoint URL is required"})

        api_key = Config.get_api_key_for_endpoint(endpoint_url)

        # Never echo the secret itself; only its presence and last 4 chars.
        return jsonify(
            {
                "success": True,
                "has_key": bool(api_key),
                "last4": api_key[-4:] if api_key else None,
            }
        )

    except Exception as e:
        return jsonify(
            {"success": False, "message": f"Error getting endpoint key: {str(e)}"}
        )


# --- LLM Providers Management Endpoints ---


@admin_bp.route("/api/providers", methods=["GET"])
@production_disabled
@admin_required
def api_list_providers():
    """List all saved LLM providers with cached model counts and details."""
    providers = LLMProvider.query.order_by(
        LLMProvider.is_default.desc(), LLMProvider.id.asc()
    ).all()
    result = []
    for p in providers:
        cached_models = ApiModel.get_models_for_api(p.api_url)
        model_names = [m.model_name for m in cached_models]
        last_fetched = (
            max(m.last_fetched for m in cached_models) if cached_models else None
        )
        p_dict = p.to_dict()
        p_dict["models"] = model_names
        p_dict["model_count"] = len(model_names)
        p_dict["last_fetched"] = last_fetched.isoformat() if last_fetched else None
        result.append(p_dict)
    return jsonify({"success": True, "providers": result})


@admin_bp.route("/api/providers", methods=["POST"])
@production_disabled
@admin_required
def api_create_provider():
    """Create a new LLM provider."""
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    api_url = str(payload.get("api_url") or "").strip().rstrip("/")
    api_key = str(payload.get("api_key") or "").strip()
    default_model = str(payload.get("default_model") or "").strip() or None
    is_default = bool(payload.get("is_default", False))

    if not name:
        return (
            jsonify({"success": False, "error": "Provider name is required"}),
            400,
        )
    if not api_url:
        return (
            jsonify({"success": False, "error": "API endpoint URL is required"}),
            400,
        )

    count = LLMProvider.query.count()
    if count == 0:
        is_default = True
    elif is_default:
        LLMProvider.query.update({"is_default": False})

    provider = LLMProvider(
        name=name,
        api_url=api_url,
        api_key=api_key if api_key else None,
        default_model=default_model,
        is_default=is_default,
    )
    db.session.add(provider)
    db.session.commit()

    # Attempt initial model fetch
    try:
        models, _ = fetch_all_models_from_api(api_url, api_key or "")
        if models:
            ApiModel.update_models_for_api(api_url, models)
            if not provider.default_model:
                provider.default_model = models[0]
                db.session.commit()
    except Exception as exc:
        logger.debug(f"Initial model fetch for provider {name} failed: {exc}")

    p_dict = provider.to_dict()
    cached_models = ApiModel.get_models_for_api(provider.api_url)
    p_dict["models"] = [m.model_name for m in cached_models]
    p_dict["model_count"] = len(p_dict["models"])
    return jsonify({"success": True, "provider": p_dict}), 201


@admin_bp.route("/api/providers/<int:provider_id>", methods=["GET"])
@production_disabled
@admin_required
def api_get_provider(provider_id):
    """Get single provider details including cached models."""
    provider = db.session.get(LLMProvider, provider_id)
    if not provider:
        return jsonify({"success": False, "error": "Provider not found"}), 404

    cached_models = ApiModel.get_models_for_api(provider.api_url)
    p_dict = provider.to_dict()
    p_dict["models"] = [m.model_name for m in cached_models]
    p_dict["model_count"] = len(p_dict["models"])
    return jsonify({"success": True, "provider": p_dict})


@admin_bp.route("/api/providers/<int:provider_id>", methods=["PUT", "POST"])
@admin_bp.route("/api/providers/<int:provider_id>/update", methods=["POST", "PUT"])
@production_disabled
@admin_required
def api_update_provider(provider_id):
    """Update an existing provider."""
    provider = db.session.get(LLMProvider, provider_id)
    if not provider:
        return jsonify({"success": False, "error": "Provider not found"}), 404

    payload = request.get_json(silent=True) or {}
    if "name" in payload:
        name = str(payload.get("name") or "").strip()
        if not name:
            return (
                jsonify({"success": False, "error": "Provider name cannot be empty"}),
                400,
            )
        provider.name = name

    if "api_url" in payload:
        api_url = str(payload.get("api_url") or "").strip().rstrip("/")
        if not api_url:
            return (
                jsonify({"success": False, "error": "API URL cannot be empty"}),
                400,
            )
        provider.api_url = api_url

    if "api_key" in payload:
        key = str(payload.get("api_key") or "").strip()
        if key != "" and key != "••••••••••••••••":
            provider.api_key = key
        elif payload.get("clear_api_key"):
            provider.api_key = None

    if "default_model" in payload:
        model_val = str(payload.get("default_model") or "").strip()
        provider.default_model = model_val or None

    if "is_default" in payload:
        new_default = bool(payload.get("is_default"))
        if new_default:
            for p in LLMProvider.query.all():
                p.is_default = p.id == provider.id
        else:
            if LLMProvider.query.count() == 1:
                provider.is_default = True
            else:
                provider.is_default = False
                if not LLMProvider.query.filter(
                    LLMProvider.id != provider.id, LLMProvider.is_default.is_(True)
                ).first():
                    other = LLMProvider.query.filter(
                        LLMProvider.id != provider.id
                    ).first()
                    if other:
                        other.is_default = True

    provider.updated_at = datetime.utcnow()
    db.session.commit()

    cached_models = ApiModel.get_models_for_api(provider.api_url)
    p_dict = provider.to_dict()
    p_dict["models"] = [m.model_name for m in cached_models]
    p_dict["model_count"] = len(p_dict["models"])
    return jsonify({"success": True, "provider": p_dict})


@admin_bp.route("/api/providers/<int:provider_id>", methods=["DELETE"])
@admin_bp.route("/api/providers/<int:provider_id>/delete", methods=["POST", "DELETE"])
@production_disabled
@admin_required
def api_delete_provider(provider_id):
    """Delete a provider. If default, makes another provider default."""
    provider = db.session.get(LLMProvider, provider_id)
    if not provider:
        return jsonify({"success": False, "error": "Provider not found"}), 404

    was_default = provider.is_default
    db.session.delete(provider)
    db.session.commit()

    if was_default:
        remaining = LLMProvider.query.order_by(LLMProvider.id.asc()).first()
        if remaining:
            remaining.is_default = True
            db.session.commit()

    return jsonify({"success": True, "message": "Provider deleted successfully"})


@admin_bp.route("/api/providers/<int:provider_id>/set-default", methods=["POST"])
@production_disabled
@admin_required
def api_set_default_provider(provider_id):
    """Set specified provider as default."""
    provider = db.session.get(LLMProvider, provider_id)
    if not provider:
        return jsonify({"success": False, "error": "Provider not found"}), 404

    LLMProvider.set_default(provider.id)
    return jsonify({"success": True, "provider": provider.to_dict()})


@admin_bp.route("/api/providers/<int:provider_id>/refresh-models", methods=["POST"])
@production_disabled
@admin_required
def api_refresh_provider_models(provider_id):
    """Fetch live models for a provider using its endpoint and stored API key."""
    provider = db.session.get(LLMProvider, provider_id)
    if not provider:
        return jsonify({"success": False, "error": "Provider not found"}), 404

    api_key = (
        provider.api_key or Config.get_api_key_for_endpoint(provider.api_url) or ""
    )
    models, fetch_message = fetch_all_models_from_api(provider.api_url, api_key)

    if models:
        try:
            ApiModel.update_models_for_api(provider.api_url, models)
        except Exception as exc:
            logger.warning("Failed to save models to database: %s", exc)

        if not provider.default_model and models:
            provider.default_model = models[0]
            db.session.commit()

        return jsonify(
            {
                "success": True,
                "models": models,
                "message": fetch_message,
                "default_model": provider.default_model,
                "count": len(models),
            }
        )
    else:
        cached_models = ApiModel.get_models_for_api(provider.api_url)
        if cached_models:
            model_names = [m.model_name for m in cached_models]
            return jsonify(
                {
                    "success": True,
                    "models": model_names,
                    "message": f"API did not return models. Using {len(model_names)} cached models.",
                    "cached": True,
                    "default_model": provider.default_model,
                    "count": len(model_names),
                }
            )
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Failed to fetch models from API: " + fetch_message,
                }
            ),
            400,
        )


@admin_bp.route("/api/providers/<int:provider_id>/models", methods=["GET"])
@production_disabled
@admin_required
def api_get_provider_models(provider_id):
    """Get active models for a provider."""
    provider = db.session.get(LLMProvider, provider_id)
    if not provider:
        return jsonify({"success": False, "error": "Provider not found"}), 404

    cached_models = ApiModel.get_models_for_api(provider.api_url)
    model_names = [m.model_name for m in cached_models]
    last_fetched = max(m.last_fetched for m in cached_models) if cached_models else None

    if not model_names and request.args.get("refresh") == "true":
        api_key = (
            provider.api_key or Config.get_api_key_for_endpoint(provider.api_url) or ""
        )
        models, _ = fetch_all_models_from_api(provider.api_url, api_key)
        if models:
            ApiModel.update_models_for_api(provider.api_url, models)
            model_names = models
            last_fetched = datetime.utcnow()
            if not provider.default_model:
                provider.default_model = models[0]
                db.session.commit()

    return jsonify(
        {
            "success": True,
            "models": model_names,
            "default_model": provider.default_model,
            "last_fetched": last_fetched.isoformat() if last_fetched else None,
            "count": len(model_names),
        }
    )


@admin_bp.route("/api/clear-jobs", methods=["POST"])
@production_disabled
@admin_required
def clear_jobs_api():
    """API endpoint to clear all jobs history."""
    try:
        # Get count of jobs before deletion for reporting
        job_count = Job.query.count()

        # Delete all jobs
        db.session.query(Job).delete()
        db.session.commit()

        logger.info(f"Cleared {job_count} jobs from history")

        return jsonify(
            {
                "success": True,
                "message": f"Successfully cleared {job_count} jobs from history",
            }
        )

    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to clear jobs history: {e}")
        return jsonify(
            {"success": False, "message": f"Failed to clear jobs history: {str(e)}"}
        )


@admin_bp.route("/api/load-default-data", methods=["POST"])
@production_disabled
@admin_required
def load_default_data_api():
    """API endpoint to load default subdeaddits and users from JSON files."""
    import json
    import os

    try:
        # Get paths to the data files
        data_dir = os.path.join(os.path.dirname(__file__), "data")
        subdeaddits_file = os.path.join(data_dir, "subdeaddits_base.json")
        users_file = os.path.join(data_dir, "users.json")

        subdeaddits_loaded = 0
        users_loaded = 0

        # Load subdeaddits
        if os.path.exists(subdeaddits_file):
            with open(subdeaddits_file) as f:
                subdeaddits_data = json.load(f)

            for subdeaddit_data in subdeaddits_data.get("subdeaddits", []):
                # Skip entries that already exist
                existing = Subdeaddit.query.filter_by(
                    name=subdeaddit_data["name"]
                ).first()
                if existing:
                    continue
                try:
                    create_subdeaddit(
                        name=subdeaddit_data["name"],
                        description=subdeaddit_data["description"],
                        post_types=subdeaddit_data.get("post_types", []),
                        update_if_exists=False,
                    )
                    subdeaddits_loaded += 1
                except (ContentValidationError, SQLAlchemyError) as exc:
                    logger.warning(
                        "Skipping subdeaddit %r during default data load: %s",
                        subdeaddit_data["name"],
                        exc,
                    )

            logger.info(f"Loaded {subdeaddits_loaded} new subdeaddits")

        # Load users (limit to first 50 to avoid overwhelming the system)
        if os.path.exists(users_file):
            with open(users_file) as f:
                users_data = json.load(f)

            for user_data in users_data.get("users", [])[:50]:
                # Skip entries that already exist
                existing = User.query.filter_by(username=user_data["username"]).first()
                if existing:
                    continue
                try:
                    create_user(
                        username=user_data["username"],
                        bio=user_data.get("bio", ""),
                        age=user_data.get("age"),
                        gender=user_data.get("gender", "Male"),
                        education=user_data.get("education", ""),
                        occupation=user_data.get("occupation", ""),
                        interests=user_data.get("interests", []),
                        personality_traits=user_data.get("personality_traits", []),
                        writing_style=user_data.get("writing_style", ""),
                        model=user_data.get("model", "default"),
                    )
                    users_loaded += 1
                except SQLAlchemyError as exc:
                    logger.warning(
                        "Skipping user %r during default data load: %s",
                        user_data["username"],
                        exc,
                    )

            logger.info(f"Loaded {users_loaded} new users")

        # Mark default data as loaded
        Config.set("DEFAULT_DATA_LOADED", "true")

        return jsonify(
            {
                "success": True,
                "message": f"Successfully loaded {subdeaddits_loaded} subdeaddits and {users_loaded} users",
                "subdeaddits_loaded": subdeaddits_loaded,
                "users_loaded": users_loaded,
            }
        )

    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to load default data: {e}")
        return jsonify(
            {"success": False, "message": f"Failed to load default data: {str(e)}"}
        )


@admin_bp.route("/api/hide-default-data", methods=["POST"])
@production_disabled
@admin_required
def hide_default_data_api():
    """API endpoint to hide the default data section permanently."""
    try:
        # Mark default data as loaded to hide the section
        Config.set("DEFAULT_DATA_LOADED", "true")

        return jsonify(
            {"success": True, "message": "Default data section will no longer be shown"}
        )

    except Exception as e:
        logger.error(f"Failed to hide default data section: {e}")
        return jsonify(
            {
                "success": False,
                "message": f"Failed to hide default data section: {str(e)}",
            }
        )


# --- LLM usage accounting & routing JSON API (Phase LLM-3) ---


@admin_bp.route("/api/usage/summary")
@production_disabled
@admin_required
def usage_summary_api():
    """Aggregate LLM usage accounting (totals, by day, by action)."""
    totals_row = db.session.query(
        func.count(LLMUsage.id),
        func.coalesce(func.sum(LLMUsage.prompt_tokens), 0),
        func.coalesce(func.sum(LLMUsage.completion_tokens), 0),
        func.coalesce(func.sum(LLMUsage.total_tokens), 0),
        # NULL-safe: rows with unknown price contribute nothing, and an
        # all-unpriced ledger stays NULL instead of faking $0.
        func.sum(LLMUsage.estimated_cost),
    ).one()

    day_expr = func.date(LLMUsage.created_at)
    action_expr = LLMUsage.action
    cost_sum = func.sum(LLMUsage.estimated_cost)
    tokens_sum = func.coalesce(func.sum(LLMUsage.total_tokens), 0)

    by_day = (
        db.session.query(
            day_expr.label("day"), tokens_sum.label("tokens"), cost_sum.label("cost")
        )
        .group_by(day_expr)
        .order_by(day_expr)
        .all()
    )
    by_action = (
        db.session.query(
            action_expr.label("action"),
            func.count(LLMUsage.id).label("rows"),
            tokens_sum.label("tokens"),
            cost_sum.label("cost"),
        )
        .group_by(action_expr)
        .order_by(action_expr)
        .all()
    )

    return jsonify(
        {
            "totals": {
                "rows": totals_row[0],
                "prompt_tokens": totals_row[1],
                "completion_tokens": totals_row[2],
                "total_tokens": totals_row[3],
                "estimated_cost_sum": totals_row[4],
            },
            "by_day": [
                {"day": row.day, "tokens": row.tokens, "cost": row.cost}
                for row in by_day
            ],
            "by_action": [
                {
                    "action": row.action,
                    "rows": row.rows,
                    "tokens": row.tokens,
                    "cost": row.cost,
                }
                for row in by_action
            ],
        }
    )


@admin_bp.route("/api/routes")
@production_disabled
@admin_required
def routes_api():
    """List model routing rows plus the currently resolved default."""
    routes = ModelRoute.query.order_by(
        ModelRoute.tier.asc(), ModelRoute.priority.desc(), ModelRoute.id.desc()
    ).all()

    resolved_api_url, resolved_model = routing.resolve()

    return jsonify(
        {
            "routes": [
                {
                    "id": route.id,
                    "tier": route.tier,
                    "api_url": route.api_url,
                    "model_name": route.model_name,
                    "priority": route.priority,
                    "is_active": route.is_active,
                    "updated_at": (
                        route.updated_at.isoformat() if route.updated_at else None
                    ),
                }
                for route in routes
            ],
            "resolved_default": {
                "api_url": resolved_api_url,
                "model_name": resolved_model,
            },
        }
    )


# --- AgenticCore: agent administration ---
# JSON API + pages for the autonomous-agent runtime (Phase 2). All routes are
# admin-gated like the rest of this blueprint; scheduling itself lives in the
# worker process (runtime.scheduler), never here.

_AGENTIC_TIERS = ("lurker", "regular", "power_user")


def _agent_json(agent, counts=None):
    """Serialize an Agent row; ``counts`` maps run status -> count."""
    from deaddit.models import AgentRun

    if counts is None:
        rows = (
            db.session.query(AgentRun.status, func.count(AgentRun.id))
            .filter(AgentRun.agent_id == agent.id)
            .group_by(AgentRun.status)
            .all()
        )
        counts = dict(rows)
    return {
        "id": agent.id,
        "user_username": agent.user_username,
        "autonomy_tier": agent.autonomy_tier,
        "is_enabled": bool(agent.is_enabled),
        "status": agent.status,
        "config": agent.config or {},
        "state": agent.state or {},
        "last_run_at": agent.last_run_at.isoformat() if agent.last_run_at else None,
        "next_run_at": agent.next_run_at.isoformat() if agent.next_run_at else None,
        "consecutive_failures": int(agent.consecutive_failures or 0),
        "runs_completed": int(counts.get("completed", 0)),
        "runs_failed": int(counts.get("failed", 0)),
        "runs_interrupted": int(counts.get("interrupted", 0)),
        "runs_total": int(sum(counts.values())),
    }


def _run_json(run):
    return {
        "id": run.id,
        "agent_id": run.agent_id,
        "trigger": run.trigger,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "turn_count": run.turn_count,
        "action_count": run.action_count,
        "token_usage": run.token_usage or {},
        "error_message": run.error_message,
    }


def _truncate(text, limit=80):
    """First ``limit`` chars of text with an ellipsis when cut."""
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _as_int(value):
    """Tolerant int coercion for JSON-sourced ids; None when not coercible."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _tool_content_card(result):
    """Resolve a ToolCall.result blob to a produced-content card, or None.

    Cards link to the post/comment a successful create_post/create_comment
    call produced; hard-deleted rows, non-dict results, and preview-wrapper
    results ({'truncated': ..., 'preview': ...}) all resolve to None.
    Removed content renders as plain text: href is None and the label gains a
    " (removed)" suffix.
    """
    from deaddit.models import Comment, Post

    if not isinstance(result, dict):
        return None
    post_id = _as_int(result.get("post_id"))
    comment_id = _as_int(result.get("comment_id"))
    if not post_id and not comment_id:
        return None
    try:
        if post_id:
            post = db.session.get(Post, post_id)
            if post is None:
                return None
            label = _truncate(post.title)
            href = url_for(
                "web.post", subdeaddit_name=post.subdeaddit_name, post_id=post.id
            )
            removed = bool(post.removed)
            if removed:
                href = None
                label += " (removed)"
            return {"kind": "post", "href": href, "label": label, "removed": removed}
        comment = db.session.get(Comment, comment_id)
        if comment is None:
            return None
        post = db.session.get(Post, comment.post_id)
        if post is None:
            return None
        label = _truncate(comment.content)
        removed = bool(comment.removed or post.removed)
        href = None
        if removed:
            label += " (removed)"
        else:
            href = (
                url_for(
                    "web.post",
                    subdeaddit_name=post.subdeaddit_name,
                    post_id=post.id,
                )
                + f"#comment-{comment.id}"
            )
        return {"kind": "comment", "href": href, "label": label, "removed": removed}
    except SQLAlchemyError:
        # Malformed id types or vanished rows must never break serialization.
        return None


@admin_bp.route("/api/agents")
@production_disabled
@admin_required
def api_agents_list():
    """List every registered agent with run tallies."""
    from deaddit.models import Agent, AgentRun

    agents = Agent.query.order_by(Agent.user_username).all()
    tally_rows = (
        db.session.query(AgentRun.agent_id, AgentRun.status, func.count(AgentRun.id))
        .group_by(AgentRun.agent_id, AgentRun.status)
        .all()
    )
    tallies = {}
    for agent_id, status, count in tally_rows:
        tallies.setdefault(agent_id, {})[status] = count
    return jsonify(
        {"agents": [_agent_json(agent, tallies.get(agent.id, {})) for agent in agents]}
    )


@admin_bp.route("/api/personas/candidates")
@production_disabled
@admin_required
def api_persona_candidates():
    """Users that are not yet agents, ranked by activity (posts + comments)."""
    from deaddit.models import Agent

    posts_sq = (
        db.session.query(Post.user.label("username"), func.count(Post.id).label("n"))
        .group_by(Post.user)
        .subquery()
    )
    comments_sq = (
        db.session.query(
            Comment.user.label("username"), func.count(Comment.id).label("n")
        )
        .group_by(Comment.user)
        .subquery()
    )
    taken = [username for (username,) in db.session.query(Agent.user_username).all()]
    activity = func.coalesce(posts_sq.c.n, 0) + func.coalesce(comments_sq.c.n, 0)
    rows = (
        db.session.query(User, posts_sq.c.n, comments_sq.c.n)
        .outerjoin(posts_sq, User.username == posts_sq.c.username)
        .outerjoin(comments_sq, User.username == comments_sq.c.username)
    )
    if taken:
        rows = rows.filter(~User.username.in_(taken))
    candidates = []
    for user, post_count, comment_count in (
        rows.order_by(desc(activity), User.username).limit(50).all()
    ):
        bio = (user.bio or "").strip()
        traits = (user.personality_traits or "").strip()
        preview_source = bio or traits
        candidates.append(
            {
                "username": user.username,
                "personality_preview": (
                    preview_source[:120] + "…"
                    if len(preview_source) > 120
                    else preview_source
                ),
                "post_count": int(post_count or 0),
                "comment_count": int(comment_count or 0),
            }
        )
    return jsonify({"candidates": candidates})


@admin_bp.route("/api/agents/presets")
@production_disabled
@admin_required
def api_agent_presets():
    """Named presets powering the create-agent form."""
    return jsonify(
        {
            "tiers": list(_AGENTIC_TIERS),
            "cadence": {
                "slow": [1800, 7200],
                "normal": [900, 3600],
                "active": [300, 1500],
            },
            "daily_request_ceiling": {"light": 200, "standard": 2000, "heavy": 8000},
            "cohort_size": {"small": 5, "medium": 12, "large": 20},
        }
    )


@admin_bp.route("/api/agents", methods=["POST"])
@production_disabled
@admin_required
def api_create_agent():
    """Create an agent from an existing user persona (created disabled by default)."""
    from deaddit.agents.loop import DEFAULT_CONFIG
    from deaddit.llm.capabilities import CapabilityError, ensure_tools_allowed
    from deaddit.models import Agent

    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    if not username:
        return jsonify({"success": False, "error": "username is required"}), 400
    if db.session.get(User, username) is None:
        return (
            jsonify({"success": False, "error": f"User '{username}' does not exist"}),
            400,
        )
    if Agent.query.filter_by(user_username=username).first() is not None:
        return (
            jsonify({"success": False, "error": f"'{username}' already has an agent"}),
            409,
        )

    tier = payload.get("autonomy_tier") or "regular"
    if tier not in _AGENTIC_TIERS:
        return jsonify({"success": False, "error": f"Unknown tier '{tier}'"}), 400

    provider_id = payload.get("provider_id")
    provider = None
    if provider_id:
        try:
            provider = db.session.get(LLMProvider, int(provider_id))
        except Exception:
            provider = None

    default_p = LLMProvider.get_default()
    if provider:
        api_url = str(payload.get("api_url") or provider.api_url or "").strip()
        model = str(payload.get("model") or provider.default_model or "llama3").strip()
        api_key = provider.api_key or Config.get_api_key_for_endpoint(api_url)
    elif payload.get("api_url"):
        api_url = str(payload.get("api_url") or "").strip()
        model = str(
            payload.get("model") or Config.get("OPENAI_MODEL", "llama3")
        ).strip()
        matching_p = LLMProvider.query.filter(
            (LLMProvider.api_url == api_url.rstrip("/"))
            | (LLMProvider.api_url == api_url)
        ).first()
        if matching_p:
            provider = matching_p
            api_key = matching_p.api_key or Config.get_api_key_for_endpoint(api_url)
        else:
            api_key = Config.get_api_key_for_endpoint(api_url)
    elif default_p:
        provider = default_p
        api_url = str(default_p.api_url or "").strip()
        model = str(
            payload.get("model")
            or default_p.default_model
            or Config.get("OPENAI_MODEL", "llama3")
        ).strip()
        api_key = default_p.api_key or Config.get_api_key_for_endpoint(api_url)
    else:
        api_url = str(Config.get("OPENAI_API_URL") or "").strip()
        model = str(
            payload.get("model") or Config.get("OPENAI_MODEL", "llama3")
        ).strip()
        api_key = Config.get_api_key_for_endpoint(api_url)

    try:
        min_delay = int(payload.get("min_delay", DEFAULT_CONFIG["min_delay"]))
        max_delay = int(payload.get("max_delay", DEFAULT_CONFIG["max_delay"]))
    except (TypeError, ValueError):
        return jsonify(
            {"success": False, "error": "min/max delay must be integers"}
        ), 400
    if min_delay < 0 or max_delay < min_delay:
        return (
            jsonify({"success": False, "error": "max_delay must be >= min_delay >= 0"}),
            400,
        )

    config = {
        "provider_id": provider.id if provider else None,
        "api_url": api_url,
        "model": model,
        "min_delay": min_delay,
        "max_delay": max_delay,
        "max_actions_per_run": DEFAULT_CONFIG["max_actions_per_run"],
        "max_run_seconds": DEFAULT_CONFIG["max_run_seconds"],
    }
    daily_ceiling = payload.get("daily_request_ceiling")
    if daily_ceiling is not None:
        try:
            config["daily_request_ceiling"] = int(daily_ceiling)
        except (TypeError, ValueError):
            return (
                jsonify(
                    {"success": False, "error": "daily_request_ceiling must be an int"}
                ),
                400,
            )

    # Owner decision 2: probe at cohort creation so a tool-less endpoint/model
    # is rejected before any agent exists.
    try:
        ensure_tools_allowed(api_url, model, api_key=api_key, auto_probe=True)
    except CapabilityError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    # Owner decision 1: nothing runs by default - enable is opt-in.
    enable = bool(payload.get("enable", False))
    agent = Agent(
        user_username=username,
        autonomy_tier=tier,
        is_enabled=enable,
        status="idle",
        config=config,
        state={},
        consecutive_failures=0,
        next_run_at=datetime.utcnow() if enable else None,
    )
    db.session.add(agent)
    db.session.commit()

    episodes = 0
    warning = None
    if payload.get("backfill_memory", True):
        try:
            from deaddit.agents.memory import backfill_persona_history
        except ImportError as exc:
            warning = f"backfill unavailable: {exc}"
        else:
            try:
                episodes = int(
                    backfill_persona_history(username, api_url=api_url, model=model)
                )
            except Exception as exc:
                logger.warning("Backfill failed for '%s': %s", username, exc)
                warning = f"backfill failed: {exc}"

    result = {"agent": _agent_json(agent), "episodes": episodes}
    if warning:
        result["warning"] = warning
    return jsonify(result), 201


@admin_bp.route("/api/agents/<int:agent_id>", methods=["PUT", "POST"])
@admin_bp.route("/api/agents/<int:agent_id>/update", methods=["POST", "PUT"])
@production_disabled
@admin_required
def api_update_agent(agent_id):
    """Update an agent's tier, enabled status, cadence, limits, model, or status reset."""
    from sqlalchemy.orm.attributes import flag_modified

    from deaddit.agents.loop import DEFAULT_CONFIG
    from deaddit.llm.capabilities import CapabilityError, ensure_tools_allowed
    from deaddit.models import Agent

    agent = db.session.get(Agent, agent_id)
    if agent is None:
        return jsonify({"success": False, "error": "agent not found"}), 404

    payload = request.get_json(silent=True) or {}

    # 1. Autonomy Tier
    if "autonomy_tier" in payload:
        tier = payload.get("autonomy_tier")
        if tier not in _AGENTIC_TIERS:
            return jsonify({"success": False, "error": f"Unknown tier '{tier}'"}), 400
        agent.autonomy_tier = tier

    # 2. is_enabled toggle
    if "is_enabled" in payload:
        new_enabled = bool(payload["is_enabled"])
        if new_enabled != agent.is_enabled:
            agent.is_enabled = new_enabled
            if new_enabled:
                agent.consecutive_failures = 0
                agent.next_run_at = datetime.utcnow()
            else:
                agent.next_run_at = None
                if agent.status != "running":
                    agent.status = "idle"

    # 3. Config fields (support both flat keys and nested {"config": {...}})
    cfg_in = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    config = dict(agent.config or {})

    # min_delay & max_delay
    min_delay_val = payload.get("min_delay", cfg_in.get("min_delay"))
    max_delay_val = payload.get("max_delay", cfg_in.get("max_delay"))
    if min_delay_val is not None or max_delay_val is not None:
        curr_min = config.get("min_delay", DEFAULT_CONFIG["min_delay"])
        curr_max = config.get("max_delay", DEFAULT_CONFIG["max_delay"])
        val_min = min_delay_val if min_delay_val is not None else curr_min
        val_max = max_delay_val if max_delay_val is not None else curr_max
        try:
            val_min = int(val_min)
            val_max = int(val_max)
        except (TypeError, ValueError):
            return (
                jsonify({"success": False, "error": "min/max delay must be integers"}),
                400,
            )
        if val_min < 0 or val_max < val_min:
            return (
                jsonify(
                    {"success": False, "error": "max_delay must be >= min_delay >= 0"}
                ),
                400,
            )
        config["min_delay"] = val_min
        config["max_delay"] = val_max

    # max_actions_per_run
    actions_val = payload.get("max_actions_per_run", cfg_in.get("max_actions_per_run"))
    if actions_val is not None:
        try:
            actions_val = int(actions_val)
        except (TypeError, ValueError):
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "max_actions_per_run must be an integer",
                    }
                ),
                400,
            )
        if actions_val <= 0:
            return (
                jsonify({"success": False, "error": "max_actions_per_run must be > 0"}),
                400,
            )
        config["max_actions_per_run"] = actions_val

    # max_run_seconds
    run_sec_val = payload.get("max_run_seconds", cfg_in.get("max_run_seconds"))
    if run_sec_val is not None:
        try:
            run_sec_val = int(run_sec_val)
        except (TypeError, ValueError):
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "max_run_seconds must be an integer",
                    }
                ),
                400,
            )
        if run_sec_val <= 0:
            return (
                jsonify({"success": False, "error": "max_run_seconds must be > 0"}),
                400,
            )
        config["max_run_seconds"] = run_sec_val

    # daily_request_ceiling
    if "daily_request_ceiling" in payload or "daily_request_ceiling" in cfg_in:
        ceiling_val = (
            payload["daily_request_ceiling"]
            if "daily_request_ceiling" in payload
            else cfg_in.get("daily_request_ceiling")
        )
        if ceiling_val is None or ceiling_val == "" or ceiling_val is False:
            config.pop("daily_request_ceiling", None)
        else:
            try:
                ceiling_val = int(ceiling_val)
            except (TypeError, ValueError):
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "daily_request_ceiling must be an int",
                        }
                    ),
                    400,
                )
            if ceiling_val <= 0:
                return (
                    jsonify(
                        {"success": False, "error": "daily_request_ceiling must be > 0"}
                    ),
                    400,
                )
            config["daily_request_ceiling"] = ceiling_val

    # provider_id
    if "provider_id" in payload or "provider_id" in cfg_in:
        pid = (
            payload["provider_id"]
            if "provider_id" in payload
            else cfg_in.get("provider_id")
        )
        if pid:
            try:
                provider = db.session.get(LLMProvider, int(pid))
                if provider:
                    config["provider_id"] = provider.id
                    if not payload.get("api_url") and not cfg_in.get("api_url"):
                        config["api_url"] = provider.api_url
            except Exception:
                pass
        else:
            config.pop("provider_id", None)

    # api_url
    if "api_url" in payload or "api_url" in cfg_in:
        url_val = payload["api_url"] if "api_url" in payload else cfg_in.get("api_url")
        config["api_url"] = str(url_val or "").strip()

    # model
    if "model" in payload or "model" in cfg_in:
        model_val = payload["model"] if "model" in payload else cfg_in.get("model")
        config["model"] = str(model_val or "").strip()

    # Capability check if model or api_url or provider changed/provided
    if (
        "api_url" in payload
        or "api_url" in cfg_in
        or "model" in payload
        or "model" in cfg_in
        or "provider_id" in payload
        or "provider_id" in cfg_in
    ):
        check_api_url = config.get("api_url") or Config.get("OPENAI_API_URL") or ""
        check_model = config.get("model") or Config.get("OPENAI_MODEL", "llama3")
        check_key = Config.get_api_key_for_endpoint(check_api_url)
        try:
            ensure_tools_allowed(
                check_api_url, check_model, api_key=check_key, auto_probe=True
            )
        except CapabilityError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    agent.config = config
    flag_modified(agent, "config")

    # 4. Status reset option
    if payload.get("reset_status") or payload.get("reset_errors"):
        agent.consecutive_failures = 0
        if agent.status != "running":
            agent.status = "idle"
        if agent.is_enabled:
            agent.next_run_at = datetime.utcnow()

    db.session.commit()
    return jsonify({"success": True, "agent": _agent_json(agent)})


@admin_bp.route("/api/agents/<int:agent_id>/toggle", methods=["POST"])
@production_disabled
@admin_required
def api_toggle_agent(agent_id):
    """Enable/disable one agent. Enabling resets the failure strike count."""
    from deaddit.models import Agent

    agent = db.session.get(Agent, agent_id)
    if agent is None:
        return jsonify({"success": False, "error": "agent not found"}), 404
    if agent.is_enabled:
        agent.is_enabled = False
        agent.next_run_at = None
        agent.status = "idle"
    else:
        # Explicit human re-enable: clear strikes and wake on next poll.
        agent.is_enabled = True
        agent.consecutive_failures = 0
        agent.next_run_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"agent": _agent_json(agent)})


@admin_bp.route("/api/agents/<int:agent_id>/force-run", methods=["POST"])
@production_disabled
@admin_required
def api_force_run(agent_id):
    """Run one agent visit synchronously; bounded by its max_run_seconds budget."""
    from deaddit.agents.loop import run_once
    from deaddit.models import Agent

    agent = db.session.get(Agent, agent_id)
    if agent is None:
        return jsonify({"success": False, "error": "agent not found"}), 404
    try:
        run = run_once(agent.user_username, trigger="manual")
    except ValueError as exc:
        # Already running / no agent registered.
        return jsonify({"success": False, "error": str(exc)}), 409
    return jsonify({"run": _run_json(run)})


@admin_bp.route("/api/agents/<int:agent_id>/runs")
@production_disabled
@admin_required
def api_agent_runs(agent_id):
    """Recent runs for one agent, newest first."""
    from deaddit.models import Agent, AgentRun

    agent = db.session.get(Agent, agent_id)
    if agent is None:
        return jsonify({"success": False, "error": "agent not found"}), 404
    limit = max(1, min(request.args.get("limit", 25, type=int) or 25, 200))
    query = AgentRun.query.filter_by(agent_id=agent_id)
    before_id = request.args.get("before_id", type=int)
    if before_id is not None:
        query = query.filter(AgentRun.id < before_id)
    runs = (
        query.order_by(desc(AgentRun.started_at), desc(AgentRun.id)).limit(limit).all()
    )
    return jsonify(
        {
            "runs": [_run_json(run) for run in runs],
            # Null when the page came up short: no older page to fetch.
            "next_before_id": runs[-1].id if len(runs) == limit else None,
        }
    )


@admin_bp.route("/api/runs/<int:run_id>/turns")
@production_disabled
@admin_required
def api_run_turns(run_id):
    """Seq-ordered LLM turns with verbatim prompt chains (View Thoughts)."""
    from deaddit.models import AgentRun, AgentTurn

    if db.session.get(AgentRun, run_id) is None:
        return jsonify({"success": False, "error": "run not found"}), 404
    turns = AgentTurn.query.filter_by(run_id=run_id).order_by(AgentTurn.seq).all()
    return jsonify(
        {
            "turns": [
                {
                    "id": turn.id,
                    "seq": turn.seq,
                    "model": turn.model,
                    "latency_ms": turn.latency_ms,
                    "request_messages": turn.request_messages,
                    "response_message": turn.response_message,
                }
                for turn in turns
            ]
        }
    )


@admin_bp.route("/api/turns/<int:turn_id>/tool_calls")
@production_disabled
@admin_required
def api_turn_tool_calls(turn_id):
    """Tool invocations recorded against one turn."""
    from deaddit.models import AgentTurn, ToolCall

    if db.session.get(AgentTurn, turn_id) is None:
        return jsonify({"success": False, "error": "turn not found"}), 404
    calls = ToolCall.query.filter_by(turn_id=turn_id).order_by(ToolCall.id).all()
    return jsonify(
        {
            "tool_calls": [
                {
                    "name": call.name,
                    "arguments": call.arguments,
                    "result": call.result,
                    "ok": bool(call.ok),
                    "error": call.error,
                    "duration_ms": call.duration_ms,
                    "created_at": (
                        call.created_at.isoformat() if call.created_at else None
                    ),
                    # Produced-content link card (post/comment), or null.
                    "content": _tool_content_card(call.result),
                }
                for call in calls
            ]
        }
    )


@admin_bp.route("/api/agents/start-all", methods=["POST"])
@production_disabled
@admin_required
def api_agents_start_all():
    """Bulk enable with toggle semantics."""
    from deaddit.models import Agent

    disabled = Agent.query.filter(Agent.is_enabled.is_(False)).all()
    for agent in disabled:
        agent.is_enabled = True
        agent.consecutive_failures = 0
        agent.next_run_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"started": len(disabled)})


@admin_bp.route("/api/agents/pause-all", methods=["POST"])
@production_disabled
@admin_required
def api_agents_pause_all():
    """Bulk disable with toggle semantics."""
    from deaddit.models import Agent

    enabled = Agent.query.filter(Agent.is_enabled.is_(True)).all()
    for agent in enabled:
        agent.is_enabled = False
        agent.next_run_at = None
        agent.status = "idle"
    db.session.commit()
    return jsonify({"paused": len(enabled)})


@admin_bp.route("/api/users/generate", methods=["POST"])
@production_disabled
@admin_required
def api_generate_users():
    """Generate human-like personas and optionally enroll them as autonomous agents."""
    from deaddit.services.persona_generator import generate_personas

    payload = request.get_json(silent=True) or {}
    count_raw = payload.get("count", 1)
    try:
        count = int(count_raw)
    except (TypeError, ValueError):
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Count must be an integer between 1 and 500",
                }
            ),
            400,
        )

    if count < 1 or count > 500:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Count must be between 1 and 500",
                }
            ),
            400,
        )

    tier = payload.get("tier", "regular")
    if tier not in _AGENTIC_TIERS:
        return jsonify({"success": False, "error": f"Unknown tier '{tier}'"}), 400

    auto_create_agent = payload.get("auto_create_agent", True)
    if isinstance(auto_create_agent, str):
        auto_create_agent = auto_create_agent.lower() in ("true", "1", "yes")
    else:
        auto_create_agent = bool(auto_create_agent)

    topic_hint = payload.get("topic_hint")
    if topic_hint is not None:
        topic_hint = str(topic_hint).strip()
        if not topic_hint:
            topic_hint = None

    api_url = payload.get("api_url")
    model = payload.get("model")

    try:
        result = generate_personas(
            count=count,
            topic_hint=topic_hint,
            auto_create_agent=auto_create_agent,
            tier=tier,
            api_url=api_url,
            model=model,
        )
        return (
            jsonify(
                {
                    "success": True,
                    "users": result["users"],
                    "agents": result["agents"],
                }
            ),
            201,
        )
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("Persona generation failed: %s", exc)
        return jsonify({"success": False, "error": str(exc)}), 500


@admin_bp.route("/agents")
@production_disabled
@admin_required
def agents_dashboard():
    """AgenticCore agent administration dashboard page."""
    return render_template("admin/agents.html")


@admin_bp.route("/agents/<username>")
@production_disabled
@admin_required
def agent_detail(username):
    """Single-agent detail page with run timeline and thought drill-down."""
    return render_template("admin/agent_detail.html", username=username)


# --- Moderation: reports queue (Phase D4) ---

_REPORT_STATUSES = ("open", "actioned", "dismissed", "all")


def _report_target(report):
    """Resolve a report's target to (kind, item_or_None).

    kind is "post" or "comment"; item is None when the row was hard-deleted
    by bulk cleanup (the queue still lists the report).
    """
    if report.post_id:
        return "post", Post.query.get(report.post_id)
    if report.comment_id:
        return "comment", Comment.query.get(report.comment_id)
    return None, None


def _report_row(report):
    """View model for one queue row: links, preview snippet, author."""
    kind, item = _report_target(report)
    url = None
    snippet = "(content no longer exists)"
    author = None
    subdeaddit_name = None
    if kind == "post" and item is not None:
        url = url_for(
            "web.post",
            subdeaddit_name=item.subdeaddit_name,
            post_id=item.id,
        )
        snippet = (item.title or item.content or "")[:120]
        author = item.user
        subdeaddit_name = item.subdeaddit_name
    elif kind == "comment" and item is not None:
        url = url_for(
            "web.post",
            subdeaddit_name=item.post.subdeaddit_name,
            post_id=item.post_id,
            _anchor=f"comment-{item.id}",
        )
        snippet = (item.content or "")[:120]
        author = item.user
        subdeaddit_name = item.post.subdeaddit_name
    return {
        "report": report,
        "kind": kind,
        "url": url,
        "snippet": snippet,
        "author": author,
        "subdeaddit_name": subdeaddit_name,
    }


def _report_subdeaddit_name(report):
    """Subdeaddit scope for a ban: the reported item's community."""
    _, item = _report_target(report)
    if item is None:
        return None
    return (
        item.subdeaddit_name
        if hasattr(item, "subdeaddit_name")
        else item.post.subdeaddit_name
    )


@admin_bp.route("/reports")
@production_disabled
@admin_required
def reports():
    """Moderation report queue (Phase D4)."""
    status = request.args.get("status", "open")
    if status not in _REPORT_STATUSES:
        status = "open"

    from deaddit.dynamics import moderation

    if status == "all":
        query = Report.query.order_by(desc(Report.created_at), desc(Report.id))
    else:
        query = moderation.list_reports(status=status)

    page = int(request.args.get("page", 1))
    pagination = query.paginate(page=page, per_page=20, error_out=False)

    return render_template(
        "admin/reports.html",
        rows=[_report_row(report) for report in pagination.items],
        pagination=pagination,
        statuses=_REPORT_STATUSES,
        current_status=status,
    )


@admin_bp.route("/reports/<int:report_id>/remove", methods=["POST"])
@production_disabled
@admin_required
def report_remove(report_id):
    """Action a report: soft-remove the reported content (Phase D4)."""
    removal_reason = (request.form.get("removal_reason") or "").strip() or None

    from deaddit.dynamics import moderation

    try:
        report = moderation.remove_report(
            report_id, moderator="admin", removal_reason=removal_reason
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.reports"))

    flash(f"Report #{report.id} actioned: content removed.", "success")
    return redirect(url_for("admin.reports"))


@admin_bp.route("/reports/<int:report_id>/dismiss", methods=["POST"])
@production_disabled
@admin_required
def report_dismiss(report_id):
    """Dismiss a report without acting on the content (Phase D4)."""
    note = (request.form.get("note") or "").strip() or None

    from deaddit.dynamics import moderation

    try:
        report = moderation.dismiss_report(report_id, moderator="admin", note=note)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.reports"))

    flash(f"Report #{report.id} dismissed.", "success")
    return redirect(url_for("admin.reports"))


@admin_bp.route("/reports/<int:report_id>/ban", methods=["POST"])
@production_disabled
@admin_required
def report_ban(report_id):
    """Ban the author of the reported content (Phase D4)."""
    report = Report.query.get_or_404(report_id)
    _, item = _report_target(report)
    if item is None:
        flash("Reported content no longer exists; cannot ban its author.", "error")
        return redirect(url_for("admin.reports", status=request.args.get("status")))

    username = item.user
    scope = request.form.get("scope", "site")
    reason = (request.form.get("reason") or "").strip()
    duration_raw = (request.form.get("duration_days") or "").strip()

    if not reason:
        flash("A ban reason is required.", "error")
        return redirect(url_for("admin.reports"))

    expires_at = None
    if duration_raw:
        try:
            days = int(duration_raw)
            if days <= 0:
                raise ValueError("duration must be positive")
        except ValueError as exc:
            flash(f"Invalid ban duration: {exc}", "error")
            return redirect(url_for("admin.reports"))
        expires_at = datetime.utcnow() + timedelta(days=days)

    subdeaddit_name = _report_subdeaddit_name(report) if scope == "subdeaddit" else None

    from deaddit.dynamics import moderation

    try:
        ban = moderation.ban_user(
            username,
            reason,
            subdeaddit_name=subdeaddit_name,
            expires_at=expires_at,
            banned_by="admin",
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.reports"))

    scope_label = ban.subdeaddit_name or "site-wide"
    duration_label = f" until {ban.expires_at:%Y-%m-%d}" if ban.expires_at else ""
    flash(f"Banned u/{username} ({scope_label}){duration_label}.", "success")
    return redirect(url_for("admin.reports"))


# --- LLM-5: prompt versioning (read-only visibility + version creation) ---
# JSON contract for the UX lane: page/template work is UX-owned; these
# endpoints are the data surface. All routes are @production_disabled +
# @admin_required like the other admin APIs.


def _version_dict(row):
    return {
        "id": row.id,
        "template_id": row.template_id,
        "version": row.version,
        "body": row.body,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@admin_bp.route("/api/prompts")
@production_disabled
@admin_required
def prompts_list_api():
    """List prompt templates with version summaries and active pins."""
    templates = PromptTemplate.query.order_by(PromptTemplate.name.asc()).all()
    return jsonify(
        [
            {
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "versions": [
                    v.version
                    for v in t.versions.order_by(PromptTemplateVersion.version.asc())
                ],
                "latest_version": (
                    t.versions.order_by(PromptTemplateVersion.version.desc())
                    .first()
                    .version
                    if t.versions.count()
                    else None
                ),
                "pinned_by": sorted(
                    f"{pin.target_kind}:{pin.target_key}"
                    for pin in PromptPin.query.filter_by(template_id=t.id)
                ),
            }
            for t in templates
        ]
    )


@admin_bp.route("/api/prompts/<name>")
@production_disabled
@admin_required
def prompts_detail_api(name):
    """Full template detail: every immutable version plus its pins."""
    template = PromptTemplate.query.filter_by(name=name).first()
    if template is None:
        return jsonify({"error": f"Unknown prompt template {name!r}"}), 404
    versions = template.versions.order_by(PromptTemplateVersion.version.asc()).all()
    return jsonify(
        {
            "id": template.id,
            "name": template.name,
            "description": template.description,
            "versions": [_version_dict(v) for v in versions],
            "pins": [
                {
                    "target_kind": p.target_kind,
                    "target_key": p.target_key,
                    "version_number": p.version_number,
                }
                for p in PromptPin.query.filter_by(template_id=template.id)
            ],
        }
    )


@admin_bp.route("/api/prompts/<name>/versions", methods=["POST"])
@production_disabled
@admin_required
def prompts_create_version_api(name):
    """Create version n+1; existing versions are immutable and queryable."""
    from deaddit.llm.prompts import PromptError, create_version

    data = request.get_json(silent=True) or {}
    body = data.get("body")
    if not body or not isinstance(body, str):
        return jsonify({"error": "Field 'body' (non-empty string) is required"}), 400
    try:
        row = create_version(name, body, created_by=data.get("created_by"))
    except PromptError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify(_version_dict(row)), 201


@admin_bp.route("/api/pins")
@production_disabled
@admin_required
def pins_list_api():
    """List agent/cohort -> prompt-version pins."""
    return jsonify(
        [
            {
                "target_kind": p.target_kind,
                "target_key": p.target_key,
                "template_id": p.template_id,
                "template_name": (
                    db.session.get(PromptTemplate, p.template_id).name
                    if db.session.get(PromptTemplate, p.template_id)
                    else None
                ),
                "version_number": p.version_number,
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            }
            for p in PromptPin.query.order_by(
                PromptPin.target_kind.asc(), PromptPin.target_key.asc()
            )
        ]
    )


@admin_bp.route("/api/pins", methods=["POST"])
@production_disabled
@admin_required
def pins_set_api():
    """Upsert one pin: {target_kind, target_key, template, version}."""
    from deaddit.llm.prompts import PromptError, set_pin

    data = request.get_json(silent=True) or {}
    try:
        set_pin(
            data.get("target_kind", ""),
            data.get("target_key", ""),
            data.get("template", ""),
            int(data.get("version", 0)),
        )
    except (PromptError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return pins_list_api()


@admin_bp.route("/api/pins/<target_kind>/<target_key>", methods=["DELETE"])
@production_disabled
@admin_required
def pins_clear_api(target_kind, target_key):
    from deaddit.llm.prompts import clear_pin

    if clear_pin(target_kind, target_key):
        return jsonify({"cleared": True})
    return jsonify({"cleared": False, "error": "No such pin"}), 404


@admin_bp.route("/api/prompt-renders")
@production_disabled
@admin_required
def prompt_renders_api():
    """Recent render-audit rows: which prompt version produced which run.

    ``?limit=<n>`` (default 50, max 500). Joins agent_run via
    subject_key == Agent.user_username and overlapping run timestamps.
    """
    limit = min(request.args.get("limit", 50, type=int) or 50, 500)
    rows = (
        PromptRenderAudit.query.order_by(PromptRenderAudit.created_at.desc())
        .limit(limit)
        .all()
    )
    return jsonify(
        [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "template_id": r.template_id,
                "template_version_id": r.template_version_id,
                "subject_kind": r.subject_kind,
                "subject_key": r.subject_key,
                "rendered_sha256": r.rendered_sha256,
                "variables_json": r.variables_json,
            }
            for r in rows
        ]
    )
