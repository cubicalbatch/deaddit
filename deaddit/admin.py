"""
Admin interface for Deaddit content management.
Provides web-based UI for job management and content generation.
"""

import base64
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Blueprint,
    current_app,
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
from deaddit.agents.executor import normalize_persona_rate_caps
from deaddit.config import Config
from deaddit.dynamics.engagement import (
    SUPPORTED_ALGORITHM_VERSIONS,
    preset_config,
    validate_policy,
)
from deaddit.images import client as image_client
from deaddit.images import service as media_service
from deaddit.images import verification as image_verification
from deaddit.images.types import ImageProviderError
from deaddit.llm import routing
from deaddit.llm.capabilities import (
    probe_endpoint,
    probe_vision,
    set_manual_override,
    set_vision_manual_override,
)
from deaddit.models import (
    Agent,
    AgentMemory,
    AgentRun,
    AgentTurn,
    ApiEndpointConfig,
    ApiModel,
    Ban,
    Comment,
    DegeneracyFlag,
    EndpointCapability,
    GeneratedWebsite,
    ImageModel,
    ImageProvider,
    Job,
    JobLog,
    LLMProvider,
    LLMUsage,
    ModelRoute,
    Notification,
    Post,
    PostImage,
    PromptPin,
    PromptRenderAudit,
    PromptTemplate,
    PromptTemplateVersion,
    Report,
    Setting,
    Subdeaddit,
    SubdeadditModerator,
    ToolCall,
    User,
    Vote,
    VoteCadencePolicy,
    VoteSimulationHourly,
)
from deaddit.services.content import (
    ContentValidationError,
    create_subdeaddit,
    create_user,
)
from deaddit.settings import SecretNotPersistable
from deaddit.utils import production_disabled
from deaddit.websites import service as website_service

logger = logging.getLogger(__name__)


def _chunked(iterable, size=500):
    """Yield successive chunks from iterable."""
    lst = list(iterable)
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def _delete_post_notifications(post_ids):
    """Delete notifications that would block hard deletion of these posts."""
    post_ids = list(post_ids)
    if post_ids:
        for chunk in _chunked(post_ids, 500):
            Notification.query.filter(Notification.post_id.in_(chunk)).delete(
                synchronize_session=False
            )


def _delete_post_reports(post_ids):
    """Delete reports that would block hard deletion of these posts."""
    post_ids = list(post_ids)
    if post_ids:
        for chunk in _chunked(post_ids, 500):
            Report.query.filter(Report.post_id.in_(chunk)).delete(
                synchronize_session=False
            )


def _get_comment_ids_with_descendants(root_comment_ids):
    """Recursively collect all descendant comment IDs for the given comment IDs."""
    all_ids = set(root_comment_ids)
    current_ids = set(root_comment_ids)
    while current_ids:
        new_child_ids = set()
        for chunk in _chunked(list(current_ids), 500):
            child_rows = (
                Comment.query.filter(Comment.parent_id.in_(chunk))
                .with_entities(Comment.id)
                .all()
            )
            for row in child_rows:
                if row.id not in all_ids:
                    new_child_ids.add(row.id)
        if not new_child_ids:
            break
        all_ids.update(new_child_ids)
        current_ids = new_child_ids
    return list(all_ids)


def _delete_comments(comment_ids):
    """Delete comments and all child comments/responses, plus their votes, notifications, reports."""
    comment_ids = list(comment_ids)
    if not comment_ids:
        return 0
    all_comment_ids = _get_comment_ids_with_descendants(comment_ids)
    if not all_comment_ids:
        return 0

    for chunk in _chunked(all_comment_ids, 500):
        Vote.query.filter(Vote.comment_id.in_(chunk)).delete(synchronize_session=False)
        Notification.query.filter(Notification.comment_id.in_(chunk)).delete(
            synchronize_session=False
        )
        Report.query.filter(Report.comment_id.in_(chunk)).delete(
            synchronize_session=False
        )
        Comment.query.filter(Comment.id.in_(chunk)).update(
            {Comment.parent_id: None}, synchronize_session=False
        )
        Comment.query.filter(Comment.id.in_(chunk)).delete(synchronize_session=False)
    return len(all_comment_ids)


def _delete_posts_cascade(post_ids):
    """Delete posts and all associated comments, images, websites, votes, notifications, reports."""
    post_ids = list(post_ids)
    if not post_ids:
        return 0, 0

    post_comment_ids = []
    for chunk in _chunked(post_ids, 500):
        post_comment_ids.extend(
            row.id
            for row in Comment.query.filter(Comment.post_id.in_(chunk)).with_entities(
                Comment.id
            )
        )
    deleted_comments_count = _delete_comments(post_comment_ids)

    for chunk in _chunked(post_ids, 500):
        _delete_post_notifications(chunk)
        _delete_post_reports(chunk)
        Vote.query.filter(Vote.post_id.in_(chunk)).delete(synchronize_session=False)
        PostImage.query.filter(PostImage.post_id.in_(chunk)).delete(
            synchronize_session=False
        )
        GeneratedWebsite.query.filter(GeneratedWebsite.post_id.in_(chunk)).delete(
            synchronize_session=False
        )
        posts = Post.query.filter(Post.id.in_(chunk)).all()
        for p in posts:
            db.session.delete(p)

    return len(post_ids), deleted_comments_count


def _delete_users_cascade(usernames):
    """Delete one or more users and all their posts, comments, responses to comments, votes, moderation records, etc."""
    usernames = list(usernames)
    if not usernames:
        return {
            "users": 0,
            "posts": 0,
            "comments": 0,
            "media_paths": [],
            "website_paths": [],
        }

    # Find all posts authored by these users
    post_ids = []
    for chunk in _chunked(usernames, 500):
        post_ids.extend(
            row.id
            for row in Post.query.filter(Post.user.in_(chunk)).with_entities(Post.id)
        )
    media_paths = media_service.media_paths_for_posts(post_ids)
    website_paths = website_service.website_paths_for_posts(post_ids)

    # 1. Collect all comments authored by the user(s) AND all comments on the users' posts
    user_comment_ids = []
    for chunk in _chunked(usernames, 500):
        user_comment_ids.extend(
            row.id
            for row in Comment.query.filter(Comment.user.in_(chunk)).with_entities(
                Comment.id
            )
        )
    post_comment_ids = []
    if post_ids:
        for chunk in _chunked(post_ids, 500):
            post_comment_ids.extend(
                row.id
                for row in Comment.query.filter(
                    Comment.post_id.in_(chunk)
                ).with_entities(Comment.id)
            )
    all_root_comment_ids = list(set(user_comment_ids + post_comment_ids))

    # 2. Delete all comments and their descendant responses, votes, notifications, reports
    total_comments_deleted = _delete_comments(all_root_comment_ids)

    # 3. Delete the users' posts
    if post_ids:
        for chunk in _chunked(post_ids, 500):
            _delete_post_notifications(chunk)
            _delete_post_reports(chunk)
            Vote.query.filter(Vote.post_id.in_(chunk)).delete(synchronize_session=False)
            PostImage.query.filter(PostImage.post_id.in_(chunk)).delete(
                synchronize_session=False
            )
            GeneratedWebsite.query.filter(GeneratedWebsite.post_id.in_(chunk)).delete(
                synchronize_session=False
            )
            Post.query.filter(Post.id.in_(chunk)).delete(synchronize_session=False)

    for chunk in _chunked(usernames, 500):
        # 4. Clean up moderator removed_by FK on remaining posts/comments
        Post.query.filter(Post.removed_by.in_(chunk)).update(
            {Post.removed_by: None}, synchronize_session=False
        )
        Comment.query.filter(Comment.removed_by.in_(chunk)).update(
            {Comment.removed_by: None}, synchronize_session=False
        )

        # 5. Clean up votes cast by the user(s) on ANY remaining posts/comments
        Vote.query.filter(Vote.voter.in_(chunk)).delete(synchronize_session=False)

        # 6. Clean up notifications where the user is recipient or actor
        Notification.query.filter(Notification.recipient.in_(chunk)).delete(
            synchronize_session=False
        )
        Notification.query.filter(Notification.actor.in_(chunk)).delete(
            synchronize_session=False
        )

        # 7. Clean up reports filed by or resolved by the user
        Report.query.filter(Report.reporter.in_(chunk)).delete(
            synchronize_session=False
        )
        Report.query.filter(Report.resolved_by.in_(chunk)).update(
            {Report.resolved_by: None}, synchronize_session=False
        )

        # 8. Clean up subdeaddit moderators and bans
        SubdeadditModerator.query.filter(
            SubdeadditModerator.username.in_(chunk)
        ).delete(synchronize_session=False)
        Ban.query.filter(Ban.username.in_(chunk)).delete(synchronize_session=False)

        # 9. Clean up agents and agent runtime data
        AgentMemory.query.filter(AgentMemory.user_username.in_(chunk)).delete(
            synchronize_session=False
        )
        agent_run_ids = [
            row.id
            for row in AgentRun.query.filter(
                AgentRun.persona_username.in_(chunk)
            ).with_entities(AgentRun.id)
        ]
        if agent_run_ids:
            for r_chunk in _chunked(agent_run_ids, 500):
                ToolCall.query.filter(ToolCall.run_id.in_(r_chunk)).delete(
                    synchronize_session=False
                )
                AgentTurn.query.filter(AgentTurn.run_id.in_(r_chunk)).delete(
                    synchronize_session=False
                )
                AgentRun.query.filter(AgentRun.id.in_(r_chunk)).delete(
                    synchronize_session=False
                )

        agents = Agent.query.filter(Agent.user_username.in_(chunk)).all()
        for agent in agents:
            agent_runs = [
                row.id
                for row in AgentRun.query.filter_by(agent_id=agent.id).with_entities(
                    AgentRun.id
                )
            ]
            if agent_runs:
                for r_chunk in _chunked(agent_runs, 500):
                    ToolCall.query.filter(ToolCall.run_id.in_(r_chunk)).delete(
                        synchronize_session=False
                    )
                    AgentTurn.query.filter(AgentTurn.run_id.in_(r_chunk)).delete(
                        synchronize_session=False
                    )
                    AgentRun.query.filter(AgentRun.id.in_(r_chunk)).delete(
                        synchronize_session=False
                    )
            db.session.delete(agent)

        # 10. Delete the user records themselves
        users = User.query.filter(User.username.in_(chunk)).all()
        for u in users:
            db.session.delete(u)

    return {
        "users": len(usernames),
        "posts": len(post_ids),
        "comments": total_comments_deleted,
        "media_paths": media_paths,
        "website_paths": website_paths,
    }


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


