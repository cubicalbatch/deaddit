from flask import Blueprint, render_template, request
from sqlalchemy import func
from sqlalchemy.orm import joinedload

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

    query = Post.query

    sort = request.args.get("sort", "")
    if sort not in ("new", "top"):
        sort = ""

    if sort == "top":
        order_by = (Post.upvote_count.desc(), Post.id.desc())
    else:
        order_by = (Post.created_at.desc(), Post.id.desc())

    posts = (
        query.order_by(*order_by)
        .offset((page - 1) * posts_per_page)
        .limit(posts_per_page)
        .all()
    )
    has_more = total_posts > page * posts_per_page
    total_pages = (total_posts + posts_per_page - 1) // posts_per_page

    # Right-rail data: top 6 communities by post count
    post_count = func.count(Post.id).label("post_count")
    rail_rows = (
        db.session.query(Subdeaddit.name, Subdeaddit.description, post_count)
        .outerjoin(Post, Subdeaddit.name == Post.subdeaddit_name)
        .group_by(Subdeaddit.name, Subdeaddit.description)
        .order_by(post_count.desc(), Subdeaddit.name)
        .limit(6)
        .all()
    )
    rail_subs = [
        {"name": row.name, "description": row.description, "post_count": row.post_count}
        for row in rail_rows
    ]

    # Right-rail data: top 6 users by post count
    user_post_count = func.count(Post.id).label("post_count")
    user_rows = (
        db.session.query(User.username, user_post_count)
        .outerjoin(Post, User.username == Post.user)
        .group_by(User.username)
        .order_by(user_post_count.desc(), User.username)
        .limit(6)
        .all()
    )
    rail_users = [
        {"username": row.username, "post_count": row.post_count} for row in user_rows
    ]

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
        title="Deaddit - The Reddit clone with AI users",
        total_pages=total_pages,
        sort=sort,
        rail_subs=rail_subs,
        rail_users=rail_users,
        description="Explore Deaddit, the AI-generated Reddit clone featuring diverse discussions and content created by artificial intelligence.",
    )


@bp.route("/d/<subdeaddit_name>")
def subdeaddit(subdeaddit_name):
    page = request.args.get("page", default=1, type=int)
    posts_per_page = 10

    # Check if the subdeaddit exists
    community = Subdeaddit.query.filter_by(name=subdeaddit_name).first_or_404()

    query = Post.query.filter_by(subdeaddit_name=subdeaddit_name)

    total_posts = query.count()
    sort = request.args.get("sort", "")
    if sort not in ("new", "top"):
        sort = ""

    if sort == "top":
        order_by = (Post.upvote_count.desc(), Post.id.desc())
    else:
        order_by = (Post.created_at.desc(), Post.id.desc())

    paginated_posts = (
        query.order_by(*order_by)
        .offset((page - 1) * posts_per_page)
        .limit(posts_per_page)
        .all()
    )
    has_more = total_posts > page * posts_per_page
    total_pages = (total_posts + posts_per_page - 1) // posts_per_page

    # Process post titles
    for post in paginated_posts:
        post.title = process_post_title(post.title)

    # Get comment counts efficiently
    post_ids = [post.id for post in paginated_posts]
    comment_counts = get_comment_counts_bulk(post_ids)
    sub_comment_count = (
        db.session.query(func.count(Comment.id))
        .join(Post, Comment.post_id == Post.id)
        .filter(Post.subdeaddit_name == subdeaddit_name)
        .scalar()
    ) or 0

    return render_template(
        "subdeaddit.html",
        posts=paginated_posts,
        comment_counts=comment_counts,
        page=page,
        subdeaddit_name=subdeaddit_name,
        has_more=has_more,
        title=f"Deaddit - d/{subdeaddit_name}",
        community=community,
        sub_post_count=total_posts,
        sub_comment_count=sub_comment_count,
        total_pages=total_pages,
        sort=sort,
    )


@bp.route("/d/<subdeaddit_name>/<int:post_id>")
def post(subdeaddit_name, post_id):
    post = Post.query.get_or_404(post_id)

    # Query all comments for this post, ordered by upvote count
    query = Comment.query.filter_by(post_id=post_id).order_by(
        Comment.upvote_count.desc()
    )

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
        title=f"Deaddit - {truncated_title}",
    )


@bp.route("/list_subdeaddit")
def list_subdeaddit():
    page = request.args.get("page", default=1, type=int)
    subdeaddits_per_page = 50

    total_post_count = func.count(Post.id).label("total_post_count")

    query = (
        db.session.query(
            Subdeaddit.name,
            Subdeaddit.description,
            total_post_count,
        )
        .outerjoin(Post, Subdeaddit.name == Post.subdeaddit_name)
        .group_by(Subdeaddit.name, Subdeaddit.description)
        .order_by(Subdeaddit.name)
    )

    # Paginate the results
    paginated_subdeaddits = query.paginate(page=page, per_page=subdeaddits_per_page)

    return render_template(
        "list_subdeaddit.html",
        subdeaddits=paginated_subdeaddits,
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
        # post_list.html expects feed paging vars; profile lists are uncapped
        # single pages.
        page=1,
        has_more=False,
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
