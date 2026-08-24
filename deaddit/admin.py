"""
Admin interface for Deaddit content management.
Provides web-based UI for job management and content generation.
"""

import base64
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
import logging

logger = logging.getLogger(__name__)
from sqlalchemy import desc

from deaddit import db
from deaddit.config import Config
from deaddit.jobs import cancel_job, create_job, get_job_status, get_queue_stats
from deaddit.utils import production_disabled
from deaddit.models import (
    ApiEndpointConfig,
    ApiModel,
    Comment,
    GenerationTemplate,
    Job,
    JobStatus,
    JobType,
    Post,
    Subdeaddit,
    User,
)

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
    """Admin dashboard with overview statistics."""

    # Get basic content statistics
    stats = {
        "total_posts": Post.query.count(),
        "total_comments": Comment.query.count(),
        "total_users": User.query.count(),
        "total_subdeaddits": Subdeaddit.query.count(),
    }

    # Get recent activity (last 24 hours)
    since_yesterday = datetime.utcnow() - timedelta(days=1)

    # Count recent completed user creation jobs as proxy for new users
    recent_user_jobs = Job.query.filter(
        Job.created_at >= since_yesterday,
        Job.type == JobType.CREATE_USER,
        Job.status == JobStatus.COMPLETED,
    ).count()

    recent_stats = {
        "posts_24h": Post.query.filter(Post.created_at >= since_yesterday).count(),
        "comments_24h": Comment.query.filter(
            Comment.created_at >= since_yesterday
        ).count(),
        "users_24h": recent_user_jobs,
    }

    # Get job statistics
    job_stats = {
        "total_jobs": Job.query.count(),
        "pending_jobs": Job.query.filter_by(status=JobStatus.PENDING).count(),
        "running_jobs": Job.query.filter_by(status=JobStatus.RUNNING).count(),
        "completed_jobs": Job.query.filter_by(status=JobStatus.COMPLETED).count(),
        "failed_jobs": Job.query.filter_by(status=JobStatus.FAILED).count(),
    }

    # Get recent jobs
    recent_jobs = Job.query.order_by(desc(Job.created_at)).limit(10).all()

    # Get queue statistics (handle Redis not available)
    try:
        queue_stats = get_queue_stats()
    except Exception as e:
        logger.warning(f"Could not get queue stats: {e}")
        queue_stats = {
            "high_priority": {"pending": 0, "failed": 0},
            "normal": {"pending": 0, "failed": 0},
            "low_priority": {"pending": 0, "failed": 0},
        }

    return render_template(
        "admin/dashboard.html",
        stats=stats,
        recent_stats=recent_stats,
        job_stats=job_stats,
        recent_jobs=recent_jobs,
        queue_stats=queue_stats,
    )


@admin_bp.route("/generate")
@production_disabled
@admin_required
def generate():
    """Content generation management page."""
    templates = GenerationTemplate.query.all()
    subdeaddits = Subdeaddit.query.all()

    # Check if default data has been loaded
    default_data_loaded = Config.get("DEFAULT_DATA_LOADED", "false") == "true"

    return render_template(
        "admin/generate.html",
        templates=templates,
        subdeaddits=subdeaddits,
        default_data_loaded=default_data_loaded,
    )


@admin_bp.route("/generate/subdeaddit", methods=["POST"])
@production_disabled
@admin_required
def generate_subdeaddit():
    """Create a job to generate subdeaddits."""

    count = int(request.form.get("count", 1))
    model = request.form.get("model")
    wait = int(request.form.get("wait", 0))
    priority = int(request.form.get("priority", 5))

    parameters = {"count": count, "wait": wait}
    if model:
        parameters["model"] = model

    job = create_job(
        job_type=JobType.CREATE_SUBDEADDIT,
        parameters=parameters,
        priority=priority,
        total_items=count,
    )

    flash(f"Subdeaddit generation job created (ID: {job.id})", "success")
    return redirect(url_for("admin.jobs"))


@admin_bp.route("/generate/user", methods=["POST"])
@production_disabled
@admin_required
def generate_user():
    """Create a job to generate users."""

    count = int(request.form.get("count", 1))
    model = request.form.get("model")
    wait = int(request.form.get("wait", 0))
    priority = int(request.form.get("priority", 5))

    parameters = {"count": count, "wait": wait}
    if model:
        parameters["model"] = model

    job = create_job(
        job_type=JobType.CREATE_USER,
        parameters=parameters,
        priority=priority,
        total_items=count,
    )

    flash(f"User generation job created (ID: {job.id})", "success")
    return redirect(url_for("admin.jobs"))


