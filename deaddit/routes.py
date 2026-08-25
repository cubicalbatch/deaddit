from flask import Blueprint, render_template, request
from sqlalchemy import func
from sqlalchemy.orm import aliased, joinedload

from deaddit.extensions import db

from .config import Config
from .models import Comment, Post, Subdeaddit, User
from .utils import (
    get_comment_counts_bulk,
    process_post_title,
)

bp = Blueprint("web", __name__)


@bp.route("/")
def index():
    # Check if the application needs initial setup
    needs_setup = False

    # Check if database has content and configuration is set
    total_posts = Post.query.count()
    total_users = User.query.count()
    total_subdeaddits = Subdeaddit.query.count()

    # Check if core configuration is set
    openai_key = Config.get("OPENAI_KEY")
    openai_url = Config.get("OPENAI_API_URL")

    is_configured = (
        openai_key
        and openai_key != "your_openrouter_api_key"
        and openai_url
        and openai_url != "http://localhost/v1"
    )

    # Show setup message only if database is empty AND configuration is not set
    if (
        total_posts == 0 and total_users == 0 and total_subdeaddits == 0
    ) and not is_configured:
        needs_setup = True

    if needs_setup:
        return render_template(
            "setup.html",
            title="Setup Required - Deaddit",
            description="Welcome to Deaddit! Initial setup required.",
            has_content=total_posts > 0 or total_users > 0 or total_subdeaddits > 0,
            is_configured=is_configured,
        )

    page = request.args.get("page", default=1, type=int)
    posts_per_page = 20

    # Get selected models from query parameters
    selected_models = request.args.getlist("models")

    query = Post.query
    if selected_models:
        query = query.filter(Post.model.in_(selected_models))

    total_posts = query.count()
    posts = (
        query.order_by(Post.created_at.desc(), Post.id.desc())
        .offset((page - 1) * posts_per_page)
        .limit(posts_per_page)
        .all()
    )
    has_more = total_posts > page * posts_per_page

    # Process post titles
    for post in posts:
        post.title = process_post_title(post.title)

    # Get comment counts efficiently
    post_ids = [post.id for post in posts]
    comment_counts = get_comment_counts_bulk(post_ids)

    return render_template(
        "index.html",
        posts=posts,
        comment_counts=comment_counts,
        page=page,
        has_more=has_more,
        selected_models=selected_models,
        title="Deaddit - The Reddit clone with AI users",
        description="Explore Deaddit, the AI-generated Reddit clone featuring diverse discussions and content created by artificial intelligence.",
    )


@bp.route("/d/<subdeaddit_name>")
def subdeaddit(subdeaddit_name):
    page = request.args.get("page", default=1, type=int)
    posts_per_page = 10

    # Get selected models from query parameters
    selected_models = request.args.getlist("models")

    # Check if the subdeaddit exists
    Subdeaddit.query.filter_by(name=subdeaddit_name).first_or_404()

    query = Post.query.filter_by(subdeaddit_name=subdeaddit_name)

    # Apply model filter if models are selected
    if selected_models:
        query = query.filter(Post.model.in_(selected_models))

    total_posts = query.count()
    paginated_posts = (
        query.order_by(Post.created_at.desc(), Post.id.desc())
        .offset((page - 1) * posts_per_page)
        .limit(posts_per_page)
        .all()
    )
    has_more = total_posts > page * posts_per_page

    # Process post titles
    for post in paginated_posts:
        post.title = process_post_title(post.title)

    # Get comment counts efficiently
    post_ids = [post.id for post in paginated_posts]
    comment_counts = get_comment_counts_bulk(post_ids)

    return render_template(
        "subdeaddit.html",
        posts=paginated_posts,
        comment_counts=comment_counts,
        subdeaddit_name=subdeaddit_name,
        page=page,
        has_more=has_more,
        selected_models=selected_models,
        title=f"Deaddit - d/{subdeaddit_name}",
    )