# ---------------------------------------------------------------------------
# Simulated voting administration

_VOTING_MODE_SETTING = "SIMULATED_VOTING_MODE"
_VOTING_MODES = frozenset({"off", "shadow", "live"})
_VOTING_PRESET_COPY = {
    "quiet": "Sparse and slower; many items receive little activity.",
    "natural": (
        "Balanced everyday activity with most votes in the first few hours "
        "and a small rediscovery tail."
    ),
    "busy": "Fast, high-volume activity with more popular outliers.",
}
_VOTING_PRESET_LABELS = {"quiet": "Quiet", "natural": "Natural", "busy": "Busy"}


def _normalize_voting_mode(value):
    """Use the same fail-closed mode interpretation as the worker."""
    mode = (value or "").strip().lower() if isinstance(value, str) else ""
    return mode if mode in _VOTING_MODES else "off"


def _policy_validation_errors(config):
    """Return API-friendly field errors without mutating or persisting config."""
    try:
        validate_policy(config)
    except (TypeError, ValueError) as exc:
        message = str(exc)
        match = re.search(r"((?:post|comment|voter|direction)\.[a-z_]+)", message)
        if match:
            return {match.group(1): message}
        if "minimum downvote probability" in message:
            return {"direction.minimum_downvote_probability": message}
        return {"policy": message}
    return {}