@admin_bp.route("/generate/post", methods=["POST"])
@production_disabled
@admin_required
def generate_post():
    """Create a job to generate posts."""

    count = int(request.form.get("count", 1))
    subdeaddit = request.form.get("subdeaddit")
    replies = request.form.get("replies", "5-10")
    model = request.form.get("model")
    wait = int(request.form.get("wait", 0))
    priority = int(request.form.get("priority", 5))

    parameters = {"count": count, "wait": wait, "replies": replies}
    if subdeaddit:
        parameters["subdeaddit"] = subdeaddit
    if model:
        parameters["model"] = model

    job = create_job(
        job_type=JobType.CREATE_POST,
        parameters=parameters,
        priority=priority,
        total_items=count,
    )

    flash(f"Post generation job created (ID: {job.id})", "success")
    return redirect(url_for("admin.jobs"))


@admin_bp.route("/generate/comment", methods=["POST"])
@production_disabled
@admin_required
def generate_comment():
    """Create a job to generate comments."""

    count = int(request.form.get("count", 1))
    post_id = request.form.get("post_id")
    subdeaddit = request.form.get("subdeaddit")
    model = request.form.get("model")
    wait = int(request.form.get("wait", 0))
    priority = int(request.form.get("priority", 5))

    parameters = {"count": count, "wait": wait}
    if post_id:
        parameters["post_id"] = int(post_id)
    if subdeaddit:
        parameters["subdeaddit"] = subdeaddit
    if model:
        parameters["model"] = model

    job = create_job(
        job_type=JobType.CREATE_COMMENT,
        parameters=parameters,
        priority=priority,
        total_items=count,
    )

    flash(f"Comment generation job created (ID: {job.id})", "success")
    return redirect(url_for("admin.jobs"))


@admin_bp.route("/jobs")
@production_disabled
@admin_required
def jobs():
    """Job management page."""

    # Get filter parameters
    status_filter = request.args.get("status")
    type_filter = request.args.get("type")
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 20))

    # Build query
    query = Job.query

    if status_filter:
        query = query.filter(Job.status == JobStatus(status_filter))

    if type_filter:
        query = query.filter(Job.type == JobType(type_filter))

    # Order by creation date (newest first)
    query = query.order_by(desc(Job.created_at))

    # Paginate
    jobs_pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    # Get available filter options
    job_types = [jt.value for jt in JobType]
    job_statuses = [js.value for js in JobStatus]

    # Get job status counts for quick stats
    job_counts = {
        'pending': Job.query.filter_by(status=JobStatus.PENDING).count(),
        'running': Job.query.filter_by(status=JobStatus.RUNNING).count(), 
        'completed': Job.query.filter_by(status=JobStatus.COMPLETED).count(),
        'failed': Job.query.filter_by(status=JobStatus.FAILED).count(),
        'cancelled': Job.query.filter_by(status=JobStatus.CANCELLED).count(),
    }

    return render_template(
        "admin/jobs.html",
        jobs=jobs_pagination.items,
        pagination=jobs_pagination,
        job_types=job_types,
        job_statuses=job_statuses,
        current_status=status_filter,
        current_type=type_filter,
        job_counts=job_counts,
    )


@admin_bp.route("/jobs/<int:job_id>")
@production_disabled
@admin_required
def job_detail(job_id):
    """Job detail page."""
    job = Job.query.get_or_404(job_id)

    # Find related jobs (same type, created around the same time)
    time_window = timedelta(hours=24)
    related_jobs = (
        Job.query.filter(
            Job.id != job.id,
            Job.type == job.type,
            Job.created_at >= job.created_at - time_window,
            Job.created_at <= job.created_at + time_window,
        )
        .order_by(desc(Job.created_at))
        .limit(10)
        .all()
    )

    return render_template(
        "admin/job_detail.html",
        job=job,
        related_jobs=related_jobs,
        User=User,
        Post=Post,
        Comment=Comment,
        Subdeaddit=Subdeaddit,
    )


@admin_bp.route("/jobs/<int:job_id>/cancel", methods=["POST"])
@production_disabled
@admin_required
def cancel_job_route(job_id):
    """Cancel a job."""
    if cancel_job(job_id):
        flash(f"Job {job_id} cancelled successfully", "success")
    else:
        flash(f"Could not cancel job {job_id}", "error")

    return redirect(url_for("admin.job_detail", job_id=job_id))


@admin_bp.route("/jobs/<int:job_id>/retry", methods=["POST"])
@production_disabled
@admin_required
def retry_job_route(job_id):
    """Retry a failed job."""
    original_job = Job.query.get_or_404(job_id)

    if original_job.status not in [JobStatus.FAILED, JobStatus.CANCELLED]:
        flash("Only failed or cancelled jobs can be retried", "error")
        return redirect(url_for("admin.job_detail", job_id=job_id))

    # Create a new job with the same parameters
    new_job = create_job(
        job_type=original_job.type,
        parameters=original_job.parameters,
        priority=original_job.priority,
        total_items=original_job.total_items,
    )

    flash(f"Job retried as new job #{new_job.id}", "success")
    return redirect(url_for("admin.job_detail", job_id=new_job.id))


