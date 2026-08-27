import json
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request, url_for
from sqlalchemy import func

from deaddit.services.content import get_available_models

from .models import Comment, Post, PostImage, Subdeaddit, User

bp = Blueprint("api", __name__)


def _public_image(image: PostImage | None, removed: bool) -> dict | None:
    """Public URL/metadata payload for a post's image, or ``None``.

    A removed post never exposes image URLs (moderation tombstone); the
    private generation provenance (source_prompt, provider snapshots,
    request IDs) never leaves ``PostImage.to_dict()`` in the first place.
    """
    if image is None or removed:
        return None
    data = image.to_dict()
    data["original_url"] = url_for("media.original", filename=data["original_url"])
    data["thumbnail_url"] = url_for("media.thumbnail", filename=data["thumbnail_url"])
    return data


@bp.route("/api/subdeaddits", methods=["GET"])
def api_subdeaddits():
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

    query = Post.query.filter(Post.removed.is_(False))

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

    # One bulk lookup for every post's image instead of a per-post query
    # (PostImage.post_id is its primary key, so this is a single IN query).
    images_by_post_id = {
        image.post_id: image
        for image in PostImage.query.filter(
            PostImage.post_id.in_([post.id for post in posts])
        ).all()
    }

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
            "score": post.score,
            "model": post.model,
            "llm_model": post.llm_model,
            "image": _public_image(images_by_post_id.get(post.id), post.removed),
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
        "score": post.score,
        "user": post.user,
        # Image posts may carry no body text (content is nullable).
        "content": (
            post.content.replace("reddit", "deaddit")
            if post.content is not None
            else None
        ),
        # Soft-removed posts stay fetchable by direct ID; consumers must
        # honor the flag (the web surface renders a tombstone instead).
        "removed": bool(post.removed),
        "comment_count": comment_count,
        "comments": comment_tree,
        "image": _public_image(post.image, post.removed),
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
        # Removed comments keep their tree position (replies stay attached)
        # but their content/author are suppressed behind a tombstone marker.
        "removed": bool(comment.removed),
        "user": None if comment.removed else comment.user,
        "content": (
            "[removed]"
            if comment.removed
            else comment.content.replace("reddit", "deaddit")
        ),
        "parent_id": comment.parent_id,
        "replies": [],
    }

    for _reply_id, reply_comment in comment_map.items():
        if reply_comment.parent_id == comment.id:
            formatted_comment["replies"].append(
                format_comment(reply_comment, comment_map)
            )

    return formatted_comment


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