def _policy_columns(*, before=None, limit=None):
    """Read policy columns without triggering ORM load hooks on bad rows."""
    query = db.session.query(
        VoteCadencePolicy.id,
        VoteCadencePolicy.preset,
        VoteCadencePolicy.algorithm_version,
        VoteCadencePolicy.config,
        VoteCadencePolicy.effective_at,
        VoteCadencePolicy.created_at,
    )
    if before is not None:
        query = query.filter(VoteCadencePolicy.effective_at <= before)
    query = query.order_by(
        VoteCadencePolicy.effective_at.desc(), VoteCadencePolicy.id.desc()
    )
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def _policy_record(row, *, include_config=True):
    """Serialize a validated policy column row for the admin surface."""
    if row.preset not in VoteCadencePolicy.VALID_PRESETS:
        raise ValueError(f"invalid policy preset {row.preset}")
    if row.algorithm_version not in SUPPORTED_ALGORITHM_VERSIONS:
        raise ValueError(
            f"unsupported policy algorithm version {row.algorithm_version}"
        )
    config = validate_policy(row.config)
    record = {
        "id": row.id,
        "preset": row.preset,
        "label": _VOTING_PRESET_LABELS.get(row.preset, "Custom"),
        "algorithm_version": row.algorithm_version,
        "effective_at": row.effective_at.isoformat() if row.effective_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    if include_config:
        record["config"] = config
    return record


def _voting_preview(config):
    """Calculate a readable, deterministic estimate from a policy config."""
    result = {}
    checkpoints = (
        ("15_minutes", 15.0),
        ("1_hour", 60.0),
        ("6_hours", 360.0),
    )
    for target_type in ("post", "comment"):
        section = config[target_type]
        mean = float(section["mean_active_votes"])
        half_life = float(section["half_life_minutes"])
        window_minutes = float(section["active_window_hours"]) * 60.0
        denominator = 1.0 - 2.0 ** (-window_minutes / half_life)
        cumulative = {}
        for name, age_minutes in checkpoints:
            fraction = (
                (1.0 - 2.0 ** (-age_minutes / half_life)) / denominator
                if age_minutes < window_minutes
                else 1.0
            )
            cumulative[name] = round(min(mean, mean * fraction), 2)
        cumulative["active_window_end"] = round(mean, 2)
        # Tail exposure cadence is intentionally not assumed here.  The API
        # reports the active budget plus the per-exposure rediscovery chance.
        cumulative["long_tail"] = round(mean, 2)
        result[target_type] = {
            "expected_votes": round(mean, 2),
            "cumulative_votes": cumulative,
            "active_window_hours": section["active_window_hours"],
            "tail_half_life_days": section["tail_half_life_days"],
            "tail_vote_probability_per_exposure": section[
                "tail_vote_probability_per_exposure"
            ],
        }
    result["eligibility"] = {
        "active": (
            "Active cadence applies to content created after the policy is "
            "saved and ends at the item's active-window boundary."
        ),
        "tail": (
            "Tail cadence applies to future archive or revival exposures, "
            "including content created under an older policy."
        ),
        "old_content": "Old content can still receive rare rediscovery votes.",
    }
    return result


def _voting_health():
    since = datetime.utcnow() - timedelta(hours=24)
    rows = VoteSimulationHourly.query.filter(VoteSimulationHourly.hour >= since).all()
    counters = {
        "ticks": 0,
        "errors": 0,
        "active_proposals": 0,
        "archive_proposals": 0,
        "revival_proposals": 0,
        "inserted_votes": 0,
        "switched_votes": 0,
        "upvotes": 0,
        "downvotes": 0,
        "cap_skips": 0,
        "min_gap_skips": 0,
        "no_voter_skips": 0,
        "guardrail_skips": 0,
    }
    for row in rows:
        for name in counters:
            counters[name] += getattr(row, name) or 0
    latest = VoteSimulationHourly.query.order_by(
        VoteSimulationHourly.updated_at.desc()
    ).first()
    direction_total = counters["upvotes"] + counters["downvotes"]
    simulated_votes = counters["inserted_votes"] + counters["switched_votes"]
    latest_cap_skips = latest.cap_skips if latest else 0
    return {
        "period_hours": 24,
        "simulated_votes": simulated_votes,
        "last_24h_simulated_votes": simulated_votes,
        "active_decisions": counters["active_proposals"],
        "archive_decisions": counters["archive_proposals"],
        "revival_decisions": counters["revival_proposals"],
        "active": counters["active_proposals"],
        "archive": counters["archive_proposals"],
        "revival": counters["revival_proposals"],
        "upvote_share": (
            round(counters["upvotes"] / direction_total, 4) if direction_total else None
        ),
        "skipped_by_cap": latest_cap_skips,
        "cap_skips": latest_cap_skips,
        "counters": counters,
        "latest_tick": (
            {
                "hour": latest.hour.isoformat() if latest.hour else None,
                "mode": latest.mode,
                "updated_at": (
                    latest.updated_at.isoformat() if latest.updated_at else None
                ),
            }
            if latest
            else None
        ),
    }


def _voting_api_payload():
    now = datetime.utcnow()
    mode = _normalize_voting_mode(Setting.get_value(_VOTING_MODE_SETTING, "off"))
    valid_rows = []
    for row in _policy_columns(limit=100):
        try:
            valid_rows.append(_policy_record(row))
        except (TypeError, ValueError):
            # A row inserted outside the ORM must not take the admin page down.
            logger.error("Ignoring invalid simulated-voting policy row id=%s", row.id)
    current = next(
        (
            record
            for record in valid_rows
            if record["effective_at"] is not None
            and record["effective_at"] <= now.isoformat()
        ),
        None,
    )
    preview_config = (
        current["config"] if current is not None else preset_config("natural")
    )
    presets = {
        name: {
            "name": name,
            "label": _VOTING_PRESET_LABELS[name],
            "description": _VOTING_PRESET_COPY[name],
            "recommended": name == "natural",
            "config": preset_config(name),
            "preview": _voting_preview(preset_config(name)),
        }
        for name in ("quiet", "natural", "busy")
    }
    history = [
        {key: value for key, value in record.items() if key != "config"}
        for record in valid_rows[:10]
    ]
    health = _voting_health()
    return {
        "mode": mode,
        "presets": presets,
        "current_policy": current,
        "resolved_policy": current,
        "preview": _voting_preview(preview_config),
        "health": health,
        "recent_health": health,
        "history": history,
        "policy_history": history,
    }


@admin_bp.route("/voting")
@production_disabled
@admin_required
def voting():
    """Dedicated simulated-voting controls and health page."""
    return render_template("admin/voting.html")


@admin_bp.route("/api/voting")
@production_disabled
@admin_required
def voting_api():
    """Return server-owned voting configuration and aggregate health."""
    return jsonify(_voting_api_payload())


@admin_bp.route("/api/voting/mode", methods=["PUT"])
@production_disabled
@admin_required
def voting_mode_api():
    payload = request.get_json(silent=True)
    mode = payload.get("mode") if isinstance(payload, dict) else None
    if not isinstance(mode, str) or mode.strip().lower() not in _VOTING_MODES:
        return jsonify({"error": "mode must be one of off, shadow, or live"}), 400
    mode = mode.strip().lower()
    if (
        mode in {"shadow", "live"}
        and not db.session.query(VoteCadencePolicy.id).first()
    ):
        return jsonify(
            {"error": f"Save a valid voting policy before enabling {mode} mode."}
        ), 400
    Setting.set_value(
        _VOTING_MODE_SETTING,
        mode,
        description="Simulated voting worker mode (off, shadow, or live)",
    )
    return jsonify({"mode": mode})


@admin_bp.route("/api/voting/policies", methods=["POST"])
@production_disabled
@admin_required
def voting_policy_api():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    preset = payload.get("preset")
    if isinstance(preset, str):
        preset = preset.strip().lower()
    if preset in {"quiet", "natural", "busy"}:
        config = preset_config(preset)
    elif preset == "custom" or "config" in payload or "policy" in payload:
        preset = "custom"
        config = payload.get("config", payload.get("policy"))
    elif set(payload) == {"post", "comment", "voter", "direction"}:
        preset = "custom"
        config = payload
    else:
        return jsonify({"error": "preset must be quiet, natural, busy, or custom"}), 400
    errors = _policy_validation_errors(config)
    if errors:
        return jsonify({"error": "Validation failed", "errors": errors}), 400
    try:
        normalized = validate_policy(config)
        policy = VoteCadencePolicy(
            preset=preset,
            algorithm_version=max(SUPPORTED_ALGORITHM_VERSIONS),
            config=normalized,
            effective_at=datetime.utcnow(),
        )
        db.session.add(policy)
        db.session.commit()
    except (TypeError, ValueError) as exc:
        db.session.rollback()
        return jsonify(
            {"error": "Validation failed", "errors": {"policy": str(exc)}}
        ), 400
    return jsonify({"policy": _policy_record(policy)}), 201


def _moderator_user() -> User | None:
    """Return the shared admin principal when it exists."""
    return db.session.get(User, "admin")


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
        "is_troll": bool(user.is_troll),
        "subscriptions": list((user.agent_state or {}).get("subscriptions") or []),
        "rate_caps": normalize_persona_rate_caps(
            (user.agent_state or {}).get("rate_caps")
        ),
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

    # Validate the per-persona rate-cap override before touching the row:
    # malformed input is a 400, not a silent drop or a 500.
    rate_caps = None
    if "rate_caps" in data:
        try:
            rate_caps = normalize_persona_rate_caps(data["rate_caps"], strict=True)
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    resolved_subs = None
    if "subscriptions" in data:
        raw_subs = data["subscriptions"]
        if raw_subs is None:
            subs_list = []
        elif isinstance(raw_subs, str):
            raw_subs = raw_subs.strip()
            if not raw_subs:
                subs_list = []
            elif raw_subs.startswith("["):
                try:
                    parsed = json.loads(raw_subs)
                    subs_list = parsed if isinstance(parsed, list) else [str(parsed)]
                except (json.JSONDecodeError, TypeError):
                    subs_list = [s.strip() for s in raw_subs.split(",") if s.strip()]
            else:
                subs_list = [s.strip() for s in raw_subs.split(",") if s.strip()]
        elif isinstance(raw_subs, list):
            subs_list = [str(s).strip() for s in raw_subs if str(s).strip()]
        else:
            return (
                jsonify({"success": False, "error": "Invalid subscriptions format"}),
                400,
            )

        valid_subs = {s.name.lower(): s.name for s in Subdeaddit.query.all()}
        resolved_subs = []
        for item in subs_list:
            cleaned = item.removeprefix("d/").removeprefix("r/").strip()
            if not cleaned:
                continue
            canonical = valid_subs.get(cleaned.lower())
            if not canonical:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": f"Subdeaddit '{item}' does not exist",
                        }
                    ),
                    400,
                )
            if canonical not in resolved_subs:
                resolved_subs.append(canonical)

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
        user.is_troll = bool(data.get("is_troll", user.is_troll))

        state = dict(user.agent_state or {})
        state_modified = False

        if rate_caps is not None:
            if rate_caps:
                state["rate_caps"] = rate_caps
            else:
                state.pop("rate_caps", None)
            state_modified = True

        if resolved_subs is not None:
            if resolved_subs:
                state["subscriptions"] = sorted(resolved_subs)
            else:
                state.pop("subscriptions", None)
            state_modified = True

        if state_modified:
            user.agent_state = state

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
    User.query.get_or_404(username)

    try:
        res = _delete_users_cascade([username])
        db.session.commit()
        media_service.delete_media_files(current_app, res["media_paths"])
        website_service.delete_website_files(current_app, res["website_paths"])

        return jsonify(
            {
                "success": True,
                "deleted": {
                    "user": username,
                    "posts": res["posts"],
                    "comments": res["comments"],
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
        existing_usernames = [
            row.username
            for row in User.query.filter(User.username.in_(usernames)).with_entities(
                User.username
            )
        ]
        res = _delete_users_cascade(existing_usernames)
        db.session.commit()
        media_service.delete_media_files(current_app, res["media_paths"])
        website_service.delete_website_files(current_app, res["website_paths"])

        return jsonify(
            {
                "success": True,
                "deleted": {
                    "users": res["users"],
                    "posts": res["posts"],
                    "comments": res["comments"],
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
        post_ids = [
            row.id
            for row in Post.query.filter_by(subdeaddit_name=name).with_entities(Post.id)
        ]
        media_paths = media_service.media_paths_for_posts(post_ids)
        website_paths = website_service.website_paths_for_posts(post_ids)
        posts_count, comments_count = _delete_posts_cascade(post_ids)

        SubdeadditModerator.query.filter_by(subdeaddit_name=name).delete(
            synchronize_session=False
        )
        Ban.query.filter_by(subdeaddit_name=name).delete(synchronize_session=False)

        db.session.delete(subdeaddit)
        db.session.commit()
        media_service.delete_media_files(current_app, media_paths)
        website_service.delete_website_files(current_app, website_paths)

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
        media_paths = []
        website_paths = []

        for name in names:
            subdeaddit = Subdeaddit.query.get(name)
            if subdeaddit:
                post_ids = [
                    row.id
                    for row in Post.query.filter_by(subdeaddit_name=name).with_entities(
                        Post.id
                    )
                ]
                media_paths.extend(media_service.media_paths_for_posts(post_ids))
                website_paths.extend(website_service.website_paths_for_posts(post_ids))
                posts_count, comments_count = _delete_posts_cascade(post_ids)

                SubdeadditModerator.query.filter_by(subdeaddit_name=name).delete(
                    synchronize_session=False
                )
                Ban.query.filter_by(subdeaddit_name=name).delete(
                    synchronize_session=False
                )
                db.session.delete(subdeaddit)

                deleted_count += 1
                total_posts += posts_count
                total_comments += comments_count

        db.session.commit()
        media_service.delete_media_files(current_app, media_paths)
        website_service.delete_website_files(current_app, website_paths)

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
        "website": post.website.to_public_dict() if post.website else None,
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
    Post.query.get_or_404(post_id)

    try:
        media_paths = media_service.media_paths_for_posts([post_id])
        website_paths = website_service.website_paths_for_posts([post_id])
        posts_count, comments_count = _delete_posts_cascade([post_id])

        db.session.commit()
        media_service.delete_media_files(current_app, media_paths)
        website_service.delete_website_files(current_app, website_paths)

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
        existing_post_ids = [
            row.id
            for row in Post.query.filter(Post.id.in_(post_ids)).with_entities(Post.id)
        ]
        media_paths = media_service.media_paths_for_posts(existing_post_ids)
        website_paths = website_service.website_paths_for_posts(existing_post_ids)
        posts_count, comments_count = _delete_posts_cascade(existing_post_ids)

        db.session.commit()
        media_service.delete_media_files(current_app, media_paths)
        website_service.delete_website_files(current_app, website_paths)

        return jsonify(
            {
                "success": True,
                "deleted": {"posts": posts_count, "comments": comments_count},
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
    Comment.query.get_or_404(comment_id)

    try:
        all_descendants = _get_comment_ids_with_descendants([comment_id])
        child_count = max(0, len(all_descendants) - 1)
        _delete_comments([comment_id])
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
        existing_comment_ids = [
            row.id
            for row in Comment.query.filter(Comment.id.in_(comment_ids)).with_entities(
                Comment.id
            )
        ]
        all_ids = _get_comment_ids_with_descendants(existing_comment_ids)
        deleted_count = len(existing_comment_ids)
        total_children = max(0, len(all_ids) - deleted_count)
        _delete_comments(existing_comment_ids)
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
    from deaddit.dynamics.metrics import daily_metric_row, daily_series

    series = daily_series(30)
    metric_rows = [daily_metric_row(row) for row in series]
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
        metric_rows=metric_rows,
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


@admin_bp.route("/capabilities/probe-vision", methods=["POST"])
@production_disabled
@admin_required
def capabilities_probe_vision():
    """Run a vision probe for one endpoint/model and flash the verdict."""
    api_url = request.form.get("api_url", "").strip()
    model_name = request.form.get("model_name", "").strip()
    api_key = request.form.get("api_key", "").strip() or None
    if not api_url or not model_name:
        flash("Both API URL and model name are required.", "error")
        return redirect(url_for("admin.capabilities"))
    try:
        cap = probe_vision(api_url, model_name, api_key=api_key)
    except Exception as exc:
        flash(f"Vision probe could not determine a verdict: {exc}", "error")
        return redirect(url_for("admin.capabilities"))
    verdict = "supported" if cap.supports_vision else "NOT supported"
    flash(
        f"Probe verdict for {model_name}: vision {verdict} "
        f"(vision_probe_method={cap.vision_probe_method}).",
        "success" if cap.supports_vision else "warning",
    )
    return redirect(url_for("admin.capabilities"))


@admin_bp.route("/capabilities/override-vision", methods=["POST"])
@production_disabled
@admin_required
def capabilities_override_vision():
    """Record a manual vision-capability override for one endpoint/model."""
    api_url = request.form.get("api_url", "").strip()
    model_name = request.form.get("model_name", "").strip()
    supports_vision = request.form.get("supports_vision") == "true"
    if not api_url or not model_name:
        flash("Both API URL and model name are required.", "error")
        return redirect(url_for("admin.capabilities"))
    set_vision_manual_override(api_url, model_name, supports_vision)
    flash(
        f"Manual override saved: {model_name} vision "
        f"{'supported' if supports_vision else 'disabled'}.",
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
        troll_chance_raw = str(data.get("troll_user_chance") or "").strip()
        if troll_chance_raw:
            try:
                troll_chance = float(troll_chance_raw)
            except ValueError:
                return (
                    jsonify(
                        {
                            "success": False,
                            "message": "Troll chance must be a number between 0 and 1",
                        }
                    ),
                    400,
                )
            if not 0.0 <= troll_chance <= 1.0:
                return (
                    jsonify(
                        {
                            "success": False,
                            "message": "Troll chance must be between 0 and 1",
                        }
                    ),
                    400,
                )
            Config.set("TROLL_USER_CHANCE", str(troll_chance))

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


# --- Image providers (separate from LLM providers; see refactor/image_post_plan.md 3A) ---

_IMAGE_PROVIDER_TYPES = ("fal", "runware")
_DEFAULT_IMAGE_CREDENTIAL_ENV = {
    "fal": "FALAI_API_KEY",
    "runware": "RUNWARE_API_KEY",
}
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _image_provider_payload(provider):
    """Serialize a provider for the admin API.

    Never returns the stored key itself - only masked ``has_key`` /
    ``key_last4`` from ``to_dict`` plus a computed ``credential_set``
    boolean and ``credential_source`` (``stored`` / ``environment`` / null)
    so the UI can show where the working credential comes from.
    """
    data = provider.to_dict()
    data["credential_set"] = image_client.credential_is_configured(provider)
    data["credential_source"] = (
        "stored"
        if image_client.stored_credential(provider)
        else (
            "environment"
            if provider.credential_env and os.environ.get(provider.credential_env)
            else None
        )
    )
    data["cached_model_count"] = (
        ImageModel.query.filter_by(provider_id=provider.id, is_active=True).count()
        if provider.id is not None
        else 0
    )
    return data


def _cache_image_model_options(provider, options):
    """Upsert search results into ImageModel for admin visibility.

    Only meaningful once *provider* is persisted (has an id); a draft
    provider used for a pre-save connection test is never cached.
    """
    if provider.id is None or not options:
        return
    now = datetime.utcnow()
    for option in options:
        row = ImageModel.query.filter_by(
            provider_id=provider.id, model_identifier=option.model_id
        ).first()
        if row is None:
            row = ImageModel(provider_id=provider.id, model_identifier=option.model_id)
            db.session.add(row)
        row.display_name = option.display_name
        row.category = option.category
        row.provider_metadata = option.metadata or None
        row.last_fetched = now
        row.is_active = True
    db.session.commit()


@admin_bp.route("/api/image-providers", methods=["GET"])
@production_disabled
@admin_required
def api_list_image_providers():
    """List all configured image providers."""
    providers = ImageProvider.query.order_by(ImageProvider.id.asc()).all()
    return jsonify(
        {
            "success": True,
            "providers": [_image_provider_payload(p) for p in providers],
        }
    )


@admin_bp.route("/api/image-providers", methods=["POST"])
@production_disabled
@admin_required
def api_create_image_provider():
    """Create a new image provider.

    Accepts an optional write-only ``api_key`` (stored on the row, never
    returned), a provider_type (which adapter to use), an optional
    ``credential_env`` fallback name (still defaulted per type so existing
    env-based deployments keep working), and an optional default_model that
    must pass the adapter's validate_model before it is ever accepted.
    """
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    provider_type = str(payload.get("provider_type") or "").strip()
    credential_env = str(payload.get("credential_env") or "").strip()
    api_key = str(payload.get("api_key") or "").strip() or None
    is_enabled = bool(payload.get("is_enabled", True))
    default_model = str(payload.get("default_model") or "").strip()

    if not name:
        return jsonify({"success": False, "error": "Provider name is required"}), 400
    if ImageProvider.query.filter_by(name=name).first():
        return (
            jsonify(
                {
                    "success": False,
                    "error": f"An image provider named {name!r} already exists",
                }
            ),
            400,
        )
    if provider_type not in _IMAGE_PROVIDER_TYPES:
        return (
            jsonify(
                {
                    "success": False,
                    "error": f"provider_type must be one of {sorted(_IMAGE_PROVIDER_TYPES)}",
                }
            ),
            400,
        )
    if not credential_env:
        credential_env = _DEFAULT_IMAGE_CREDENTIAL_ENV[provider_type]
    if not _ENV_NAME_RE.match(credential_env):
        return (
            jsonify(
                {
                    "success": False,
                    "error": "credential_env must be a valid environment variable name",
                }
            ),
            400,
        )

    provider = ImageProvider(
        name=name,
        provider_type=provider_type,
        api_key=api_key,
        credential_env=credential_env,
        is_enabled=is_enabled,
        is_default=ImageProvider.query.count() == 0
        or bool(payload.get("is_default", False)),
    )

    if default_model:
        try:
            verdict = image_client.validate_model(provider, default_model)
        except ImageProviderError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        if not verdict.compatible:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": verdict.reason
                        or f"model {default_model!r} is not compatible with this provider",
                    }
                ),
                400,
            )
        provider.default_model = default_model

    if provider.is_default:
        ImageProvider.query.update({"is_default": False})
    db.session.add(provider)
    db.session.commit()
    return jsonify(
        {"success": True, "provider": _image_provider_payload(provider)}
    ), 201


@admin_bp.route("/api/image-providers/test-connection", methods=["POST"])
@production_disabled
@admin_required
def api_test_image_provider_connection():
    """Authenticated catalog search only - never generation.

    Accepts either {"provider_id": N} for a saved provider, or
    {"provider_type", "credential_env"} for an unsaved draft so the admin UI
    can test before the first save. An optional ``api_key`` overrides the
    credential for this probe only (in memory, never committed), letting the
    UI test a freshly typed key before saving it. No secret is ever returned.
    """
    payload = request.get_json(silent=True) or {}
    provider_id = payload.get("provider_id")
    query = str(payload.get("query") or "")
    typed_key = str(payload.get("api_key") or "").strip() or None

    if provider_id:
        try:
            provider = db.session.get(ImageProvider, int(provider_id))
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "Invalid provider_id"}), 400
        if not provider:
            return jsonify(
                {"success": False, "message": "Image provider not found"}
            ), 404
        if typed_key:
            # Probe with the freshly typed key without persisting it.
            provider.api_key = typed_key
    else:
        provider_type = str(payload.get("provider_type") or "").strip()
        credential_env = str(payload.get("credential_env") or "").strip()
        if provider_type not in _IMAGE_PROVIDER_TYPES:
            return (
                jsonify(
                    {
                        "success": False,
                        "message": f"provider_type must be one of {sorted(_IMAGE_PROVIDER_TYPES)}",
                    }
                ),
                400,
            )
        if not credential_env:
            credential_env = _DEFAULT_IMAGE_CREDENTIAL_ENV[provider_type]
        if not _ENV_NAME_RE.match(credential_env):
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "credential_env must be a valid environment variable name",
                    }
                ),
                400,
            )
        provider = ImageProvider(
            name="(unsaved)",
            provider_type=provider_type,
            api_key=typed_key,
            credential_env=credential_env,
            is_enabled=True,
        )

    result = image_verification.test_connection(provider, query=query)
    return jsonify(
        {
            "success": result.ok,
            "message": result.message,
            "sample_model_ids": result.sample_model_ids,
        }
    )


@admin_bp.route("/api/image-providers/<int:provider_id>", methods=["GET"])
@production_disabled
@admin_required
def api_get_image_provider(provider_id):
    """Get a single image provider."""
    provider = db.session.get(ImageProvider, provider_id)
    if not provider:
        return jsonify({"success": False, "error": "Image provider not found"}), 404
    return jsonify({"success": True, "provider": _image_provider_payload(provider)})


@admin_bp.route("/api/image-providers/<int:provider_id>", methods=["PUT"])
@production_disabled
@admin_required
def api_update_image_provider(provider_id):
    """Update an image provider.

    Field changes are applied to the in-session object first; if a
    requested default_model fails validate_model, every change from this
    request (including provider_type/credential_env/api_key/is_enabled) is
    rolled back so the update is all-or-nothing.
    """
    provider = db.session.get(ImageProvider, provider_id)
    if not provider:
        return jsonify({"success": False, "error": "Image provider not found"}), 404

    payload = request.get_json(silent=True) or {}

    if "name" in payload:
        name = str(payload.get("name") or "").strip()
        if not name:
            return (
                jsonify({"success": False, "error": "Provider name cannot be empty"}),
                400,
            )
        if ImageProvider.query.filter(
            ImageProvider.id != provider.id, ImageProvider.name == name
        ).first():
            return (
                jsonify(
                    {
                        "success": False,
                        "error": f"An image provider named {name!r} already exists",
                    }
                ),
                400,
            )
        provider.name = name

    if "provider_type" in payload:
        provider_type = str(payload.get("provider_type") or "").strip()
        if provider_type not in _IMAGE_PROVIDER_TYPES:
            db.session.rollback()
            return (
                jsonify(
                    {
                        "success": False,
                        "error": f"provider_type must be one of {sorted(_IMAGE_PROVIDER_TYPES)}",
                    }
                ),
                400,
            )
        provider.provider_type = provider_type

    if "credential_env" in payload:
        credential_env = str(payload.get("credential_env") or "").strip()
        if not _ENV_NAME_RE.match(credential_env):
            db.session.rollback()
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "credential_env must be a valid environment variable name",
                    }
                ),
                400,
            )
        provider.credential_env = credential_env

    # Write-only: absent keeps the stored key, "" clears it (falling back to
    # the credential_env variable), any other value replaces it.
    if "api_key" in payload:
        provider.api_key = str(payload.get("api_key") or "").strip() or None

    if "is_enabled" in payload:
        provider.is_enabled = bool(payload.get("is_enabled"))

    if "default_model" in payload:
        model_id = str(payload.get("default_model") or "").strip()
        if not model_id:
            provider.default_model = None
        else:
            try:
                verdict = image_client.validate_model(provider, model_id)
            except ImageProviderError as exc:
                db.session.rollback()
                return jsonify({"success": False, "error": str(exc)}), 400
            if not verdict.compatible:
                db.session.rollback()
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": verdict.reason
                            or f"model {model_id!r} is not compatible with this provider",
                        }
                    ),
                    400,
                )
            provider.default_model = model_id

    if "is_default" in payload:
        new_default = bool(payload.get("is_default"))
        if new_default:
            for other in ImageProvider.query.all():
                other.is_default = other.id == provider.id
        elif ImageProvider.query.count() == 1:
            provider.is_default = True
        else:
            provider.is_default = False
            if not ImageProvider.query.filter(
                ImageProvider.id != provider.id, ImageProvider.is_default.is_(True)
            ).first():
                other = ImageProvider.query.filter(
                    ImageProvider.id != provider.id
                ).first()
                if other:
                    other.is_default = True

    provider.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"success": True, "provider": _image_provider_payload(provider)})


