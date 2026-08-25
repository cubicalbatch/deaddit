import json
import os
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request
from sqlalchemy import func

from deaddit.services.content import (
    ContentValidationError,
    create_comment,
    create_post,
    create_subdeaddit,
    create_user,
    get_available_models,
)
from deaddit.utils import production_disabled

from .models import Comment, Post, Subdeaddit, User

bp = Blueprint("api", __name__)


def authenticate_ingest():
    """Token-gate only the ingest endpoints, matching the legacy global guard."""
    if request.path.startswith("/api/ingest"):
        token = request.headers.get("Authorization")
        # Use Config to get API_TOKEN (database first, then environment)
        api_token = None
        try:
            from .config import Config

            api_token = Config.get("API_TOKEN")
        except Exception:
            # Fallback to environment if Config isn't available yet
            api_token = os.environ.get("API_TOKEN")

        if api_token and (not token or token != f"Bearer {api_token}"):
            return jsonify({"error": "Unauthorized"}), 401


bp.before_request(authenticate_ingest)


@bp.route("/api/ingest", methods=["POST"])
@production_disabled
def ingest():
    """Ingest posts, comments and subdeaddits.

    THIN WRAPPER over :mod:`deaddit.services.content` (Resolution 1), kept
    only for external tooling compatibility and scheduled for deletion at
    Wave 6 (owner decision 8). Internal callers must use the service
    (``create_post`` / ``create_comment`` / ``create_subdeaddit``) directly.
    """
    data = request.get_json()

    if not data:
        return jsonify({"error": "No data provided"}), 400

    posts = data.get("posts", [])
    comments = data.get("comments", [])
    subdeaddits = data.get("subdeaddits", [])

    # Read-only validation pass replicating the legacy all-or-nothing checks
    # with exact precedence (posts, then comments, then subdeaddits). The
    # service commits per item, so every item must validate BEFORE any
    # create_* call; ContentValidationError below is only a backstop.
    for post_data in posts:
        user = post_data.get("user")
        if not User.query.filter_by(username=user).first():
            return jsonify({"error": f"User '{user}' does not exist"}), 400

        if not all(
            [
                post_data.get("title"),
                post_data.get("content"),
                post_data.get("upvote_count"),
                user,
                post_data.get("subdeaddit"),
            ]
        ):
            return jsonify({"error": "Invalid post data"}), 400

        subdeaddit_name = post_data.get("subdeaddit")
        if not Subdeaddit.query.filter_by(name=subdeaddit_name).first():
            return (
                jsonify(
                    {"error": f"Subdeaddit '{subdeaddit_name}' does not exist"}
                ),
                400,
            )

    for comment_data in comments:
        user = comment_data.get("user")
        if not User.query.filter_by(username=user).first():
            return jsonify({"error": f"User '{user}' does not exist"}), 400

        missing_fields = [
            field
            for field in ("post_id", "content", "user")
            if not comment_data.get(field)
        ]
        if missing_fields:
            return (
                jsonify(
                    {
                        "error": (
                            "Comment missing required fields: "
                            f"{', '.join(missing_fields)}"
                        )
                    }
                ),
                400,
            )

    for subdeaddit_data in subdeaddits:
        missing_fields = [
            field
            for field in ("name", "description")
            if not subdeaddit_data.get(field)
        ]
        if missing_fields:
            return (
                jsonify(
                    {
                        "error": (
                            "Subdeaddit missing required fields: "
                            f"{', '.join(missing_fields)}"
                        )
                    }
                ),
                400,
            )

    added = []
    created_posts = []
    created_comments = []

    try:
        for post_data in posts:
            post = create_post(
                title=post_data.get("title"),
                content=post_data.get("content"),
                user=post_data.get("user"),
                subdeaddit=post_data.get("subdeaddit"),
                upvote_count=post_data.get("upvote_count"),
                model=post_data.get("model", "unknown"),
            )
            added.append(post.title)
            created_posts.append(post)

        for comment_data in comments:
            comment = create_comment(
                post_id=comment_data.get("post_id"),
                content=comment_data.get("content"),
                user=comment_data.get("user"),
                parent_id=comment_data.get("parent_id"),
                upvote_count=comment_data.get("upvote_count", 0),
                model=comment_data.get("model", "unknown"),
            )
            added.append(comment.content)
            created_comments.append(comment)

        for subdeaddit_data in subdeaddits:
            name = subdeaddit_data.get("name")
            existed = Subdeaddit.query.get(name) is not None
            create_subdeaddit(
                name=name,
                description=subdeaddit_data.get("description"),
                post_types=subdeaddit_data.get("post_types", []),
                update_if_exists=True,
            )
            added.append(
                f"{'Updated' if existed else 'Created'} subdeaddit: {name}"
            )
    except ContentValidationError as exc:  # backstop, see validation above
        return jsonify({"error": str(exc)}), 400

    # Prepare response with created post IDs
    response_data = {
        "message": "Posts and comments created successfully",
        "added": added,
    }

    # Add post IDs to response if posts were created
    if created_posts:
        response_data["posts"] = [
            {"id": post.id, "title": post.title} for post in created_posts
        ]

    # Add comment IDs to response if comments were created
    if created_comments:
        response_data["comments"] = [
            {"id": comment.id, "content": comment.content[:50]}
            for comment in created_comments
        ]

    return jsonify(response_data), 201


@bp.route("/api/subdeaddits", methods=["GET"])
def api_subdeaddits():
    """
    Retrieves a list of subdeaddits.

    Returns:
        A JSON response containing a list of subdeaddits with their names, descriptions, and post_types.
    """
    subdeaddits = Subdeaddit.query.all()
    subdeaddit_list = []
    for subdeaddit in subdeaddits:
        subdeaddit_data = {
            "name": subdeaddit.name,
            "description": subdeaddit.description,
            "post_types": subdeaddit.get_post_types(),
        }
        subdeaddit_list.append(subdeaddit_data)

    response = {"subdeaddits": subdeaddit_list}
    return jsonify(response)