@bp.route("/d/<subdeaddit_name>/<int:post_id>")
def post(subdeaddit_name, post_id):
    post = Post.query.get_or_404(post_id)

    # Get selected models from query parameters
    selected_models = request.args.getlist("models")

    # Query all comments for this post, ordered by upvote count
    query = Comment.query.filter_by(post_id=post_id).order_by(
        Comment.upvote_count.desc()
    )

    # Apply model filter if models are selected
    if selected_models:
        query = query.filter(Comment.model.in_(selected_models))

    comments = query.all()

    def build_comment_tree(comments):
        comment_dict = {
            comment.id: {
                "id": comment.id,
                "content": comment.content,
                "upvote_count": comment.upvote_count,
                "user": comment.user,
                "model": comment.model,
                "created_at": comment.created_at,
                "children": [],
            }
            for comment in comments
        }

        root_comments = []
        for comment in comments:
            if comment.parent_id is None or comment.parent_id == "":
                root_comments.append(comment_dict[comment.id])
            else:
                parent_id = (
                    int(comment.parent_id)
                    if isinstance(comment.parent_id, str)
                    and comment.parent_id.isdigit()
                    else comment.parent_id
                )
                parent = comment_dict.get(parent_id)
                if parent:
                    parent["children"].append(comment_dict[comment.id])

        # Sort children by upvote count
        for comment in comment_dict.values():
            comment["children"].sort(key=lambda x: x["upvote_count"], reverse=True)

        # Sort root comments by upvote count
        root_comments.sort(key=lambda x: x["upvote_count"], reverse=True)

        return root_comments

    def add_comment_levels(comments, level=0):
        for comment in comments:
            comment["level"] = level
            add_comment_levels(comment["children"], level + 1)
        return comments

    # Build the comment tree
    root_comments = build_comment_tree(comments)
    comment_tree = add_comment_levels(root_comments)

    # Truncate the post title for the page title
    truncated_title = (post.title[:60] + "...") if len(post.title) > 60 else post.title

    return render_template(
        "post.html",
        post=post,
        comment_tree=comment_tree,
        subdeaddit_name=subdeaddit_name,
        selected_models=selected_models,
        title=f"Deaddit - {truncated_title}",
    )


@bp.route("/list_subdeaddit")
def list_subdeaddit():
    page = request.args.get("page", default=1, type=int)
    subdeaddits_per_page = 50

    # Get selected models from query parameters
    selected_models = request.args.getlist("models")

    # Query for total post count (all models)
    total_post_count = func.count(Post.id).label("total_post_count")

    # Subquery for filtered post count
    filtered_post_subquery = db.session.query(
        Post.subdeaddit_name, func.count(Post.id).label("filtered_post_count")
    )
    if selected_models:
        filtered_post_subquery = filtered_post_subquery.filter(
            Post.model.in_(selected_models)
        )
    filtered_post_subquery = filtered_post_subquery.group_by(
        Post.subdeaddit_name
    ).subquery()

    # Alias for the subquery
    filtered_post_alias = aliased(filtered_post_subquery)

    # Main query
    query = (
        db.session.query(
            Subdeaddit.name,
            Subdeaddit.description,
            total_post_count,
            func.coalesce(filtered_post_alias.c.filtered_post_count, 0).label(
                "filtered_post_count"
            ),
        )
        .outerjoin(Post, Subdeaddit.name == Post.subdeaddit_name)
        .outerjoin(
            filtered_post_alias,
            Subdeaddit.name == filtered_post_alias.c.subdeaddit_name,
        )
        .group_by(Subdeaddit.name, Subdeaddit.description)
        .order_by(Subdeaddit.name)
    )

    # Paginate the results
    paginated_subdeaddits = query.paginate(page=page, per_page=subdeaddits_per_page)

    return render_template(
        "list_subdeaddit.html",
        subdeaddits=paginated_subdeaddits,
        selected_models=selected_models,
        title="Deaddit - List of Subdeaddits",
    )


@bp.route("/user/<username>")
def user_profile(username):
    user = User.query.get_or_404(username)

    # Get the 20 most recent posts for the user
    posts = (
        Post.query.filter_by(user=username)
        .order_by(Post.created_at.desc())
        .limit(20)
        .all()
    )

    # Get the 20 most recent comments for the user
    comments = (
        Comment.query.options(joinedload(Comment.post))
        .filter_by(user=username)
        .order_by(Comment.created_at.desc())
        .limit(20)
        .all()
    )

    # Get total counts for posts and comments
    total_posts = Post.query.filter_by(user=username).count()
    total_comments = Comment.query.filter_by(user=username).count()

    # Get comment counts efficiently
    post_ids = [post.id for post in posts]
    comment_counts = get_comment_counts_bulk(post_ids)

    return render_template(
        "user_profile.html",
        user=user,
        posts=posts,
        comments=comments,
        total_posts=total_posts,
        total_comments=total_comments,
        comment_counts=comment_counts,
        title=f"Deaddit - User Profile: {username}",
    )


@bp.route("/users")
def list_users():
    page = request.args.get("page", default=1, type=int)
    users_per_page = 50

    # Count the total number of users
    total_users = db.session.query(func.count(User.username)).scalar()

    # Query users with pagination
    users = User.query.order_by(User.username).paginate(
        page=page, per_page=users_per_page
    )

    return render_template(
        "users_list.html",
        users=users,
        total_users=total_users,
        title="Deaddit - List of Users",
    )