@admin_bp.route("/api/image-providers/<int:provider_id>", methods=["DELETE"])
@production_disabled
@admin_required
def api_delete_image_provider(provider_id):
    """Delete an image provider, refusing while any agent config references it.

    Per-agent image configuration lands in 3B under
    Agent.config["image_posts"]["provider_id"]; this check is written
    against that shape now so it is already correct once 3B ships.
    """
    provider = db.session.get(ImageProvider, provider_id)
    if not provider:
        return jsonify({"success": False, "error": "Image provider not found"}), 404

    referencing = [
        agent
        for agent in Agent.query.all()
        if isinstance(agent.config, dict)
        and (agent.config.get("image_posts") or {}).get("provider_id") == provider.id
    ]
    if referencing:
        usernames = ", ".join(sorted(agent.user_username for agent in referencing))
        return (
            jsonify(
                {
                    "success": False,
                    "error": f"Cannot delete: referenced by agent image configuration ({usernames})",
                }
            ),
            400,
        )

    was_default = provider.is_default
    db.session.delete(provider)
    db.session.commit()
    if was_default:
        remaining = ImageProvider.query.order_by(ImageProvider.id.asc()).first()
        if remaining:
            remaining.is_default = True
            db.session.commit()
    return jsonify({"success": True, "message": "Image provider deleted successfully"})


