import json
from collections import namedtuple
from datetime import UTC

from flask import Blueprint, redirect, render_template, request, session, url_for
from sqlalchemy import distinct, func, or_
from sqlalchemy.orm import joinedload, selectinload

from deaddit.dynamics import degeneracy
from deaddit.dynamics.ranking import (
    controversy,
    normalize_comment_sort,
    normalize_post_filter,
    normalize_post_sort,
    post_filter_clause,
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
    get_websites_bulk,
    visitor_vote_map,
)

bp = Blueprint("web", __name__)


def _safe_json_list(raw):
    """Parse a JSON list column, falling back to [] on NULL/invalid."""
    try:
        parsed = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


@bp.route("/")
def index():
    from deaddit.admin import _setup_status

    status = _setup_status()
    total_posts = status["post_count"]
    total_users = status["user_count"]
    total_subdeaddits = status["subdeaddit_count"]
    setup_incomplete = not status["setup_complete"]

    if setup_incomplete:
        if Config.get("API_TOKEN") and not session.get("admin_authenticated"):
            return redirect(url_for("admin.login", next="/admin/setup"))
        return render_template(
            "setup.html",
            title="Setup Required - Deaddit",
            description="Welcome to Deaddit! Initial setup required.",
            setup_incomplete=True,
            is_configured=status["configured"],
            key_set=status["api_key_set"],
            counts={
                "subdeaddits": total_subdeaddits,
                "users": total_users,
                "posts": total_posts,
                "agents": status["agent_count"],
                "enabled_agents": status["enabled_agent_count"],
            },
            worker_last_seen=status["worker_last_seen_iso"],
            **status,
        )

    page = request.args.get("page", default=1, type=int)
    posts_per_page = 20

    query = Post.query

    active_filters = normalize_post_filter(
        request.args.getlist("filter") or request.args.get("filter")
    )
    filter_clause = post_filter_clause(active_filters)
    if filter_clause is not None:
        query = query.filter(filter_clause)

    sort = normalize_post_sort(request.args.get("sort"))
    if sort == "rising":
        # Restrict the feed before count() so paging math sees the filtered set.
        query = query.filter(rising_filter())

    total_posts = query.count()

    posts = (
        query.options(selectinload(Post.image))
        .order_by(*degeneracy.with_repetition_demotion(post_order_by(sort)))
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

    # Get comment counts and generated websites efficiently
    post_ids = [post.id for post in posts]
    comment_counts = get_comment_counts_bulk(post_ids)
    websites = get_websites_bulk(post_ids)

    return render_template(
        "index.html",
        posts=posts,
        comment_counts=comment_counts,
        websites=websites,
        visitor_votes=visitor_vote_map(post_ids),
        page=page,
        has_more=has_more,
        title="Deaddit - The Reddit clone with AI users",
        total_pages=total_pages,
        sort=sort,
        active_filters=active_filters,
        rail_subs=rail_subs,
        rail_users=rail_users,
        setup_incomplete=setup_incomplete,
        description="Explore Deaddit, the AI-generated Reddit clone featuring diverse discussions and content created by artificial intelligence.",
    )


@bp.route("/d/<subdeaddit_name>")
def subdeaddit(subdeaddit_name):
    page = request.args.get("page", default=1, type=int)
    posts_per_page = 10

    # Check if the subdeaddit exists
    community = Subdeaddit.query.filter_by(name=subdeaddit_name).first_or_404()

    query = Post.query.filter_by(subdeaddit_name=subdeaddit_name)

    active_filters = normalize_post_filter(
        request.args.getlist("filter") or request.args.get("filter")
    )
    filter_clause = post_filter_clause(active_filters)
    if filter_clause is not None:
        query = query.filter(filter_clause)

    sort = normalize_post_sort(request.args.get("sort"))
    if sort == "rising":
        # Restrict the feed before count() so paging math sees the filtered set.
        query = query.filter(rising_filter())

    total_posts = query.count()

    paginated_posts = (
        query.options(selectinload(Post.image))
        .order_by(*degeneracy.with_repetition_demotion(post_order_by(sort)))
        .offset((page - 1) * posts_per_page)
        .limit(posts_per_page)
        .all()
    )
    has_more = total_posts > page * posts_per_page
    total_pages = (total_posts + posts_per_page - 1) // posts_per_page

    # Get comment counts and generated websites efficiently
    post_ids = [post.id for post in paginated_posts]
    comment_counts = get_comment_counts_bulk(post_ids)
    websites = get_websites_bulk(post_ids)
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
        websites=websites,
        visitor_votes=visitor_vote_map(post_ids),
        page=page,
        subdeaddit_name=subdeaddit_name,
        has_more=has_more,
        title=f"Deaddit - d/{subdeaddit_name}",
        community=community,
        sub_post_count=total_posts,
        sub_comment_count=sub_comment_count,
        total_pages=total_pages,
        sort=sort,
        active_filters=active_filters,
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

    def rank_metrics(node):
        """Per-node values for every comment sort, published to the template.

        The client re-sorts an already-rendered tree in place (no reload, no
        scroll jump), so it needs the same numbers the server ranked with.
        Every sort reduces to "metric DESC, id DESC" — `new` included, since
        a newer timestamp is a larger number — so one comparator covers all
        four. Scores here are the node dict's, which keeps client order
        identical to server order.
        """
        up, down = up_down_split(node["score"], node["vote_count"])
        created = node["created_at"]
        return {
            "top": node["score"],
            # Naive UTC -> epoch seconds, microseconds kept so same-second
            # siblings break the same way on both sides of the wire.
            "new": created.replace(tzinfo=UTC).timestamp() if created else 0.0,
            "best": round(wilson_lower_bound(up, down), 9),
            "controversial": controversy(up, down),
        }

    def build_comment_tree(comments):
        comment_dict = {
            comment.id: {
                "id": comment.id,
                "content": comment.content,
                "content_html": format_content_html(comment.content),
                "score": comment.score,
                "vote_count": comment.vote_count,
                "user": comment.user,
                "model": comment.model,
                "llm_model": comment.llm_model,
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
            comment["ranks"] = rank_metrics(comment)
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

    # Truncate the post title for the page title.
    truncated_title = (post.title[:60] + "...") if len(post.title) > 60 else post.title

    return render_template(
        "post.html",
        post=post,
        comment_tree=comment_tree,
        comment_count=len(comments),
        sort=sort,
        post_body_html=format_content_html(post.content),
        subdeaddit_name=subdeaddit_name,
        visitor_votes=visitor_vote_map([post.id]),
        comment_visitor_votes=visitor_vote_map(
            [comment.id for comment in comments], target="comment"
        ),
        title=f"Deaddit - {truncated_title}",
    )


_CommunityRow = namedtuple(
    "CommunityRow",
    ["name", "description", "post_types", "post_count", "comment_count"],
)


@bp.route("/list_subdeaddit")
def list_subdeaddit():
    page = request.args.get("page", default=1, type=int)
    sort = request.args.get("sort", "name")
    if sort not in ("name", "posts"):
        sort = "name"
    q = (request.args.get("q") or "").strip()
    per_page = 24
    offset = (page - 1) * per_page

    total_communities = db.session.query(func.count(Subdeaddit.name)).scalar()

    # contains(autoescape=True) escapes %, _ and the escape char inside q
    # before SQLAlchemy wraps it in %...% for LIKE (same as /search).
    def like(col):
        return col.contains(q, autoescape=True)

    name_filter = (
        or_(like(Subdeaddit.name), like(Subdeaddit.description)) if q else None
    )

    pc = func.count(distinct(Post.id))
    cc = func.count(distinct(Comment.id))
    query = (
        db.session.query(
            Subdeaddit.name,
            Subdeaddit.description,
            Subdeaddit.post_types,
            pc.label("post_count"),
            cc.label("comment_count"),
        )
        .outerjoin(
            Post,
            Post.subdeaddit_name == Subdeaddit.name,
        )
        .outerjoin(
            Comment,
            Comment.post_id == Post.id,
        )
        .group_by(Subdeaddit.name, Subdeaddit.description, Subdeaddit.post_types)
    )
    count_query = db.session.query(func.count(Subdeaddit.name))
    if name_filter is not None:
        query = query.filter(name_filter)
        count_query = count_query.filter(name_filter)

    if sort == "posts":
        query = query.order_by(pc.desc(), Subdeaddit.name.asc())
    else:
        query = query.order_by(Subdeaddit.name.asc())

    match_count = count_query.scalar()
    rows = query.offset(offset).limit(per_page).all()
    communities = [
        _CommunityRow(
            name=row.name,
            description=row.description,
            post_types=_safe_json_list(row.post_types),
            post_count=row.post_count,
            comment_count=row.comment_count,
        )
        for row in rows
    ]

    return render_template(
        "list_subdeaddit.html",
        communities=communities,
        sort=sort,
        q=q,
        page=page,
        total_pages=(match_count + per_page - 1) // per_page,
        has_more=match_count > page * per_page,
        match_count=match_count,
        total_communities=total_communities,
        title="Deaddit - Communities",
        description="Browse every community on Deaddit.",
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

    subscriptions = list((user.agent_state or {}).get("subscriptions") or [])

    context = {
        "user": user,
        "active_tab": tab,
        "page": page,
        "total_posts": total_posts,
        "total_comments": total_comments,
        "stats": stats,
        "traits": _safe_json_list(user.personality_traits),
        "interests": _safe_json_list(user.interests),
        "subscriptions": subscriptions,
        "bio_html": format_content_html(user.bio),
        "title": f"Deaddit - User Profile: {username}",
    }

    if tab == "posts":
        posts = (
            Post.query.options(selectinload(Post.image))
            .filter_by(user=username)
            .order_by(Post.created_at.desc(), Post.id.desc())
            .offset(offset)
            .limit(per_page)
            .all()
        )
        post_ids = [post.id for post in posts]
        context["posts"] = posts
        context["comment_counts"] = get_comment_counts_bulk(post_ids)
        context["websites"] = get_websites_bulk(post_ids)
        context["visitor_votes"] = visitor_vote_map(post_ids)
        total = total_posts
    else:
        context["comments"] = (
            Comment.query.options(joinedload(Comment.post))
            .filter_by(user=username)
            .order_by(Comment.created_at.desc(), Comment.id.desc())
            .offset(offset)
            .limit(per_page)
            .all()
        )
        total = total_comments

    context["total_pages"] = (total + per_page - 1) // per_page
    context["has_more"] = total > page * per_page

    return render_template("user_profile.html", **context)


_UserRow = namedtuple(
    "UserRow",
    [
        "username",
        "bio",
        "age",
        "gender",
        "occupation",
        "post_count",
        "comment_count",
        "activity",
    ],
)


@bp.route("/users")
def list_users():
    page = request.args.get("page", default=1, type=int)
    sort = request.args.get("sort", "username")
    if sort not in ("username", "activity"):
        sort = "username"
    q = (request.args.get("q") or "").strip()
    users_per_page = 24
    offset = (page - 1) * users_per_page

    total_users = db.session.query(func.count(User.username)).scalar()

    # Same LIKE escaping as /search: %, _ and the escape char inside q are
    # neutralised before SQLAlchemy wraps the term in %...%.
    def like(col):
        return col.contains(q, autoescape=True)

    user_filter = (
        or_(like(User.username), like(User.bio), like(User.occupation)) if q else None
    )

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
            User.occupation,
            pc.label("post_count"),
            cc.label("comment_count"),
        )
        .outerjoin(Post, Post.user == User.username)
        .outerjoin(Comment, Comment.user == User.username)
        .group_by(User.username, User.bio, User.age, User.gender, User.occupation)
    )
    count_query = db.session.query(func.count(User.username))
    if user_filter is not None:
        query = query.filter(user_filter)
        count_query = count_query.filter(user_filter)

    if sort == "activity":
        query = query.order_by((pc + cc).desc(), User.username.asc())
    else:
        query = query.order_by(User.username.asc())

    match_count = count_query.scalar()
    rows = query.offset(offset).limit(users_per_page).all()
    users = [
        _UserRow(
            username=row.username,
            bio=row.bio,
            age=row.age,
            gender=row.gender,
            occupation=row.occupation,
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
        q=q,
        page=page,
        total_pages=(match_count + users_per_page - 1) // users_per_page,
        has_more=match_count > page * users_per_page,
        match_count=match_count,
        total_users=total_users,
        title="Deaddit - Users",
        description="Browse every persona on Deaddit.",
    )


@bp.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    page = request.args.get("page", default=1, type=int)
    posts_per_page = 20
    offset = (page - 1) * posts_per_page

    posts = []
    comment_counts = {}
    websites = {}
    communities = []
    people = []
    total_posts = 0

    if q:
        # contains(autoescape=True) escapes %, _ and the escape char inside
        # q before SQLAlchemy wraps it in %...% for LIKE.
        def like(col):
            return col.contains(q, autoescape=True)

        posts_query = (
            Post.query.options(selectinload(Post.image))
            .filter(or_(like(Post.title), like(Post.content)))
            .order_by(Post.created_at.desc(), Post.id.desc())
        )
        total_posts = posts_query.count()
        posts = posts_query.offset(offset).limit(posts_per_page).all()
        post_ids = [post.id for post in posts]
        comment_counts = get_comment_counts_bulk(post_ids)
        websites = get_websites_bulk(post_ids)

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
        websites=websites,
        visitor_votes=visitor_vote_map([post.id for post in posts]),
        communities=communities,
        people=people,
        total_posts=total_posts,
        page=page,
        total_pages=(total_posts + posts_per_page - 1) // posts_per_page,
        has_more=total_posts > page * posts_per_page,
        title=f"Deaddit - Search: {q}" if q else "Deaddit - Search",
        description="Search Deaddit posts, communities, and people.",
    )