@admin_bp.route("/api/jobs/<int:job_id>/status")
@production_disabled
@admin_required
def job_status_api(job_id):
    """API endpoint to get job status (for real-time updates)."""
    status = get_job_status(job_id)
    if status:
        return jsonify(status)
    else:
        return jsonify({"error": "Job not found"}), 404


@admin_bp.route("/api/jobs/stats")
@production_disabled
@admin_required
def jobs_stats_api():
    """API endpoint to get job statistics."""
    try:
        stats = get_queue_stats()
    except Exception as e:
        logger.warning(f"Could not get queue stats: {e}")
        stats = {
            "scheduler_running": False,
            "total_jobs": 0,
            "pending_jobs": 0,
            "running_jobs": 0,
        }

    # Add database job counts
    stats["database"] = {
        "pending": Job.query.filter_by(status=JobStatus.PENDING).count(),
        "running": Job.query.filter_by(status=JobStatus.RUNNING).count(),
        "completed": Job.query.filter_by(status=JobStatus.COMPLETED).count(),
        "failed": Job.query.filter_by(status=JobStatus.FAILED).count(),
    }

    return jsonify(stats)


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

    return render_template(
        "admin/content.html",
        content_stats=content_stats,
        recent_posts=recent_posts,
        recent_comments=recent_comments,
    )


# CRUD API endpoints for content management


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
            "users": [
                {
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
                    "comments_count": Comment.query.filter_by(
                        user=user.username
                    ).count(),
                }
                for user in users.items
            ],
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