@admin_bp.route("/api/image-providers/<int:provider_id>/set-default", methods=["POST"])
@production_disabled
@admin_required
def api_set_default_image_provider(provider_id):
    """Set specified image provider as default."""
    provider = db.session.get(ImageProvider, provider_id)
    if not provider:
        return jsonify({"success": False, "error": "Image provider not found"}), 404

    ImageProvider.set_default(provider.id)
    return jsonify({"success": True, "provider": _image_provider_payload(provider)})


@admin_bp.route("/api/image-providers/<int:provider_id>/models", methods=["GET"])
@production_disabled
@admin_required
def api_search_image_provider_models(provider_id):
    """Paginated/typeahead catalog search - never an unbounded catalog fetch.

    Forwards ?q= and ?cursor= to the adapter's search_models, which owns
    page size; this route never asks for more than one page.
    """
    provider = db.session.get(ImageProvider, provider_id)
    if not provider:
        return jsonify({"success": False, "error": "Image provider not found"}), 404

    query = request.args.get("q", "")
    cursor = request.args.get("cursor") or None

    try:
        result = image_client.search_models(provider, query=query, cursor=cursor)
    except ImageProviderError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    _cache_image_model_options(provider, result.options)

    return jsonify(
        {
            "success": True,
            "options": [
                {
                    "model_id": option.model_id,
                    "display_name": option.display_name,
                    "category": option.category,
                    "metadata": option.metadata,
                }
                for option in result.options
            ],
            "next_cursor": result.next_cursor,
        }
    )