@bp.route("/api/posts", methods=["GET"])
def api_posts():
    subdeaddit_name = request.args.get("subdeaddit")
    post_type = request.args.get("post_type")
    days = request.args.get("days", type=int)
    max_comments = request.args.get("max_comments", type=int)
    limit = request.args.get("limit", default=50, type=int)
    title = request.args.get("title")  # New parameter for title filtering

    query = Post.query

    # Filter by Subdeaddit if provided
    if subdeaddit_name:
        subdeaddit = Subdeaddit.query.filter_by(name=subdeaddit_name).first()
        if not subdeaddit:
            return jsonify(
                {"error": f"Subdeaddit '{subdeaddit_name}' does not exist"}
            ), 404
        query = query.filter(Post.subdeaddit == subdeaddit)

    # Filter by post_type if provided
    if post_type:
        query = query.filter(Post.post_type == post_type)

    # Filter by date if days parameter is provided
    if days is not None:
        date_limit = datetime.utcnow() - timedelta(days=days)
        query = query.filter(Post.created_at >= date_limit)

    # Filter by title if provided
    if title:
        query = query.filter(func.lower(Post.title) == func.lower(title))

    # Add sorting
    query = query.order_by(Post.created_at.desc())

    # Execute query and limit results
    posts = query.limit(limit).all()

    # Build response data, filtering by comment count if required
    post_data = []
    for post in posts:
        comment_count = Comment.query.filter_by(post_id=post.id).count()

        # Apply max_comments filter if provided
        if max_comments is not None and comment_count > max_comments:
            continue

        post_info = {
            "id": post.id,
            "subdeaddit": post.subdeaddit.name,
            "title": post.title,
            "content": post.content,
            "comment_count": comment_count,
            "created_at": post.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "post_type": post.post_type,
            "user": post.user,
            "upvote_count": post.upvote_count,
            "model": post.model,
        }
        post_data.append(post_info)

    return jsonify({"posts": post_data})


@bp.route("/api/post/<post_id>", methods=["GET"])
def api_post(post_id):
    if not post_id:
        return jsonify({"error": "Post ID is required"}), 400

    post = Post.query.get(post_id)
    if not post:
        return jsonify({"error": f"Post with ID {post_id} does not exist"}), 404

    comment_count = Comment.query.filter_by(post_id=post.id).count()

    comments = Comment.query.filter_by(post_id=post.id).all()
    comment_tree = build_comment_tree(comments)

    post_data = {
        "id": post.id,
        "subdeaddit": post.subdeaddit.name,
        "title": post.title,
        "upvote_count": post.upvote_count,
        "user": post.user,
        "content": post.content.replace("reddit", "deaddit"),
        "comment_count": comment_count,
        "comments": comment_tree,
    }

    return jsonify(post_data)


def build_comment_tree(comments):
    comment_map = {comment.id: comment for comment in comments}
    comment_tree = []

    for comment in comments:
        if comment.parent_id is None or comment.parent_id == "":
            comment_tree.append(format_comment(comment, comment_map))

    return comment_tree


def format_comment(comment, comment_map):
    formatted_comment = {
        "id": comment.id,
        "user": comment.user,
        "content": comment.content.replace("reddit", "deaddit"),
        "parent_id": comment.parent_id,
        "replies": [],
    }

    for _reply_id, reply_comment in comment_map.items():
        if reply_comment.parent_id == comment.id:
            formatted_comment["replies"].append(
                format_comment(reply_comment, comment_map)
            )

    return formatted_comment


@bp.route("/api/ingest/user", methods=["POST"])
@production_disabled
def ingest_user():
    """Ingest a single user.

    THIN WRAPPER over :func:`deaddit.services.content.create_user`
    (Resolution 1), kept only for external tooling compatibility and
    scheduled for deletion at Wave 6 (owner decision 8). Internal callers
    must use the service directly.
    """
    data = request.get_json()

    if not data:
        return jsonify({"error": "No data provided"}), 400

    required_fields = [
        "username",
        "age",
        "gender",
        "bio",
        "interests",
        "occupation",
        "education",
        "writing_style",
        "personality_traits",
    ]

    for field in required_fields:
        if field not in data:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    try:
        user = create_user(
            username=data["username"],
            age=data["age"],
            gender=data["gender"],
            bio=data["bio"],
            interests=data["interests"],
            occupation=data["occupation"],
            education=data["education"],
            writing_style=data["writing_style"],
            personality_traits=data["personality_traits"],
            model=data.get("model", "unknown"),
        )
    except ContentValidationError as exc:  # backstop; service-side validation
        return jsonify({"error": str(exc)}), 400

    return (
        jsonify({"message": "User created successfully", "username": user.username}),
        201,
    )


@bp.route("/api/users", methods=["GET"])
def get_users():
    users = User.query.all()
    user_list = [
        {
            "username": user.username,
            "age": user.age,
            "gender": user.gender,
            "bio": user.bio,
            "interests": json.loads(user.interests),
            "occupation": user.occupation,
            "education": user.education,
            "writing_style": user.writing_style,
            "personality_traits": json.loads(user.personality_traits),
            "model": user.model
            if isinstance(user.model, str)
            else json.loads(user.model)
            if user.model
            else "unknown",
        }
        for user in users
    ]
    return jsonify({"users": user_list})


@bp.route("/api/available_models")
def available_models():
    models = get_available_models()
    return jsonify({"models": models})
