import json
from collections import namedtuple

from flask import Blueprint, render_template, request
from sqlalchemy import distinct, func, or_
from sqlalchemy.orm import joinedload

from deaddit.dynamics import degeneracy
from deaddit.dynamics.ranking import (
    controversy,
    normalize_comment_sort,
    normalize_post_sort,
    post_order_by,
    rising_filter,
    up_down_split,
    wilson_lower_bound,
)
from deaddit.extensions import db

from .config import Config
from .models import Comment, Post, Subdeaddit, User
from .utils import (
    format_content_html,
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

    is_configured = bool(
        openai_key and openai_url and openai_url != "http://localhost/v1"
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

    query = Post.query.filter(Post.removed.is_(False))

    sort = normalize_post_sort(request.args.get("sort"))
    if sort == "rising":
        # Restrict the feed before count() so paging math sees the filtered set.
        query = query.filter(rising_filter())

    total_posts = query.count()

    posts = (
        query.order_by(*degeneracy.with_repetition_demotion(post_order_by(sort)))
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

    query = Post.query.filter_by(subdeaddit_name=subdeaddit_name, removed=False)

    sort = normalize_post_sort(request.args.get("sort"))
    if sort == "rising":
        # Restrict the feed before count() so paging math sees the filtered set.
        query = query.filter(rising_filter())

    total_posts = query.count()

    paginated_posts = (
        query.order_by(*degeneracy.with_repetition_demotion(post_order_by(sort)))
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


# Maximum rendered comment nesting depth; deeper replies are flattened into
# a "continue this thread" tail on their depth-cap ancestor.
DEPTH_CAP = 8


@bp.route("/d/<subdeaddit_name>/<int:post_id>")
def post(subdeaddit_name, post_id):
    post = Post.query.get_or_404(post_id)

    # Comment sort: "top" default plus new/best/controversial; garbage falls back.
    sort = normalize_comment_sort(request.args.get("sort"))

    comments = Comment.query.filter_by(post_id=post_id).all()

    def comment_rank_key(node):
        """Single python sort-key path; id tiebreak is baked into each key."""
        if sort == "new":
            return (node["created_at"], node["id"])
        up, down = up_down_split(node["score"], node["vote_count"])
        if sort == "best":
            return (-wilson_lower_bound(up, down), -node["id"])
        if sort == "controversial":
            return (-controversy(up, down), -node["id"])
        return (-node["score"], -node["id"])

    def build_comment_tree(comments):
        comment_dict = {
            comment.id: {
                "id": comment.id,
                # Tombstone convention: a removed comment keeps its node (so
                # children stay attached and thread structure survives) but
                # its content/author/score are suppressed.
                "removed": comment.removed,
                "removal_reason": comment.removal_reason if comment.removed else None,
                "content": "" if comment.removed else comment.content,
                "content_html": (
                    "" if comment.removed else format_content_html(comment.content)
                ),
                "score": 0 if comment.removed else comment.score,
                "vote_count": 0 if comment.removed else comment.vote_count,
                "user": None if comment.removed else comment.user,
                "model": None if comment.removed else comment.model,
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

        # Sort roots and children by the chosen key, with deterministic ties.
        for comment in comment_dict.values():
            comment["children"].sort(key=comment_rank_key, reverse=(sort == "new"))
        root_comments.sort(key=comment_rank_key, reverse=(sort == "new"))

        return root_comments

    def count_descendants(node):
        total = 0
        stack = list(node["children"])
        while stack:
            current = stack.pop()
            total += 1
            stack.extend(current["children"])
        return total

    def flatten_inorder(nodes, start_depth):
        """Flatten a subtree in-order; each item keeps its real absolute depth."""
        flat = []

        def walk(node, real_depth):
            node["flat"] = True
            node["level"] = real_depth  # real depth for data-depth / flat JS
            node["descendant_count"] = count_descendants(node)
            flat.append(node)
            children = node["children"]
            node["children"] = []  # subtree lives inline in tail order
            for child in children:
                walk(child, real_depth + 1)

        for node in nodes:
            walk(node, start_depth)
        return flat

    def cap_comment_depth(comments, level=0):
        """Assign levels (capped at DEPTH_CAP) and flatten deep tails in-order.

        ``descendant_count`` is computed over the FULL subtree before any
        capping; capping affects structure only — all rows are already loaded.
        """
        for comment in comments:
            comment["level"] = min(level, DEPTH_CAP)  # informational
            comment["descendant_count"] = count_descendants(comment)
            if level >= DEPTH_CAP and comment["children"]:
                comment["tail"] = flatten_inorder(comment["children"], level + 1)
                comment["children"] = []  # NOT rendered nested
            else:
                cap_comment_depth(comment["children"], level + 1)
        return comments

    # Build the comment tree, then cap nesting depth and flatten the tails.
    root_comments = build_comment_tree(comments)
    comment_tree = cap_comment_depth(root_comments)

    # Truncate the post title for the page title; a removed post keeps its
    # direct-link reachability but its title must not leak anywhere.
    truncated_title = (
        "removed post"
        if post.removed
        else (post.title[:60] + "...")
        if len(post.title) > 60
        else post.title
    )

    return render_template(
        "post.html",
        post=post,
        comment_tree=comment_tree,
        sort=sort,
        # Direct links to removed posts stay reachable: the template renders
        # a tombstone notice instead of title/body, comments stay visible.
        post_body_html=("" if post.removed else format_content_html(post.content)),
        removal_reason=post.removal_reason,
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

    tab = request.args.get("tab", "posts")
    if tab not in ("posts", "comments"):
        tab = "posts"
    page = request.args.get("page", default=1, type=int)
    per_page = 20
    offset = (page - 1) * per_page

    total_posts = Post.query.filter_by(user=username).count()
    total_comments = Comment.query.filter_by(user=username).count()
    # Listings/pagination exclude removed rows; profile stats keep counting
    # everything (soft removal must not corrupt the displayed totals).
    visible_posts = Post.query.filter_by(user=username, removed=False).count()
    visible_comments = Comment.query.filter_by(user=username, removed=False).count()

    post_upvotes = (
        db.session.query(func.coalesce(func.sum(Post.score), 0))
        .filter(Post.user == username)
        .scalar()
    )
    comment_upvotes = (
        db.session.query(func.coalesce(func.sum(Comment.score), 0))
        .filter(Comment.user == username)
        .scalar()
    )
    stats = {
        "post_count": total_posts,
        "comment_count": total_comments,
        "total_upvotes": post_upvotes + comment_upvotes,
    }

    def _safe_json_list(raw):
        """Parse a JSON list column, falling back to [] on NULL/invalid."""
        try:
            parsed = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []

    context = {
        "user": user,
        "active_tab": tab,
        "page": page,
        "total_posts": total_posts,
        "total_comments": total_comments,
        "stats": stats,
        "traits": _safe_json_list(user.personality_traits),
        "interests": _safe_json_list(user.interests),
        "bio_html": format_content_html(user.bio),
        "title": f"Deaddit - User Profile: {username}",
    }

    if tab == "posts":
        posts = (
            Post.query.filter_by(user=username, removed=False)
            .order_by(Post.created_at.desc(), Post.id.desc())
            .offset(offset)
            .limit(per_page)
            .all()
        )
        for post in posts:
            post.title = process_post_title(post.title)
        context["posts"] = posts
        context["comment_counts"] = get_comment_counts_bulk([post.id for post in posts])
        total = visible_posts
    else:
        context["comments"] = (
            Comment.query.options(joinedload(Comment.post))
            .filter_by(user=username, removed=False)
            .order_by(Comment.created_at.desc(), Comment.id.desc())
            .offset(offset)
            .limit(per_page)
            .all()
        )
        total = visible_comments

    context["total_pages"] = (total + per_page - 1) // per_page
    context["has_more"] = total > page * per_page

    return render_template("user_profile.html", **context)


_UserRow = namedtuple(
    "UserRow",
    ["username", "bio", "age", "gender", "post_count", "comment_count", "activity"],
)


@bp.route("/users")
def list_users():
    page = request.args.get("page", default=1, type=int)
    sort = request.args.get("sort", "username")
    if sort not in ("username", "activity"):
        sort = "username"
    users_per_page = 50
    offset = (page - 1) * users_per_page

    total_users = db.session.query(func.count(User.username)).scalar()

    # Two independent outerjoins multiply post/comment rows per user, so the
    # counts must be DISTINCT to stay accurate.
    pc = func.count(distinct(Post.id))
    cc = func.count(distinct(Comment.id))
    query = (
        db.session.query(
            User.username,
            User.bio,
            User.age,
            User.gender,
            pc.label("post_count"),
            cc.label("comment_count"),
        )
        .outerjoin(Post, Post.user == User.username)
        .outerjoin(Comment, Comment.user == User.username)
        .group_by(User.username, User.bio, User.age, User.gender)
    )
    if sort == "activity":
        query = query.order_by((pc + cc).desc(), User.username.asc())
    else:
        query = query.order_by(User.username.asc())

    rows = query.offset(offset).limit(users_per_page).all()
    users = [
        _UserRow(
            username=row.username,
            bio=row.bio,
            age=row.age,
            gender=row.gender,
            post_count=row.post_count,
            comment_count=row.comment_count,
            activity=row.post_count + row.comment_count,
        )
        for row in rows
    ]

    return render_template(
        "users_list.html",
        users=users,
        sort=sort,
        page=page,
        total_pages=(total_users + users_per_page - 1) // users_per_page,
        has_more=total_users > page * users_per_page,
        total_users=total_users,
        title="Deaddit - List of Users",
    )


@bp.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    page = request.args.get("page", default=1, type=int)
    posts_per_page = 20
    offset = (page - 1) * posts_per_page

    posts = []
    comment_counts = {}
    communities = []
    people = []
    total_posts = 0

    if q:
        # contains(autoescape=True) escapes %, _ and the escape char inside
        # q before SQLAlchemy wraps it in %...% for LIKE.
        def like(col):
            return col.contains(q, autoescape=True)

        posts_query = Post.query.filter(
            Post.removed.is_(False),
            or_(like(Post.title), like(Post.content)),
        ).order_by(Post.created_at.desc(), Post.id.desc())
        total_posts = posts_query.count()
        posts = posts_query.offset(offset).limit(posts_per_page).all()
        for post in posts:
            post.title = process_post_title(post.title)
        comment_counts = get_comment_counts_bulk([post.id for post in posts])

        sub_pc = func.count(Post.id)
        communities_rows = (
            db.session.query(
                Subdeaddit.name,
                Subdeaddit.description,
                sub_pc.label("post_count"),
            )
            .outerjoin(Post, Subdeaddit.name == Post.subdeaddit_name)
            .filter(or_(like(Subdeaddit.name), like(Subdeaddit.description)))
            .group_by(Subdeaddit.name, Subdeaddit.description)
            .order_by(sub_pc.desc(), Subdeaddit.name.asc())
            .limit(8)
            .all()
        )
        communities = communities_rows

        upc = func.count(distinct(Post.id))
        ucc = func.count(distinct(Comment.id))
        people_rows = (
            db.session.query(
                User.username,
                User.bio,
                upc.label("post_count"),
                ucc.label("comment_count"),
            )
            .outerjoin(Post, Post.user == User.username)
            .outerjoin(Comment, Comment.user == User.username)
            .filter(or_(like(User.username), like(User.bio)))
            .group_by(User.username, User.bio)
            .order_by((upc + ucc).desc(), User.username.asc())
            .limit(8)
            .all()
        )
        people = people_rows

    return render_template(
        "search.html",
        q=q,
        posts=posts,
        comment_counts=comment_counts,
        communities=communities,
        people=people,
        total_posts=total_posts,
        page=page,
        total_pages=(total_posts + posts_per_page - 1) // posts_per_page,
        has_more=total_posts > page * posts_per_page,
        title=f"Deaddit - Search: {q}" if q else "Deaddit - Search",
        description="Search Deaddit posts, communities, and people.",
    )