@admin_bp.route(
    "/api/image-providers/<int:provider_id>/models/validate", methods=["POST"]
)
@production_disabled
@admin_required
def api_validate_image_provider_model(provider_id):
    """Confirm a manually-typed model id before it may become a default_model."""
    provider = db.session.get(ImageProvider, provider_id)
    if not provider:
        return jsonify({"success": False, "error": "Image provider not found"}), 404

    payload = request.get_json(silent=True) or {}
    model_id = str(payload.get("model_id") or "").strip()
    if not model_id:
        return jsonify({"success": False, "error": "model_id is required"}), 400

    try:
        verdict = image_client.validate_model(provider, model_id)
    except ImageProviderError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    return jsonify(
        {
            "success": True,
            "compatible": verdict.compatible,
            "reason": verdict.reason,
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

        # Delete all job logs then jobs
        JobLog.query.delete(synchronize_session=False)
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

# --- Per-agent image-post configuration (Phase 3B) ---
# Lives at Agent.config["image_posts"]; see refactor/image_post_plan.md 3B.
# Canonical disabled shape: the "image_posts" key is absent entirely (missing
# means disabled), so disabling an agent always pops the key rather than
# storing some {"enabled": false, ...} variant.
_IMAGE_POST_POLICIES = ("optional", "image_only")
_WEBSITE_POST_POLICIES = ("optional", "website_only")
_UNSET = object()


def _resolve_image_posts(raw, tier):
    """Validate a requested ``image_posts`` payload into its stored shape.

    ``raw`` is the value the caller supplied for the "image_posts" key, or
    ``_UNSET`` if the caller did not touch it at all (existing config, if
    any, is preserved by callers in that case). Returns
    ``(value, error_response)``:

    - ``(_UNSET, None)`` when the caller did not request a change.
    - ``(None, None)`` when the caller asked to disable: callers must pop
      the "image_posts" key from the stored config.
    - ``(dict, None)`` with the fully-resolved config to store.
    - ``(None, (response, 400))`` on any validation failure.
    """
    if raw is _UNSET:
        return _UNSET, None
    if not isinstance(raw, dict):
        return None, (
            jsonify({"success": False, "error": "image_posts must be an object"}),
            400,
        )

    if not raw.get("enabled"):
        return None, None

    if tier == "lurker":
        return None, (
            jsonify(
                {
                    "success": False,
                    "error": "image posts cannot be enabled for a lurker agent",
                }
            ),
            400,
        )

    provider_id = raw.get("provider_id")
    use_default = provider_id is None or provider_id == ""
    if use_default:
        provider = ImageProvider.get_default()
        provider_id_int = provider.id if provider else None
    else:
        try:
            provider_id_int = int(provider_id)
        except (TypeError, ValueError):
            provider_id_int = None
        provider = (
            db.session.get(ImageProvider, provider_id_int) if provider_id_int else None
        )
    if provider is None:
        error = (
            "no default image provider is configured"
            if use_default
            else "Image provider not found"
        )
        return None, (jsonify({"success": False, "error": error}), 400)
    if not provider.is_enabled:
        return None, (
            jsonify(
                {
                    "success": False,
                    "error": f"Image provider {provider.name!r} is disabled",
                }
            ),
            400,
        )
    if not image_client.credential_is_configured(provider):
        return None, (
            jsonify(
                {
                    "success": False,
                    "error": (
                        f"Image provider {provider.name!r} has no credential "
                        f"configured (save an API key in the admin UI or set "
                        f"{provider.credential_env})"
                    ),
                }
            ),
            400,
        )

    model = str(raw.get("model") or "").strip()
    if model:
        try:
            verdict = image_client.validate_model(provider, model)
        except ImageProviderError as exc:
            return None, (jsonify({"success": False, "error": str(exc)}), 400)
        if not verdict.compatible:
            return None, (
                jsonify(
                    {
                        "success": False,
                        "error": verdict.reason
                        or f"model {model!r} is not compatible with provider {provider.name!r}",
                    }
                ),
                400,
            )
    else:
        model = None
        if not provider.default_model:
            return None, (
                jsonify(
                    {
                        "success": False,
                        "error": (
                            f"Image provider {provider.name!r} has no default "
                            "model configured; set a model override"
                        ),
                    }
                ),
                400,
            )

    policy = raw.get("policy") or "optional"
    if policy not in _IMAGE_POST_POLICIES:
        return None, (
            jsonify(
                {
                    "success": False,
                    "error": f"image_posts.policy must be one of {list(_IMAGE_POST_POLICIES)}",
                }
            ),
            400,
        )

    return {
        "enabled": True,
        "provider_id": None if use_default else provider.id,
        "model": model,
        "policy": policy,
    }, None


def _resolve_website_posts(raw, tier):
    """Validate a requested ``website_posts`` payload into its stored shape.

    ``raw`` is the value the caller supplied for the "website_posts" key, or
    ``_UNSET`` if the caller did not touch it at all (existing config, if
    any, is preserved by callers in that case). Returns
    ``(value, error_response)``:

    - ``(_UNSET, None)`` when the caller did not request a change.
    - ``(None, None)`` when the caller asked to disable: callers must pop
      the "website_posts" key from the stored config.
    - ``(dict, None)`` with the fully-resolved config to store.
    - ``(None, (response, 400))`` on any validation failure.
    """
    if raw is _UNSET:
        return _UNSET, None
    if not isinstance(raw, dict):
        return None, (
            jsonify({"success": False, "error": "website_posts must be an object"}),
            400,
        )

    if not raw.get("enabled"):
        return None, None

    if tier == "lurker":
        return None, (
            jsonify(
                {
                    "success": False,
                    "error": "website posts cannot be enabled for a lurker agent",
                }
            ),
            400,
        )

    policy = raw.get("policy") or "optional"
    if policy not in _WEBSITE_POST_POLICIES:
        return None, (
            jsonify(
                {
                    "success": False,
                    "error": f"website_posts.policy must be one of {list(_WEBSITE_POST_POLICIES)}",
                }
            ),
            400,
        )

    return {"enabled": True, "policy": policy}, None


def _agent_display_label(agent):
    """Return the human-readable identity for an agent."""
    return (
        agent.user_username
        if agent.user_username is not None
        else f"Random #{agent.id}"
    )


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
        "persona_mode": agent.persona_mode or "fixed",
        "display_label": _agent_display_label(agent),
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
        "persona_username": run.persona_username,
        "trigger": run.trigger,
        "intent": getattr(run, "intent", None),
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

    agents = Agent.query.all()
    agents.sort(key=lambda a: (a.persona_mode or "fixed", a.user_username or "", a.id))
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
    taken = (
        db.session.query(Agent.user_username)
        .filter(Agent.persona_mode == "fixed", Agent.user_username.isnot(None))
        .all()
    )
    taken = [username for (username,) in taken]
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
    persona_mode = str(payload.get("persona_mode") or "fixed").strip()
    if persona_mode not in {"fixed", "random"}:
        return (
            jsonify(
                {"success": False, "error": f"Unknown persona_mode '{persona_mode}'"}
            ),
            400,
        )

    username = str(payload.get("username") or "").strip()
    if persona_mode == "fixed":
        if not username:
            return jsonify({"success": False, "error": "username is required"}), 400
        if db.session.get(User, username) is None:
            return (
                jsonify(
                    {"success": False, "error": f"User '{username}' does not exist"}
                ),
                400,
            )
        if Agent.query.filter_by(user_username=username).first() is not None:
            return (
                jsonify(
                    {"success": False, "error": f"'{username}' already has an agent"}
                ),
                409,
            )
    elif username:
        return (
            jsonify(
                {"success": False, "error": "random agents must not specify a username"}
            ),
            400,
        )
    else:
        username = None

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
    if persona_mode == "random" and payload.get("backfill_memory", True):
        config["backfill_memory"] = True
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

    image_posts, image_posts_error = _resolve_image_posts(
        payload.get("image_posts", _UNSET), tier
    )
    if image_posts_error:
        return image_posts_error
    if image_posts not in (_UNSET, None):
        config["image_posts"] = image_posts

    website_posts, website_posts_error = _resolve_website_posts(
        payload.get("website_posts", _UNSET), tier
    )
    if website_posts_error:
        return website_posts_error
    if website_posts not in (_UNSET, None):
        config["website_posts"] = website_posts

    image_config = config.get("image_posts")
    website_config = config.get("website_posts")
    if (
        isinstance(image_config, dict)
        and isinstance(website_config, dict)
        and image_config.get("enabled")
        and image_config.get("policy") == "image_only"
        and website_config.get("enabled")
        and website_config.get("policy") == "website_only"
    ):
        return (
            jsonify(
                {
                    "success": False,
                    "error": (
                        "image_only and website_only policies cannot be combined; "
                        "choose one forced post policy"
                    ),
                }
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
        persona_mode=persona_mode,
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
    if persona_mode == "fixed" and payload.get("backfill_memory", True):
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


@admin_bp.route("/api/agents/<int:agent_id>", methods=["GET"])
@production_disabled
@admin_required
def api_get_agent(agent_id):
    """Return one agent by id."""
    from deaddit.models import Agent

    agent = db.session.get(Agent, agent_id)
    if agent is None:
        return jsonify({"success": False, "error": "agent not found"}), 404
    return jsonify(_agent_json(agent))


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
    cfg_in = payload.get("config") if isinstance(payload.get("config"), dict) else {}

    current_mode = agent.persona_mode or "fixed"
    for source in (payload, cfg_in):
        if "persona_mode" in source:
            mode_value = str(source["persona_mode"] or "").strip()
            if mode_value != current_mode:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "persona_mode is immutable; create a new agent instead",
                        }
                    ),
                    400,
                )

    for source in (payload, cfg_in):
        for key in ("user_username", "username"):
            if key in source:
                username_value = str(source[key] or "").strip() or None
                if username_value != agent.user_username:
                    return (
                        jsonify(
                            {
                                "success": False,
                                "error": "fixed persona is immutable; create a new agent instead",
                            }
                        ),
                        400,
                    )

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

    # image_posts (namespaced per-agent image configuration, Phase 3B)
    image_posts_raw = payload.get("image_posts", cfg_in.get("image_posts", _UNSET))
    image_posts, image_posts_error = _resolve_image_posts(
        image_posts_raw, agent.autonomy_tier
    )
    if image_posts_error:
        return image_posts_error
    if image_posts is None and image_posts_raw is not _UNSET:
        config.pop("image_posts", None)
    elif image_posts is not _UNSET:
        config["image_posts"] = image_posts

    website_posts_raw = payload.get(
        "website_posts", cfg_in.get("website_posts", _UNSET)
    )
    website_posts, website_posts_error = _resolve_website_posts(
        website_posts_raw, agent.autonomy_tier
    )
    if website_posts_error:
        return website_posts_error
    if website_posts is None and website_posts_raw is not _UNSET:
        config.pop("website_posts", None)
    elif website_posts is not _UNSET:
        config["website_posts"] = website_posts

    # A tier change to lurker must not leave an already-enabled image
    # configuration in place, even when this request never touches
    # "image_posts" itself.
    if agent.autonomy_tier == "lurker" and (config.get("image_posts") or {}).get(
        "enabled"
    ):
        return (
            jsonify(
                {
                    "success": False,
                    "error": "image posts cannot be enabled for a lurker agent",
                }
            ),
            400,
        )

    # A tier change to lurker must not leave an already-enabled website
    # configuration in place, even when this request never touches
    # "website_posts" itself.
    if agent.autonomy_tier == "lurker" and (config.get("website_posts") or {}).get(
        "enabled"
    ):
        return (
            jsonify(
                {
                    "success": False,
                    "error": "website posts cannot be enabled for a lurker agent",
                }
            ),
            400,
        )

    image_config = config.get("image_posts")
    website_config = config.get("website_posts")
    if (
        isinstance(image_config, dict)
        and isinstance(website_config, dict)
        and image_config.get("enabled")
        and image_config.get("policy") == "image_only"
        and website_config.get("enabled")
        and website_config.get("policy") == "website_only"
    ):
        return (
            jsonify(
                {
                    "success": False,
                    "error": (
                        "image_only and website_only policies cannot be combined; "
                        "choose one forced post policy"
                    ),
                }
            ),
            400,
        )

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


def _has_active_run(agent) -> bool:
    """True when the agent is queued for, or already executing, a run."""
    return agent.status in ("queued", "running") or (
        AgentRun.query.filter_by(agent_id=agent.id, status="running").first()
        is not None
    )


def _manual_intent_error(agent, intent) -> str | None:
    """Eligibility error for a requested media intent, or None when allowed.

    Reuses the registry's static post-tool truth table — the same helpers the
    visit planner and executor use — so admin validation can never drift
    from runtime policy. Reasons mirror the agent-detail menu: lurker tier,
    a conflicting static post-only policy, or the media kind not enabled.
    """
    from deaddit.agents.registry import (
        AutonomyTier,
        image_posts_config,
        offered_post_tool_names,
        website_posts_config,
    )

    if intent not in ("image", "website"):
        return None
    tier = getattr(agent.autonomy_tier, "value", agent.autonomy_tier)
    if tier == AutonomyTier.LURKER.value:
        return "lurker-tier agents cannot make image or website posts"
    image_cfg = image_posts_config(agent)
    website_cfg = website_posts_config(agent)
    if (
        image_cfg["enabled"]
        and image_cfg["policy"] == "image_only"
        and website_cfg["enabled"]
        and website_cfg["policy"] == "website_only"
    ):
        # The truth table fails closed to no post tools for this combination;
        # name the conflict rather than the downstream "not enabled" symptom.
        return (
            "conflicting static post-only policy (image_only and website_only): "
            "no media run can be offered"
        )
    static_offered = offered_post_tool_names(image_cfg, website_cfg)
    if intent == "image" and "create_image_post" not in static_offered:
        return "image posts are not enabled for this agent"
    if intent == "website" and "create_website" not in static_offered:
        return "website posts are not enabled for this agent"
    return None


def _enqueue_agent_run(agent, requested_intent):
    """Atomically claim an idle agent and queue an AGENT_RUN job for it.

    Returns the queued :class:`~deaddit.models.Job`, or ``None`` when a
    concurrent request claimed the agent first. The conditional UPDATE is
    the queue-ownership gate; the job insert, ``status='queued'``, and the
    ``state.manual_run`` marker land in a single transaction.
    """
    import uuid

    from sqlalchemy import update

    from deaddit.jobs import AGENT_RUN_JOB_PRIORITY
    from deaddit.models import JobStatus, JobType

    job = Job(
        type=JobType.AGENT_RUN,
        status=JobStatus.PENDING,
        priority=AGENT_RUN_JOB_PRIORITY,
        total_items=1,
        parameters={"agent_id": agent.id, "requested_intent": requested_intent},
        rq_job_id=str(uuid.uuid4()),
    )
    db.session.add(job)
    db.session.flush()  # assign job.id for the manual-run marker

    state = dict(agent.state or {})
    state["manual_run"] = {
        "job_id": job.id,
        "requested_intent": requested_intent,
        "queued_at": datetime.utcnow().isoformat(),
        "previous_status": agent.status or "idle",
    }
    claimed = db.session.execute(
        update(Agent)
        .where(Agent.id == agent.id, Agent.status.notin_(("queued", "running")))
        .values(status="queued", state=state)
    ).rowcount
    if not claimed:
        db.session.rollback()
        return None
    db.session.commit()
    logger.info(
        "Queued AGENT_RUN job %s for agent %s (requested_intent=%r)",
        job.id,
        agent.id,
        requested_intent,
    )
    return job


@admin_bp.route("/api/agents/<int:agent_id>/force-run", methods=["POST"])
@production_disabled
@admin_required
def api_force_run(agent_id):
    """Queue one manual agent visit as a high-priority worker job.

    Returns 202 immediately; the worker process owns execution through
    ``run_once()`` — the web process never runs an LLM turn. ``intent`` is
    ``null``/absent for the existing generic visit, or ``"image"`` /
    ``"website"`` for a requested media intent.
    """
    agent = db.session.get(Agent, agent_id)
    if agent is None:
        return jsonify({"success": False, "error": "agent not found"}), 404

    intent = (request.get_json(silent=True) or {}).get("intent")
    if intent not in (None, "image", "website"):
        return (
            jsonify(
                {
                    "success": False,
                    "error": "intent must be null, 'image', or 'website'",
                }
            ),
            400,
        )

    if _has_active_run(agent):
        return (
            jsonify({"success": False, "error": "agent already has a run in progress"}),
            409,
        )

    error = _manual_intent_error(agent, intent)
    if error:
        return jsonify({"success": False, "error": error}), 422

    job = _enqueue_agent_run(agent, intent)
    if job is None:
        # Lost the atomic claim to a concurrent request.
        return (
            jsonify({"success": False, "error": "agent already has a run in progress"}),
            409,
        )
    return jsonify({"job": job.to_dict(), "agent": _agent_json(agent)}), 202


@admin_bp.route("/api/jobs/<int:job_id>")
@production_disabled
@admin_required
def api_job_status(job_id):
    """One queue job by ID (generic queue-status lookup).

    Lets the agent-detail live panel distinguish pending, claimed, completed,
    and failed jobs while no ``AgentRun`` row exists yet. Returns the job's
    own ``to_dict`` serialization — no second activity representation.
    """
    job = db.session.get(Job, job_id)
    if job is None:
        return jsonify({"success": False, "error": "job not found"}), 404
    return jsonify({"job": job.to_dict()})


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


_BULK_AGENT_ACTIONS = frozenset(
    {
        "enable",
        "disable",
        "force_run",
        "enable_image",
        "disable_image",
        "enable_website",
        "disable_website",
        "delete",
    }
)


def _bulk_flag_posts(agent, key, enable, resolver):
    """Flip one agent's ``image_posts``/``website_posts`` flag in place.

    Returns an error string when the agent cannot be flipped (lurker tier,
    missing default image provider, already in the requested state, ...).
    Disabling pops the key entirely - the canonical disabled shape is an
    absent key; enabling resolves a fresh default-backed config only for
    agents that are currently disabled, so existing provider/model/policy
    overrides of enabled agents are never clobbered.
    """
    from sqlalchemy.orm.attributes import flag_modified

    config = dict(agent.config or {})
    if enable:
        if config.get(key, {}).get("enabled"):
            return "already enabled"
        resolved, error_response = resolver(
            {"enabled": True, "policy": "optional"}, agent.autonomy_tier
        )
        if error_response is not None:
            return error_response[0].get_json().get("error", "invalid config")
        config[key] = resolved
    else:
        if key not in config:
            return "already disabled"
        config.pop(key, None)
    agent.config = config
    flag_modified(agent, "config")
    return None


@admin_bp.route("/api/agents/bulk", methods=["POST"])
@production_disabled
@admin_required
def api_agents_bulk():
    """Apply one action to a selected set of agents.

    Flag actions (enable/disable, image/website toggles, delete) are plain
    DB updates committed once. ``force_run`` validates the batch, skips
    agents that are already queued or running, and enqueues one generic
    AGENT_RUN job per remaining agent on the worker queue; the response
    reports the queued job IDs. Per-agent failures (e.g. enabling images on
    a lurker) are reported as ``skipped`` entries and never abort the rest
    of the batch.
    """

    payload = request.get_json(silent=True) or {}
    action = payload.get("action")
    if action not in _BULK_AGENT_ACTIONS:
        return (
            jsonify(
                {
                    "success": False,
                    "error": f"action must be one of {sorted(_BULK_AGENT_ACTIONS)}",
                }
            ),
            400,
        )

    raw_ids = payload.get("agent_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return (
            jsonify({"success": False, "error": "agent_ids must be a non-empty list"}),
            400,
        )
    agent_ids = []
    for value in raw_ids:
        parsed = _as_int(value)
        if parsed is None:
            return (
                jsonify({"success": False, "error": "agent_ids must contain integers"}),
                400,
            )
        if parsed not in agent_ids:
            agent_ids.append(parsed)

    agents = {a.id: a for a in Agent.query.filter(Agent.id.in_(agent_ids))}
    skipped = [
        {"id": agent_id, "label": str(agent_id), "error": "agent not found"}
        for agent_id in agent_ids
        if agent_id not in agents
    ]
    affected = []
    errors = []
    queued_jobs = []

    for agent_id in agent_ids:
        agent = agents.get(agent_id)
        if agent is None:
            continue
        label = _agent_display_label(agent)
        error = None
        if action == "enable":
            if agent.is_enabled:
                error = "already enabled"
            else:
                # Mirror the toggle endpoint: enabling clears strikes and
                # wakes the agent on the next scheduler poll.
                agent.is_enabled = True
                agent.consecutive_failures = 0
                agent.next_run_at = datetime.utcnow()
        elif action == "disable":
            if not agent.is_enabled:
                error = "already disabled"
            else:
                agent.is_enabled = False
                agent.next_run_at = None
                agent.status = "idle"
        elif action == "force_run":
            if _has_active_run(agent):
                error = "already has a run in progress"
            else:
                job = _enqueue_agent_run(agent, None)
                if job is None:
                    error = "already has a run in progress"
                else:
                    queued_jobs.append({"agent_id": agent.id, "job_id": job.id})
        elif action == "enable_image":
            error = _bulk_flag_posts(agent, "image_posts", True, _resolve_image_posts)
        elif action == "disable_image":
            error = _bulk_flag_posts(agent, "image_posts", False, _resolve_image_posts)
        elif action == "enable_website":
            error = _bulk_flag_posts(
                agent, "website_posts", True, _resolve_website_posts
            )
        elif action == "disable_website":
            error = _bulk_flag_posts(
                agent, "website_posts", False, _resolve_website_posts
            )
        elif action == "delete":
            if agent.status == "running":
                error = "run in progress; wait for it to finish"
            else:
                # FKs cascade at the DB level: runs/turns/tool_calls go with
                # the agent, generated-website references are nulled, and the
                # persona user account itself is kept.
                db.session.delete(agent)
        if error is None:
            affected.append(agent_id)
        else:
            errors.append({"id": agent_id, "label": label, "error": error})

    if action == "force_run":
        return jsonify(
            {
                "success": True,
                "action": action,
                "affected": affected,
                "jobs": queued_jobs,
                "skipped": skipped + errors,
            }
        )

    db.session.commit()
    return jsonify(
        {
            "success": True,
            "action": action,
            "affected": affected,
            "skipped": skipped + errors,
        }
    )


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

    auto_create_agent = payload.get("auto_create_agent", False)
    if isinstance(auto_create_agent, str):
        auto_create_agent = auto_create_agent.lower() in ("true", "1", "yes")
    else:
        auto_create_agent = bool(auto_create_agent)

    troll_mode = str(payload.get("troll_mode") or "chance").strip() or "chance"
    if troll_mode not in ("chance", "troll", "no_troll"):
        return (
            jsonify({"success": False, "error": f"Unknown troll_mode '{troll_mode}'"}),
            400,
        )

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
            troll_mode=troll_mode,
        )
        return (
            jsonify(
                {
                    "success": True,
                    "users": result["users"],
                    "agents": result["agents"],
                    "skipped": result.get("skipped", 0),
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
    troll_chance = Config.get("TROLL_USER_CHANCE") or "0.1"
    return render_template("admin/agents.html", troll_chance=troll_chance)


@admin_bp.route("/agents/<int:agent_id>")
@production_disabled
@admin_required
def agent_detail(agent_id):
    """Single-agent detail page addressed by numeric agent id."""
    return render_template("admin/agent_detail.html", agent_id=agent_id)


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

    moderator = _moderator_user()
    if moderator is None:
        flash(
            "Moderation actions require the 'admin' user to exist; create it first.",
            "error",
        )
        return redirect(url_for("admin.reports"))

    try:
        report = moderation.remove_report(
            report_id, moderator=moderator.username, removal_reason=removal_reason
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

    moderator = _moderator_user()
    if moderator is None:
        flash(
            "Moderation actions require the 'admin' user to exist; create it first.",
            "error",
        )
        return redirect(url_for("admin.reports"))

    try:
        report = moderation.dismiss_report(
            report_id, moderator=moderator.username, note=note
        )
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

    moderator = _moderator_user()
    if moderator is None:
        flash(
            "Moderation actions require the 'admin' user to exist; create it first.",
            "error",
        )
        return redirect(url_for("admin.reports"))

    try:
        ban = moderation.ban_user(
            username,
            reason,
            subdeaddit_name=subdeaddit_name,
            expires_at=expires_at,
            banned_by=moderator.username,
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
                    "updated_at": p.updated_at.isoformat() if p.updated_at else None,
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
    from deaddit.llm.prompts import PromptError, create_version, get_template

    if get_template(name) is None:
        return jsonify({"error": f"Unknown prompt template {name!r}"}), 404
    data = request.get_json(silent=True) or {}
    body = data.get("body")
    if not body or not isinstance(body, str):
        return jsonify({"error": "Field 'body' (non-empty string) is required"}), 400
    try:
        row = create_version(name, body, created_by=data.get("created_by"))
    except PromptError as exc:
        # The template exists; failure here is body validation.
        return jsonify({"error": str(exc)}), 400
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
    target_kind = data.get("target_kind", "")
    template_name = data.get("template", "")
    target_key = data.get("target_key", "")
    # The visit-profile resolver has exactly one global target key. Normalize
    # this at the API boundary so neither a hand-written request nor an older
    # admin page can create an inert global rollout pin.
    if target_kind == "global" and template_name == _PROFILE_TEMPLATE:
        target_key = _PROFILE_TEMPLATE
    try:
        set_pin(
            target_kind,
            target_key,
            template_name,
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

    ``?limit=<n>`` (default 50, max 500). ``subject_key`` is the agent's
    decimal id (for example, ``"42"``); this endpoint lists the audit rows.
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


# --- Prompt-builder Phase 5: visit-profile validation, preview, rollout ---
#
# ``agent.visit_profile`` is the only template with preview semantics: its
# versions are validated immutable documents, and the preview endpoint runs
# the exact runtime preparation path (``prepare_agent_visit``) with a seeded
# RNG and no persistence, so a reviewer can inspect behavior before pinning.

_PROFILE_TEMPLATE = "agent.visit_profile"
_PREVIEW_INTENTS = frozenset({"browse", "post", "image", "website"})


def _profile_leaf_diff(path, effective, preview):
    """One leaf-level change entry, or None when the values are equal."""
    if effective == preview:
        return None
    return {
        "path": path,
        "change": "modified",
        "effective": effective,
        "preview": preview,
    }


def _profile_diff(effective, preview, path=""):
    """Structural diff of two canonical profile documents.

    Lists of ``{"id": ...}`` items diff by stable id, so reordering or
    editing one catalog entry yields one keyed entry instead of an
    index-shifted cascade. Other leaves compare wholesale.
    """
    if isinstance(effective, dict) and isinstance(preview, dict):
        entries = []
        for key in sorted(set(effective) | set(preview)):
            child = f"{path}.{key}" if path else str(key)
            if key not in effective:
                entries.append(
                    {"path": child, "change": "added", "preview": preview[key]}
                )
            elif key not in preview:
                entries.append(
                    {"path": child, "change": "removed", "effective": effective[key]}
                )
            else:
                entries.extend(_profile_diff(effective[key], preview[key], child))
        return entries
    if (
        isinstance(effective, list)
        and isinstance(preview, list)
        and all(isinstance(i, dict) and "id" in i for i in effective + preview)
    ):
        effective_by_id = {item["id"]: item for item in effective}
        preview_by_id = {item["id"]: item for item in preview}
        entries = []
        for item_id in sorted(set(effective_by_id) | set(preview_by_id)):
            child = f"{path}[{item_id}]"
            if item_id not in effective_by_id:
                entries.append(
                    {
                        "path": child,
                        "change": "added",
                        "preview": preview_by_id[item_id],
                    }
                )
            elif item_id not in preview_by_id:
                entries.append(
                    {
                        "path": child,
                        "change": "removed",
                        "effective": effective_by_id[item_id],
                    }
                )
            else:
                entries.extend(
                    _profile_diff(
                        effective_by_id[item_id], preview_by_id[item_id], child
                    )
                )
        return entries
    entry = _profile_leaf_diff(path or "$", effective, preview)
    return [entry] if entry else []


def _preview_warnings(plan, requested_intent):
    """Derive the same conditions runtime logs as reviewer-facing warnings."""
    warnings = []
    if requested_intent in ("image", "website") and plan.intent != requested_intent:
        warnings.append(
            f"Requested intent '{requested_intent}' is ineligible for this "
            f"agent; at runtime it degrades to '{plan.intent}'."
        )
    if (
        requested_intent not in (None, "browse")
        and plan.intent == "browse"
        and plan.intent_source in ("requested", "degraded_request")
    ):
        warnings.append(
            "No post tool can be offered for this agent's capability "
            "configuration; the visit falls back to browsing."
        )
    return warnings


@admin_bp.route("/prompts")
@production_disabled
@admin_required
def prompts_page():
    """Prompt profile administration: versions, pins/rollout, and preview."""
    return render_template("admin/prompts.html")


@admin_bp.route("/api/prompts/<name>/validate", methods=["POST"])
@production_disabled
@admin_required
def prompts_validate_api(name):
    """Dry-run visit-profile validation without storing anything."""
    from deaddit.llm.prompts import (
        PromptError,
        get_template,
        parse_visit_profile,
        serialize_visit_profile,
    )

    if name != _PROFILE_TEMPLATE:
        # Unknown names 404; other real templates explain the restriction.
        if get_template(name) is None:
            return jsonify({"error": f"Unknown prompt template {name!r}"}), 404
        return jsonify(
            {"error": f"Validation supports only {_PROFILE_TEMPLATE!r}"}
        ), 400
    data = request.get_json(silent=True) or {}
    body = data.get("body")
    if not body or not isinstance(body, str):
        return jsonify({"error": "Field 'body' (non-empty string) is required"}), 400
    try:
        canonical = serialize_visit_profile(parse_visit_profile(body))
    except PromptError as exc:
        return jsonify({"valid": False, "error": str(exc)})
    return jsonify({"valid": True, "error": None, "normalized_body": canonical})


@admin_bp.route("/api/prompts/<name>/preview", methods=["POST"])
@production_disabled
@admin_required
def prompts_preview_api(name):
    """Deterministic, side-effect-free visit preview through the runtime path.

    Body: ``{agent_id, seed, requested_intent?, unread_count?, version?}``.
    ``version`` selects a stored immutable ``agent.visit_profile`` version
    to preview; without it the agent's effective profile (pin precedence
    agent > cohort > global > source default) is previewed. The diff is
    always computed against that effective profile.
    """
    from dataclasses import replace

    from deaddit.agents.prompts import DEFAULT_VISIT_PROFILE, prepare_agent_visit
    from deaddit.llm.prompts import (
        PromptError,
        get_template,
        get_version,
        parse_visit_profile,
        resolve_visit_profile,
        serialize_visit_profile,
    )

    if name != _PROFILE_TEMPLATE:
        # Unknown names 404; other real templates explain the restriction.
        if get_template(name) is None:
            return jsonify({"error": f"Unknown prompt template {name!r}"}), 404
        return jsonify({"error": f"Preview supports only {_PROFILE_TEMPLATE!r}"}), 400
    data = request.get_json(silent=True) or {}
    agent_id = data.get("agent_id")
    seed = data.get("seed")
    if isinstance(agent_id, bool) or not isinstance(agent_id, int):
        return jsonify({"error": "Field 'agent_id' (integer) is required"}), 400
    if isinstance(seed, bool) or not isinstance(seed, int):
        return jsonify({"error": "Field 'seed' (integer) is required"}), 400
    requested_intent = data.get("requested_intent")
    if requested_intent is not None and requested_intent not in _PREVIEW_INTENTS:
        return jsonify(
            {
                "error": "Field 'requested_intent' must be one of "
                "browse, post, image, website, or null"
            }
        ), 400
    unread_count = data.get("unread_count", 0)
    if (
        isinstance(unread_count, bool)
        or not isinstance(unread_count, int)
        or unread_count < 0
    ):
        return jsonify({"error": "Field 'unread_count' must be an integer >= 0"}), 400
    version = data.get("version")
    if version is not None and (
        isinstance(version, bool) or not isinstance(version, int)
    ):
        return jsonify({"error": "Field 'version' must be an integer or null"}), 400

    agent = db.session.get(Agent, agent_id)
    if agent is None:
        return jsonify({"error": f"No agent with id {agent_id}"}), 404
    user = db.session.get(User, agent.user_username)
    if user is None:
        return jsonify(
            {"error": f"Agent {agent_id} has no persona user {agent.user_username!r}"}
        ), 404

    effective_profile, _effective_row, effective_source = resolve_visit_profile(
        agent, DEFAULT_VISIT_PROFILE
    )
    if version is None:
        profile = None  # prepare_agent_visit re-resolves the effective pins
    else:
        version_row = get_version(_PROFILE_TEMPLATE, version)
        if version_row is None:
            return jsonify(
                {"error": f"Unknown version {version} of {_PROFILE_TEMPLATE!r}"}
            ), 404
        try:
            profile = replace(
                parse_visit_profile(version_row.body),
                profile_version=version_row.version,
                profile_ref=f"{_PROFILE_TEMPLATE}:v{version_row.version}",
            )
        except PromptError as exc:
            return jsonify(
                {"error": f"Stored version {version} is invalid: {exc}"}
            ), 409

    visit = prepare_agent_visit(
        agent,
        user,
        requested_intent=requested_intent,
        unread=unread_count,
        profile=profile,
        rng=random.Random(seed),
    )
    plan = visit.plan
    effective_doc = json.loads(serialize_visit_profile(effective_profile))
    preview_doc = json.loads(serialize_visit_profile(plan.profile))
    cohort = (agent.config or {}).get("cohort")
    return jsonify(
        {
            "agent": {
                "id": agent.id,
                "username": agent.user_username,
                "label": _agent_display_label(agent),
                "tier": getattr(agent.autonomy_tier, "value", agent.autonomy_tier),
                "cohort": cohort,
            },
            "requested": {
                "intent": requested_intent,
                "unread_count": unread_count,
                "seed": seed,
                "version": version,
            },
            "plan": {
                "intent": plan.intent,
                "intent_source": plan.intent_source,
                "content_kind": plan.content_kind,
                "offered_tool_names": sorted(plan.offered_tool_names),
                "length_target_id": plan.length_target_id,
                "direction_ids": list(plan.direction_ids),
                "profile_name": plan.profile_name,
                "profile_version": plan.profile_version,
                "profile_ref": plan.profile_ref,
                "resolution_source": plan.resolution_source,
            },
            "messages": [dict(message) for message in visit.messages],
            "tools": [spec.to_openai_tool() for spec in visit.tool_specs],
            "warnings": _preview_warnings(plan, requested_intent),
            "effective": {
                "profile_name": effective_profile.profile_ref.split(":v", 1)[0],
                "profile_version": effective_profile.profile_version,
                "profile_ref": effective_profile.profile_ref,
                "resolution_source": effective_source,
            },
            "diff": _profile_diff(effective_doc, preview_doc),
        }
    )