@admin_bp.route("/api/users/<username>", methods=["DELETE"])
@production_disabled
@admin_required
def api_delete_user(username):
    """Delete a user and all associated content."""
    user = User.query.get_or_404(username)

    try:
        # Get impact stats before deletion
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
            "subdeaddits": [
                {
                    "name": sub.name,
                    "description": sub.description or "",
                    "post_types": sub.post_types or "",
                    "posts_count": Post.query.filter_by(
                        subdeaddit_name=sub.name
                    ).count(),
                }
                for sub in subdeaddits.items
            ],
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
            "posts": [
                {
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
                    "upvote_count": post.upvote_count or 0,
                    "post_type": post.post_type or "",
                    "comments_count": Comment.query.filter_by(post_id=post.id).count(),
                    "created_at": post.created_at.isoformat()
                    if post.created_at
                    else "",
                    "model": post.model or "",
                }
                for post in posts.items
            ],
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
        post.upvote_count = data.get("upvote_count", post.upvote_count)
        post.post_type = data.get("post_type", post.post_type)

        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating post {post_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


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
            "comments": [
                {
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
                    "upvote_count": comment.upvote_count or 0,
                    "created_at": comment.created_at.isoformat()
                    if comment.created_at
                    else "",
                    "model": comment.model or "",
                }
                for comment in comments.items
            ],
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
        comment.upvote_count = data.get("upvote_count", comment.upvote_count)

        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating comment {comment_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


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
        return jsonify({"success": False, "error": str(e)}), 500


@admin_bp.route("/analytics")
@production_disabled
@admin_required
def analytics():
    """Analytics and insights page."""

    # Get generation metrics over time
    # This is a placeholder - in a real implementation, you'd want more sophisticated analytics

    # Model usage statistics
    model_stats = {}
    for model in db.session.query(Post.model).distinct():
        if model[0]:
            count = Post.query.filter_by(model=model[0]).count()
            model_stats[model[0]] = count

    # Daily generation counts (last 30 days)
    daily_stats = []
    for i in range(30):
        date = datetime.utcnow() - timedelta(days=i)
        date_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        date_end = date_start + timedelta(days=1)

        posts_count = Post.query.filter(
            Post.created_at >= date_start, Post.created_at < date_end
        ).count()

        comments_count = Comment.query.filter(
            Comment.created_at >= date_start, Comment.created_at < date_end
        ).count()

        daily_stats.append(
            {
                "date": date_start.strftime("%Y-%m-%d"),
                "posts": posts_count,
                "comments": comments_count,
            }
        )

    daily_stats.reverse()  # Show oldest to newest

    return render_template(
        "admin/analytics.html", model_stats=model_stats, daily_stats=daily_stats
    )


@admin_bp.route("/settings")
@production_disabled
@admin_required
def settings():
    """Settings and configuration page."""

    # Get current configuration from database
    all_settings = Config.get_all_settings()

    config = {
        "openai_api_url": all_settings["OPENAI_API_URL"]["value"],
        "openai_model": all_settings["OPENAI_MODEL"]["value"],
        "api_base_url": all_settings["API_BASE_URL"]["value"],
        "models": all_settings["MODELS"]["value"],
        "api_token_set": all_settings["API_TOKEN"]["value"] == "***set***",
        "openai_key_set": all_settings["OPENAI_KEY"]["value"]
        != "your_openrouter_api_key"
        and bool(all_settings["OPENAI_KEY"]["value"]),
        "all_settings": all_settings,
    }

    return render_template("admin/settings.html", config=config)


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

        # Handle per-endpoint API key storage
        if data.get("openai_key"):
            if endpoint_url:
                Config.set_api_key_for_endpoint(endpoint_url, data["openai_key"])
            else:
                # If no endpoint URL, use current endpoint
                current_endpoint = Config.get("OPENAI_API_URL")
                if current_endpoint:
                    Config.set_api_key_for_endpoint(
                        current_endpoint, data["openai_key"]
                    )
                else:
                    Config.set("OPENAI_KEY", data["openai_key"])

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
            "openai_model": Config.get("OPENAI_MODEL", "Not set"),
            "api_base_url": Config.get("API_BASE_URL", "Not set"),
            "openai_key_set": bool(Config.get_api_key_for_endpoint(current_endpoint))
            and Config.get_api_key_for_endpoint(current_endpoint)
            != "your_openrouter_api_key",
        }

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

        if data.get("api_token"):
            # Validate minimum length
            token = data["api_token"].strip()
            if len(token) < 3:
                return jsonify(
                    {
                        "success": False,
                        "message": "API Token must be at least 3 characters long",
                    }
                )
            Config.set("API_TOKEN", token)

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
        data = request.get_json()
        api_url = data.get("api_url")
        api_key = data.get("api_key")

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
            if not api_key:
                return jsonify(
                    {
                        "success": False,
                        "message": "API key is required. Please enter a key or save one in settings first.",
                        "status_code": None,
                    }
                )

        # Test connection to AI service
        headers = {"Authorization": f"Bearer {api_key}"}
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
                    "message": "AI service returned an error response",
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
        data = request.get_json()
        api_url = data.get("api_url")
        api_key = data.get("api_key")

        if not api_url:
            return jsonify({"success": False, "message": "API URL is required"})

        # If no API key provided or masked key, try to use saved key for this endpoint
        if not api_key or api_key == "••••••••••••••••":
            api_key = Config.get_api_key_for_endpoint(api_url)
            if not api_key:
                return jsonify(
                    {
                        "success": False,
                        "message": "API key is required. Please enter a key or save one in settings first.",
                    }
                )

        # Use the comprehensive model fetching function
        models, fetch_message = fetch_all_models_from_api(api_url, api_key)

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

        return jsonify(
            {
                "success": True,
                "api_key": api_key,
                "masked_key": "••••••••••••••••" if api_key else None,
                "has_key": bool(api_key),
            }
        )

    except Exception as e:
        return jsonify(
            {"success": False, "message": f"Error getting endpoint key: {str(e)}"}
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
                # Check if subdeaddit already exists
                existing = Subdeaddit.query.filter_by(
                    name=subdeaddit_data["name"]
                ).first()
                if not existing:
                    subdeaddit = Subdeaddit(
                        name=subdeaddit_data["name"],
                        description=subdeaddit_data["description"],
                    )
                    # Use the helper method to properly set post_types as JSON
                    subdeaddit.set_post_types(subdeaddit_data.get("post_types", []))
                    db.session.add(subdeaddit)
                    subdeaddits_loaded += 1

            logger.info(f"Loaded {subdeaddits_loaded} new subdeaddits")

        # Load users (limit to first 50 to avoid overwhelming the system)
        if os.path.exists(users_file):
            with open(users_file) as f:
                users_data = json.load(f)

            for user_data in users_data.get("users", [])[
                :50
            ]:  # Limit to first 50 users
                # Check if user already exists
                existing = User.query.filter_by(username=user_data["username"]).first()
                if not existing:
                    user = User(
                        username=user_data["username"],
                        bio=user_data["bio"],
                        age=user_data["age"],
                        gender=user_data["gender"],
                        education=user_data["education"],
                        occupation=user_data["occupation"],
                        interests=json.dumps(
                            user_data["interests"]
                        ),  # Convert list to JSON string
                        personality_traits=json.dumps(
                            user_data["personality_traits"]
                        ),  # Convert list to JSON string
                        writing_style=user_data["writing_style"],
                        model=user_data.get("model", "default"),
                    )
                    db.session.add(user)
                    users_loaded += 1

            logger.info(f"Loaded {users_loaded} new users")

        # Commit all changes
        db.session.commit()

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
