import json
from datetime import datetime
from enum import Enum
from pathlib import Path

from sqlalchemy import event

from deaddit.extensions import db


class Subdeaddit(db.Model):
    name = db.Column(db.String(50), primary_key=True)
    description = db.Column(db.Text)
    post_types = db.Column(db.Text)
    created_at = db.Column(db.DateTime)  # Phase D5: history seeding

    def get_post_types(self):
        return json.loads(self.post_types) if self.post_types else []

    def set_post_types(self, post_types_list):
        self.post_types = json.dumps(post_types_list)


class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    score = db.Column(db.Integer, nullable=False, server_default="0")
    vote_count = db.Column(db.Integer, nullable=False, server_default="0")
    content = db.Column(db.Text)
    subdeaddit_name = db.Column(
        db.String(50), db.ForeignKey("subdeaddit.name"), nullable=False, index=True
    )
    user = db.Column(
        db.String(50), db.ForeignKey("user.username"), nullable=False, index=True
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    model = db.Column(db.String(100), index=True)
    # LLM that generated this content (e.g. 'qwen3-32b'); None = not
    # recorded (seeded/legacy rows). Distinct from `model` above, which
    # stores provenance ('agent:<username>' / 'seed').
    llm_model = db.Column(db.String(100))
    post_type = db.Column(db.String(50), index=True)

    subdeaddit = db.relationship("Subdeaddit", backref=db.backref("posts", lazy=True))
    comments = db.relationship("Comment", back_populates="post", lazy="dynamic")

    __table_args__ = (
        db.Index("ix_post_subdeaddit_name_created_at", "subdeaddit_name", "created_at"),
        db.Index("ix_post_model_created_at", "model", "created_at"),
    )
    # --- Phase D4 moderation: soft removal ---
    removed = db.Column(db.Boolean, default=False, index=True)
    removed_by = db.Column(db.String(50), db.ForeignKey("user.username"), nullable=True)
    removal_reason = db.Column(db.Text, nullable=True)
    removed_at = db.Column(db.DateTime, nullable=True)


class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(
        db.Integer, db.ForeignKey("post.id"), nullable=False, index=True
    )
    parent_id = db.Column(
        db.Integer, db.ForeignKey("comment.id"), nullable=True, index=True
    )
    content = db.Column(db.Text)
    score = db.Column(db.Integer, nullable=False, server_default="0", index=True)
    vote_count = db.Column(db.Integer, nullable=False, server_default="0")
    user = db.Column(
        db.String(50), db.ForeignKey("user.username"), nullable=False, index=True
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    model = db.Column(db.String(100), index=True)
    # Same contract as Post.llm_model: the generating LLM, None if unknown.
    llm_model = db.Column(db.String(100))

    post = db.relationship("Post", back_populates="comments")

    __table_args__ = (
        db.Index("ix_comment_post_id_created_at", "post_id", "created_at"),
    )
    # --- Phase D4 moderation: soft removal ---
    removed = db.Column(db.Boolean, default=False, index=True)
    removed_by = db.Column(db.String(50), db.ForeignKey("user.username"), nullable=True)
    removal_reason = db.Column(db.Text, nullable=True)
    removed_at = db.Column(db.DateTime, nullable=True)


class User(db.Model):
    username = db.Column(db.String(50), primary_key=True)
    age = db.Column(db.Integer)
    gender = db.Column(db.String(10))
    bio = db.Column(db.Text)
    interests = db.Column(db.Text)
    occupation = db.Column(db.String(100))
    education = db.Column(db.String(100))
    writing_style = db.Column(db.Text)
    personality_traits = db.Column(db.Text)
    is_troll = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    model = db.Column(db.String(100))
    post_karma = db.Column(db.Integer, nullable=False, server_default="0")
    comment_karma = db.Column(db.Integer, nullable=False, server_default="0")
    created_at = db.Column(db.DateTime)  # Phase D5: history seeding
    agent_state = db.Column(db.JSON, nullable=False, default=dict, server_default="{}")

    posts = db.relationship(
        "Post", backref="author", lazy="dynamic", foreign_keys="Post.user"
    )
    comments = db.relationship(
        "Comment", backref="author", lazy="dynamic", foreign_keys="Comment.user"
    )

    def get_interests(self):
        return json.loads(self.interests)

    def get_personality_traits(self):
        return json.loads(self.personality_traits)


class JobType(Enum):
    BATCH_OPERATION = "batch_operation"


class JobStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.Enum(JobType), nullable=False)
    status = db.Column(db.Enum(JobStatus), default=JobStatus.PENDING)
    priority = db.Column(db.Integer, default=5)
    progress = db.Column(db.Integer, default=0)
    total_items = db.Column(db.Integer, default=1)
    parameters = db.Column(db.JSON)
    result = db.Column(db.JSON)
    error_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    estimated_completion = db.Column(db.DateTime)
    rq_job_id = db.Column(db.String(36), unique=True, index=True)
    claimed_at = db.Column(db.DateTime)
    worker_id = db.Column(db.String(64))
    heartbeat_at = db.Column(db.DateTime)

    __table_args__ = (db.Index("ix_job_status_priority", "status", "priority"),)

    def to_dict(self):
        return {
            "id": self.id,
            "type": self.type.value if self.type else None,
            "status": self.status.value if self.status else None,
            "priority": self.priority,
            "progress": self.progress,
            "total_items": self.total_items,
            "parameters": self.parameters,
            "result": self.result,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat()
            if self.completed_at
            else None,
            "estimated_completion": self.estimated_completion.isoformat()
            if self.estimated_completion
            else None,
            "rq_job_id": self.rq_job_id,
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at else None,
            "worker_id": self.worker_id,
            "heartbeat_at": self.heartbeat_at.isoformat()
            if self.heartbeat_at
            else None,
        }


class ApiModel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    api_url = db.Column(db.String(255), nullable=False, index=True)
    model_name = db.Column(db.String(100), nullable=False)
    last_fetched = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)

    __table_args__ = (db.UniqueConstraint("api_url", "model_name"),)

    @staticmethod
    def get_models_for_api(api_url):
        """Get all active models for a specific API endpoint."""
        return ApiModel.query.filter_by(api_url=api_url, is_active=True).all()

    @staticmethod
    def update_models_for_api(api_url, model_names):
        """Update the models for a specific API endpoint."""
        # Mark all existing models as inactive
        ApiModel.query.filter_by(api_url=api_url).update({"is_active": False})

        # Add or reactivate models
        for model_name in model_names:
            existing = ApiModel.query.filter_by(
                api_url=api_url, model_name=model_name
            ).first()
            if existing:
                existing.is_active = True
                existing.last_fetched = datetime.utcnow()
            else:
                new_model = ApiModel(
                    api_url=api_url,
                    model_name=model_name,
                    last_fetched=datetime.utcnow(),
                    is_active=True,
                )
                db.session.add(new_model)

        db.session.commit()

    def to_dict(self):
        return {
            "id": self.id,
            "api_url": self.api_url,
            "model_name": self.model_name,
            "last_fetched": self.last_fetched.isoformat()
            if self.last_fetched
            else None,
            "is_active": self.is_active,
        }


class LLMProvider(db.Model):
    """Configured LLM Provider storing API endpoint, key, default model, and default flag."""

    __tablename__ = "llm_provider"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    api_url = db.Column(db.String(255), nullable=False, index=True)
    api_key = db.Column(db.String(255), nullable=True)
    default_model = db.Column(db.String(100), nullable=True)
    is_default = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def to_dict(self, include_key=False):
        data = {
            "id": self.id,
            "name": self.name,
            "api_url": self.api_url,
            "default_model": self.default_model,
            "is_default": self.is_default,
            "has_key": bool(self.api_key and self.api_key.strip()),
            "key_last4": self.api_key.strip()[-4:]
            if (self.api_key and self.api_key.strip())
            else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_key:
            data["api_key"] = self.api_key
        return data

    @classmethod
    def get_default(cls):
        """Get the default provider, or the first provider if none marked default."""
        provider = cls.query.filter_by(is_default=True).first()
        if provider is None:
            provider = cls.query.order_by(cls.id.asc()).first()
            if provider is not None and not provider.is_default:
                provider.is_default = True
                db.session.commit()
        return provider

    @classmethod
    def set_default(cls, provider_id: int):
        """Mark provider_id as default and unmark all others."""
        providers = cls.query.all()
        target = None
        for p in providers:
            if p.id == provider_id:
                p.is_default = True
                target = p
            else:
                p.is_default = False
        db.session.commit()
        return target


class ApiEndpointConfig(db.Model):
    """Store configuration settings per API endpoint."""

    id = db.Column(db.Integer, primary_key=True)
    api_url = db.Column(db.String(255), nullable=False, unique=True, index=True)
    default_model = db.Column(db.String(100))
    last_updated = db.Column(db.DateTime, default=datetime.utcnow)

    @staticmethod
    def get_default_model_for_endpoint(api_url):
        """Get the default model for a specific API endpoint."""
        config = ApiEndpointConfig.query.filter_by(api_url=api_url).first()
        return config.default_model if config else None

    @staticmethod
    def set_default_model_for_endpoint(api_url, model_name):
        """Set the default model for a specific API endpoint."""
        config = ApiEndpointConfig.query.filter_by(api_url=api_url).first()
        if config:
            config.default_model = model_name
            config.last_updated = datetime.utcnow()
        else:
            config = ApiEndpointConfig(
                api_url=api_url,
                default_model=model_name,
                last_updated=datetime.utcnow(),
            )
            db.session.add(config)
        db.session.commit()
        return config

    def to_dict(self):
        return {
            "id": self.id,
            "api_url": self.api_url,
            "default_model": self.default_model,
            "last_updated": self.last_updated.isoformat()
            if self.last_updated
            else None,
        }


class EndpointCapability(db.Model):
    """Cached per-endpoint/per-model capability verdicts (Phase LLM-2).

    Rows are written by deaddit.llm.capabilities.probe_endpoint or by a
    human override (probe_method='manual', which always wins over probes).
    ``supports_streaming`` stays NULL until streaming lands in Phase 4.

    ``supports_vision`` (Phase 5A) is a separate three-state verdict for
    image-input support: NULL means unknown, which callers must treat
    conservatively as non-vision. It is probed and overridden independently
    of the tools verdict above, via its own ``vision_probed_at`` /
    ``vision_probe_method`` columns, so a vision action never touches
    ``supports_tools``, ``supports_streaming``, ``probed_at`` or
    ``probe_method``.
    """

    api_url = db.Column(db.String(255), primary_key=True)
    model_name = db.Column(db.String(100), primary_key=True)
    supports_tools = db.Column(db.Boolean, nullable=False)
    supports_streaming = db.Column(db.Boolean, nullable=True)
    context_tokens = db.Column(db.Integer, nullable=True)
    probed_at = db.Column(db.DateTime)
    probe_method = db.Column(db.String(20))  # 'probe' | 'declared' | 'manual'
    supports_vision = db.Column(db.Boolean, nullable=True)
    vision_probed_at = db.Column(db.DateTime, nullable=True)
    vision_probe_method = db.Column(db.String(20), nullable=True)  # 'probe' | 'manual'


class Setting(db.Model):
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    @staticmethod
    def get_value(key, default=None):
        """Get a setting value by key, returning default if not found."""
        setting = db.session.get(Setting, key)
        return setting.value if setting else default

    @staticmethod
    def set_value(key, value, description=None):
        """Set a setting value, creating or updating as needed."""
        setting = db.session.get(Setting, key)
        if setting:
            setting.value = value
            if description:
                setting.description = description
            setting.updated_at = datetime.utcnow()
        else:
            setting = Setting(key=key, value=value, description=description)
            db.session.add(setting)
        db.session.commit()
        return setting

    def to_dict(self):
        return {
            "key": self.key,
            "value": self.value,
            "description": self.description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# --- LLM accounting & routing ---
class LLMUsage(db.Model):
    """One row per LLM provider attempt, including failed attempts."""

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    request_id = db.Column(db.String(32))
    attempt = db.Column(db.Integer)
    api_url = db.Column(db.String(255))
    model = db.Column(db.String(120))
    action = db.Column(db.String(40), nullable=True)
    agent = db.Column(db.String(120), nullable=True)
    status = db.Column(db.String(10))  # 'ok' | 'failed'
    error_type = db.Column(db.String(80), nullable=True)  # exception class name
    prompt_tokens = db.Column(db.Integer, nullable=True)
    completion_tokens = db.Column(db.Integer, nullable=True)
    total_tokens = db.Column(db.Integer, nullable=True)
    estimated_cost = db.Column(
        db.Float, nullable=True
    )  # USD; NULL = unknown price (never fake $0); 0.0 exactly = local/free endpoint
    latency_ms = db.Column(db.Float, nullable=True)


class ModelPrice(db.Model):
    """Dated price rows, glob pattern matched against model name."""

    id = db.Column(db.Integer, primary_key=True)
    pattern = db.Column(db.String(200), unique=True)
    prompt_price_per_1k = db.Column(db.Float)
    completion_price_per_1k = db.Column(db.Float)
    currency = db.Column(db.String(8), default="USD")
    note = db.Column(db.String(255), nullable=True)
    effective_at = db.Column(db.DateTime, default=datetime.utcnow)


class ModelRoute(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tier = db.Column(
        db.String(40), index=True, nullable=False
    )  # 'default', 'creative', 'analytical'
    api_url = db.Column(db.String(255), nullable=True)  # NULL -> Config OPENAI_API_URL
    model_name = db.Column(db.String(120), nullable=False)
    priority = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


# --- AgenticCore agent runtime ---
class Agent(db.Model):
    """An autonomous agent with fixed or random persona selection.

    Fixed agents are bound to exactly one user account via ``user_username``.
    Random agents select a persona at runtime and leave that column null.
    ``config`` holds static settings (schedule, model prefs, tool allowlist);
    ``state`` is scratch space the runtime mutates across runs (cursors,
    backoff bookkeeping). Rows are created disabled by default so a user must
    explicitly enable an agent before the scheduler picks it up.
    """

    id = db.Column(db.Integer, primary_key=True)
    persona_mode = db.Column(
        db.String(12), nullable=False, default="fixed", server_default="fixed"
    )
    user_username = db.Column(
        db.String(50),
        db.ForeignKey("user.username", ondelete="CASCADE"),
        unique=True,
        nullable=True,
    )
    autonomy_tier = db.Column(
        db.String(20), nullable=False, default="regular"
    )  # 'lurker' | 'regular' | 'power_user'
    is_enabled = db.Column(db.Boolean, nullable=False, default=False)
    status = db.Column(db.String(20), nullable=False, default="idle")
    config = db.Column(db.JSON, nullable=False, default=dict)
    state = db.Column(db.JSON, nullable=False, default=dict)
    last_run_at = db.Column(db.DateTime)
    next_run_at = db.Column(db.DateTime)
    consecutive_failures = db.Column(db.Integer, nullable=False, default=0)

    runs = db.relationship(
        "AgentRun", backref="agent", lazy="dynamic", passive_deletes=True
    )

    __table_args__ = (
        db.CheckConstraint(
            "(persona_mode = 'fixed' AND user_username IS NOT NULL) OR "
            "(persona_mode = 'random' AND user_username IS NULL)",
            name="ck_agent_persona_mode_user",
        ),
    )


class AgentRun(db.Model):
    """One execution of an agent, from trigger to terminal status."""

    __tablename__ = "agent_run"

    id = db.Column(db.Integer, primary_key=True)
    agent_id = db.Column(
        db.Integer,
        db.ForeignKey("agent.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    persona_username = db.Column(
        db.String(50),
        db.ForeignKey("user.username", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trigger = db.Column(
        db.String(20), nullable=False, default="manual"
    )  # 'manual' | 'schedule' | 'event'
    status = db.Column(
        db.String(20), nullable=False, default="running"
    )  # 'running' | 'completed' | 'failed'
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime)
    turn_count = db.Column(db.Integer, nullable=False, default=0)
    action_count = db.Column(db.Integer, nullable=False, default=0)
    token_usage = db.Column(db.JSON)  # {'prompt': n, 'completion': n, 'total': n}
    error_message = db.Column(db.Text)

    turns = db.relationship(
        "AgentTurn", backref="run", lazy="dynamic", passive_deletes=True
    )
    tool_calls = db.relationship(
        "ToolCall", backref="run", lazy="dynamic", passive_deletes=True
    )

    __table_args__ = (
        db.Index(
            "uq_agent_run_running_persona",
            "persona_username",
            unique=True,
            sqlite_where=db.text("status = 'running'"),
        ),
    )


class AgentTurn(db.Model):
    """A single LLM request/response exchange within a run."""

    __tablename__ = "agent_turn"

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(
        db.Integer,
        db.ForeignKey("agent_run.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    seq = db.Column(db.Integer, nullable=False)  # 0-based order within the run
    request_messages = db.Column(db.JSON, nullable=False)
    response_message = db.Column(db.JSON, nullable=False)
    model = db.Column(db.String(100))
    latency_ms = db.Column(db.Integer)


class ToolCall(db.Model):
    """One tool invocation made during a run, kept for audit and replay."""

    __tablename__ = "tool_call"

    id = db.Column(db.Integer, primary_key=True)
    turn_id = db.Column(
        db.Integer, db.ForeignKey("agent_turn.id", ondelete="CASCADE"), nullable=True
    )
    run_id = db.Column(
        db.Integer,
        db.ForeignKey("agent_run.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = db.Column(db.String(100), nullable=False)
    arguments = db.Column(db.JSON)
    result = db.Column(db.JSON)
    ok = db.Column(db.Boolean, nullable=False, default=True)
    error = db.Column(db.Text)
    duration_ms = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class AgentMemory(db.Model):
    """Persona-owned long-term memory (episodes, facts, and backfill).

    Memory is keyed by username and does not require a dedicated agent.
    """

    __tablename__ = "agent_memory"

    id = db.Column(db.Integer, primary_key=True)
    user_username = db.Column(
        db.String(50),
        db.ForeignKey("user.username", ondelete="CASCADE"),
        nullable=False,
    )
    kind = db.Column(db.String(20), nullable=False, default="episode")
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index(
            "ix_agent_memory_user_kind_created",
            "user_username",
            "kind",
            "created_at",
        ),
    )


# --- Platform dynamics: votes & karma ---
class Vote(db.Model):
    """A single up/down vote on a post or a comment (Phase D1)."""

    id = db.Column(db.Integer, primary_key=True)
    voter = db.Column(
        db.String(50), db.ForeignKey("user.username"), nullable=False, index=True
    )
    post_id = db.Column(db.Integer, db.ForeignKey("post.id"), nullable=True, index=True)
    comment_id = db.Column(
        db.Integer, db.ForeignKey("comment.id"), nullable=True, index=True
    )
    value = db.Column(db.SmallInteger, nullable=False)
    source = db.Column(
        db.String(16), nullable=False, server_default="agent", index=True
    )  # 'agent'|'human'|'backfill'|'simulated'
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        db.CheckConstraint("value IN (1, -1)"),
        db.CheckConstraint("(post_id IS NULL) != (comment_id IS NULL)"),
        db.UniqueConstraint("voter", "post_id", name="uq_vote_post"),
        db.UniqueConstraint("voter", "comment_id", name="uq_vote_comment"),
    )
 
 
class VoteCadencePolicy(db.Model):
    """Immutable, versioned policy used by simulated voting."""

    id = db.Column(db.Integer, primary_key=True)
    preset = db.Column(db.String(16), nullable=False)
    algorithm_version = db.Column(db.Integer, nullable=False)
    config = db.Column(db.JSON, nullable=False)
    effective_at = db.Column(db.DateTime, nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.CheckConstraint(
            "preset IN ('quiet', 'natural', 'busy', 'custom')",
            name="ck_vote_cadence_policy_preset",
        ),
    )

    VALID_PRESETS = frozenset({"quiet", "natural", "busy", "custom"})


    def __init__(self, **kwargs):
        # Validate on ORM construction as well as on database loading.  The
        # loader path below catches rows inserted outside SQLAlchemy.
        from deaddit.dynamics.engagement import validate_policy

        preset = kwargs.get("preset")
        if preset not in self.VALID_PRESETS:
            raise ValueError("invalid vote cadence policy preset")
        algorithm_version = kwargs.get("algorithm_version")
        if not isinstance(algorithm_version, int) or algorithm_version < 1:
            raise ValueError("algorithm_version must be a positive integer")
        kwargs["config"] = validate_policy(kwargs.get("config"))
        super().__init__(**kwargs)

    @property
    def validated_config(self):
        """Return a validated, detached policy configuration."""
        from deaddit.dynamics.engagement import validate_policy

        return validate_policy(self.config)

    def to_dict(self):
        return {
            "id": self.id,
            "preset": self.preset,
            "algorithm_version": self.algorithm_version,
            "config": self.validated_config,
            "effective_at": self.effective_at.isoformat()
            if self.effective_at
            else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def resolve_for_content(cls, created_at):
        """Resolve the latest policy effective when content was created."""
        return (
            cls.query.filter(cls.effective_at <= created_at)
            .order_by(cls.effective_at.desc(), cls.id.desc())
            .first()
        )

    @classmethod
    def resolve_for_exposure(cls, exposed_at):
        """Resolve the latest policy effective at a tail exposure."""
        return (
            cls.query.filter(cls.effective_at <= exposed_at)
            .order_by(cls.effective_at.desc(), cls.id.desc())
            .first()
        )

 
    @classmethod
    def resolve_for_tail_exposure(cls, exposed_at):
        """Resolve a policy for an archive/revival exposure."""
        return cls.resolve_for_exposure(exposed_at)

class VoteSimulationHourly(db.Model):
    """Cross-process counters for one UTC hour and simulator mode."""

    hour = db.Column(db.DateTime, primary_key=True)
    mode = db.Column(db.String(16), primary_key=True)
    ticks = db.Column(db.Integer, nullable=False, server_default="0")
    errors = db.Column(db.Integer, nullable=False, server_default="0")
    active_proposals = db.Column(db.Integer, nullable=False, server_default="0")
    archive_proposals = db.Column(db.Integer, nullable=False, server_default="0")
    revival_proposals = db.Column(db.Integer, nullable=False, server_default="0")
    inserted_votes = db.Column(db.Integer, nullable=False, server_default="0")
    switched_votes = db.Column(db.Integer, nullable=False, server_default="0")
    upvotes = db.Column(db.Integer, nullable=False, server_default="0")
    downvotes = db.Column(db.Integer, nullable=False, server_default="0")
    cap_skips = db.Column(db.Integer, nullable=False, server_default="0")
    min_gap_skips = db.Column(db.Integer, nullable=False, server_default="0")
    no_voter_skips = db.Column(db.Integer, nullable=False, server_default="0")
    guardrail_skips = db.Column(db.Integer, nullable=False, server_default="0")
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Short aliases make the names used by the worker's decision vocabulary
    # readable without adding duplicate persisted columns.
    @property
    def active(self):
        return self.active_proposals

    @property
    def archive(self):
        return self.archive_proposals

    @property
    def revival(self):
        return self.revival_proposals
 
 
@event.listens_for(VoteCadencePolicy, "before_update")
def _reject_vote_cadence_policy_update(mapper, connection, target):
    raise ValueError("VoteCadencePolicy rows are immutable")
 
 
@event.listens_for(VoteCadencePolicy, "load")
def _validate_loaded_vote_cadence_policy(target, context):
    from deaddit.dynamics.engagement import load_policy_config

    load_policy_config(target)


# --- Platform dynamics: notifications ---
class Notification(db.Model):
    """An inbox item for a user: reply, mention, or mod action (Phase D3)."""

    id = db.Column(db.Integer, primary_key=True)
    recipient = db.Column(
        db.String(50), db.ForeignKey("user.username"), nullable=False, index=True
    )
    kind = db.Column(db.String(16), nullable=False)  # 'reply'|'mention'|'mod_action'
    actor = db.Column(db.String(50), db.ForeignKey("user.username"), nullable=True)
    post_id = db.Column(db.Integer, db.ForeignKey("post.id"), nullable=True, index=True)
    comment_id = db.Column(db.Integer, db.ForeignKey("comment.id"), nullable=True)
    snippet = db.Column(
        db.Text, nullable=True
    )  # first ~200 chars, frozen at write time
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    read_at = db.Column(db.DateTime, nullable=True, index=True)


# --- Platform dynamics: moderation ---
class Report(db.Model):
    """A user-submitted complaint about a post or a comment (Phase D4).

    Exactly one of post_id / comment_id is set (XOR). The constraint is
    enforced by the reporting service, not the schema.
    """

    id = db.Column(db.Integer, primary_key=True)
    reporter = db.Column(
        db.String(50), db.ForeignKey("user.username"), nullable=False, index=True
    )
    post_id = db.Column(db.Integer, db.ForeignKey("post.id"), nullable=True, index=True)
    comment_id = db.Column(
        db.Integer, db.ForeignKey("comment.id"), nullable=True, index=True
    )
    reason = db.Column(db.String(500), nullable=False)
    status = db.Column(
        db.String(16), nullable=False, default="open"
    )  # 'open'|'actioned'|'dismissed' (service-enforced)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    resolved_by = db.Column(
        db.String(50), db.ForeignKey("user.username"), nullable=True
    )
    resolved_at = db.Column(db.DateTime, nullable=True)
    resolution_note = db.Column(db.Text, nullable=True)


class SubdeadditModerator(db.Model):
    """Moderator membership for a subdeaddit (Phase D4). Composite PK."""

    subdeaddit_name = db.Column(
        db.String(50), db.ForeignKey("subdeaddit.name"), primary_key=True
    )
    username = db.Column(
        db.String(50), db.ForeignKey("user.username"), primary_key=True
    )


class Ban(db.Model):
    """A ban of a user from one subdeaddit or the whole site (Phase D4).

    A NULL subdeaddit_name means a site-wide ban. An active ban is one where
    lifted_at IS NULL AND (expires_at IS NULL OR expires_at > now).
    """

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(
        db.String(50), db.ForeignKey("user.username"), nullable=False, index=True
    )
    subdeaddit_name = db.Column(
        db.String(50), db.ForeignKey("subdeaddit.name"), nullable=True
    )
    reason = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)
    lifted_at = db.Column(db.DateTime, nullable=True, index=True)


# --- UX-5: streamed job logs ---
class JobLog(db.Model):
    """One captured log line emitted while a job executed (Phase UX-5).

    Written by the worker-side ``JobLogHandler`` (deaddit.runtime.joblog) and
    streamed to the web process through these rows -- no broker involved.
    """

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("job.id"), nullable=False, index=True)
    seq = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    level = db.Column(db.String(16), nullable=False, default="INFO")
    message = db.Column(db.Text, nullable=False)

    __table_args__ = (db.Index("ix_job_log_job_seq", "job_id", "seq"),)

    def to_dict(self):
        return {
            "seq": self.seq,
            "ts": self.created_at.isoformat() if self.created_at else None,
            "level": self.level,
            "message": self.message,
        }


# --- LLM-5: prompt versioning ---
class PromptTemplate(db.Model):
    """A named prompt template; content lives only in immutable versions."""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    versions = db.relationship(
        "PromptTemplateVersion", backref="template", lazy="dynamic"
    )


class PromptTemplateVersion(db.Model):
    """One immutable revision of a prompt template body.

    Immutability is enforced by deaddit.llm.prompts's before_update guard:
    edits create version n+1; v(n) stays queryable forever.
    """

    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(
        db.Integer, db.ForeignKey("prompt_template.id"), nullable=False, index=True
    )
    version = db.Column(db.Integer, nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_by = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("template_id", "version", name="uq_prompt_version"),
    )


class PromptPin(db.Model):
    """Pins one agent or cohort to an exact prompt template version.

    ``target_kind`` is 'agent' (target_key = decimal agent id) or 'cohort'
    (target_key = cohort name). Historical pins whose agent username cannot
    be matched during migration are left unchanged. One row per target;
    re-pinning updates the row in place — render history keeps the audit
    trail.
    """

    id = db.Column(db.Integer, primary_key=True)
    target_kind = db.Column(db.String(20), nullable=False)
    target_key = db.Column(db.String(120), nullable=False)
    template_id = db.Column(
        db.Integer, db.ForeignKey("prompt_template.id"), nullable=False
    )
    version_number = db.Column(db.Integer, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        db.UniqueConstraint("target_kind", "target_key", name="uq_prompt_pin_target"),
    )


class PromptRenderAudit(db.Model):
    """Audit trail: which prompt version rendered for which subject, when.

    Written by deaddit.llm.prompts on every registry-mediated render.
    ``variables_json`` plus the stored version body reproduce the exact
    bytes (rendered_sha256 proves it) without duplicating full text.
    """

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    template_id = db.Column(
        db.Integer, db.ForeignKey("prompt_template.id"), nullable=False
    )
    template_version_id = db.Column(
        db.Integer,
        db.ForeignKey("prompt_template_version.id"),
        nullable=False,
        index=True,
    )
    subject_kind = db.Column(db.String(20), nullable=False)
    subject_key = db.Column(db.String(120))
    rendered_sha256 = db.Column(db.String(64), nullable=False)
    variables_json = db.Column(db.Text)


# --- Platform dynamics: anti-degeneracy & metrics (Phase D6) ---
class ActivityEvent(db.Model):
    """One platform action, the raw truth for the daily rollup (plan §8).

    Emitted by deaddit.dynamics.activity from the content service, vote
    service, and report service strictly AFTER their transactions commit;
    emission is failure-isolated and never blocks the action itself.
    Retention: raw rows are kept (plan §Risks — ~1 MB/month at this scale).
    """

    id = db.Column(db.Integer, primary_key=True)
    occurred_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    event_type = db.Column(
        db.String(20), nullable=False, index=True
    )  # 'post' | 'comment' | 'vote' | 'report' | 'login_session'
    username = db.Column(db.String(50), index=True)
    post_id = db.Column(db.Integer)
    comment_id = db.Column(db.Integer)
    meta = db.Column(db.Text)  # JSON we build ourselves (never model output)


class PlatformDaily(db.Model):
    """Per-UTC-day rollup of engagement, spend, and health metrics (§8).

    Written by the nightly rollup job (deaddit.dynamics.metrics), idempotent
    per day. ``llm_*`` columns join LLMUsage on day=date(created_at) per the
    LLM-3 conventions: token sums are COALESCEd to 0, but ``llm_cost_usd`` is
    NULL when no priced attempt exists that day (never fake $0), and
    ``cost_per_engagement`` is NULL when cost or engagement is absent.
    ``provenance_json`` keeps Resolution-9 provenance splits intact: post and
    comment counts bucketed by model marker ('agent:*' vs 'seed' vs other).
    """

    day = db.Column(db.Date, primary_key=True)
    posts = db.Column(db.Integer, nullable=False, server_default="0")
    comments = db.Column(db.Integer, nullable=False, server_default="0")
    votes = db.Column(db.Integer, nullable=False, server_default="0")
    reports = db.Column(db.Integer, nullable=False, server_default="0")
    active_agents = db.Column(
        db.Integer, nullable=False, server_default="0"
    )  # distinct users with >=1 event
    actions_per_active = db.Column(db.Float)  # events / active_agents
    llm_tokens_in = db.Column(db.Integer)
    llm_tokens_out = db.Column(db.Integer)
    llm_cost_usd = db.Column(db.Float)
    cost_per_engagement = db.Column(db.Float)  # llm_cost_usd / (posts+comments)
    median_thread_depth = db.Column(db.Float)
    dissent_share_avg = db.Column(db.Float)
    gini_participation_avg = db.Column(db.Float)
    provenance_json = db.Column(db.Text)


class DegeneracyFlag(db.Model):
    """One detector finding feeding the admin degeneracy watchlist (§7).

    ``kind``: 'repetition' (trigram-Jaccard echo, per write), 'echo_chamber'
    or 'brigading' (nightly scans). Hot-feed demotion derives from recent
    repetition flags by author — derived at query time, so it is idempotent.
    """

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(20), nullable=False, index=True)
    username = db.Column(db.String(50), index=True)
    subdeaddit_name = db.Column(db.String(50))
    post_id = db.Column(db.Integer)
    comment_id = db.Column(db.Integer)
    metric = db.Column(db.Float)  # max Jaccard / Gini / voter-overlap
    detail = db.Column(db.Text)  # JSON context we assemble ourselves
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


# --- Image posts (Phase 1) ---
class ImageProvider(db.Model):
    """Configured image provider and its cached model listings.

    ``api_key`` holds an admin-entered credential (the LLMProvider
    precedent): write-only through the admin UI, masked in ``to_dict``, and
    taking precedence over the ``credential_env`` environment fallback.
    """

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    provider_type = db.Column(db.String(20), nullable=False)
    api_key = db.Column(db.String(255))
    credential_env = db.Column(db.String(100), nullable=False)
    default_model = db.Column(db.String(200))
    is_enabled = db.Column(db.Boolean, nullable=False, default=True)
    is_default = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    models = db.relationship(
        "ImageModel", back_populates="provider", cascade="all, delete-orphan"
    )

    def to_dict(self):
        has_key = bool(self.api_key and self.api_key.strip())
        return {
            "id": self.id,
            "name": self.name,
            "provider_type": self.provider_type,
            "has_key": has_key,
            "key_last4": self.api_key.strip()[-4:] if has_key else None,
            "credential_env": self.credential_env,
            "default_model": self.default_model,
            "is_enabled": self.is_enabled,
            "is_default": self.is_default,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @classmethod
    def get_default(cls):
        """Get the default provider, or the first provider if none marked default."""
        provider = cls.query.filter_by(is_default=True).first()
        if provider is None:
            provider = cls.query.order_by(cls.id.asc()).first()
            if provider is not None and not provider.is_default:
                provider.is_default = True
                db.session.commit()
        return provider

    @classmethod
    def set_default(cls, provider_id: int):
        """Mark provider_id as default and unmark all others."""
        providers = cls.query.all()
        target = None
        for provider in providers:
            if provider.id == provider_id:
                provider.is_default = True
                target = provider
            else:
                provider.is_default = False
        db.session.commit()
        return target


class ImageModel(db.Model):
    """Cached model listing and compatibility metadata for an image provider."""

    id = db.Column(db.Integer, primary_key=True)
    provider_id = db.Column(
        db.Integer,
        db.ForeignKey("image_provider.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_identifier = db.Column(db.String(200), nullable=False)
    display_name = db.Column(db.String(200))
    category = db.Column(db.String(50))
    provider_metadata = db.Column(db.JSON)
    compatibility_verdict = db.Column(db.String(20))
    compatibility_reason = db.Column(db.Text)
    last_fetched = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, default=True)

    provider = db.relationship("ImageProvider", back_populates="models")

    __table_args__ = (
        db.UniqueConstraint(
            "provider_id",
            "model_identifier",
            name="uq_image_model_provider_identifier",
        ),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "provider_id": self.provider_id,
            "model_identifier": self.model_identifier,
            "display_name": self.display_name,
            "category": self.category,
            "provider_metadata": self.provider_metadata,
            "compatibility_verdict": self.compatibility_verdict,
            "compatibility_reason": self.compatibility_reason,
            "last_fetched": self.last_fetched.isoformat()
            if self.last_fetched
            else None,
            "is_active": self.is_active,
        }


class PostImage(db.Model):
    """Stored image variants and private generation provenance for a post."""

    post_id = db.Column(
        db.Integer, db.ForeignKey("post.id"), primary_key=True, nullable=False
    )
    original_path = db.Column(db.String(300), nullable=False)
    thumbnail_path = db.Column(db.String(300), nullable=False)
    mime_type = db.Column(db.String(50), nullable=False)
    byte_size = db.Column(db.Integer, nullable=False)
    width = db.Column(db.Integer, nullable=False)
    height = db.Column(db.Integer, nullable=False)
    alt_text = db.Column(db.String(500), nullable=False)
    source_prompt = db.Column(db.Text, nullable=False)
    provider_id = db.Column(
        db.Integer,
        db.ForeignKey("image_provider.id", ondelete="SET NULL"),
        nullable=True,
    )
    provider_snapshot = db.Column(db.String(100), nullable=False)
    model_snapshot = db.Column(db.String(200), nullable=False)
    request_snapshot = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    provider = db.relationship(
        "ImageProvider",
        backref=db.backref("post_images", passive_deletes=True),
    )

    def to_dict(self):
        return {
            "original_url": Path(self.original_path).name,
            "thumbnail_url": Path(self.thumbnail_path).name,
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
            "alt_text": self.alt_text,
        }


Post.image = db.relationship(
    "PostImage", backref="post", uselist=False, cascade="all, delete-orphan"
)


class GeneratedWebsite(db.Model):
    """The stored HTML file and private generation provenance for a website post.

    One row per link post produced by the ``create_website`` agent tool (see
    ``aidocs/CREATE_WEBSITE_TOOL_PLAN.md``, "Data model and migration").
    ``public_path`` is the normalized, user-facing ``hostname/page-name.html``
    served under ``/out/``; ``storage_path`` is the opaque ``pages/<uuid>.html``
    location on disk, never derived from request input. Every field besides
    ``hostname``, ``page_name``, and the derived public URL is private
    generation provenance - see :meth:`to_public_dict`, which is the only
    sanctioned public/API-facing view of this model.
    """

    __tablename__ = "generated_website"

    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(
        db.Integer,
        db.ForeignKey("post.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    public_path = db.Column(db.String(400), nullable=False, unique=True, index=True)
    storage_path = db.Column(db.String(300), nullable=False, unique=True)
    hostname = db.Column(db.String(253), nullable=False)
    page_name = db.Column(db.String(160), nullable=False)
    source_description = db.Column(db.Text, nullable=False)
    byte_size = db.Column(db.Integer, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    agent_id = db.Column(
        db.Integer,
        db.ForeignKey("agent.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    creator_username_snapshot = db.Column(db.String(50), nullable=False)
    agent_run_id = db.Column(
        db.Integer,
        db.ForeignKey("agent_run.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Effective endpoint URL only - never a key or authorization value. Keep
    # it that way; see AGENTS.md "Secrets" and the spec's explicit invariant.
    api_url_snapshot = db.Column(db.String(255), nullable=False)
    model_snapshot = db.Column(db.String(120), nullable=False)
    request_id = db.Column(db.String(32), nullable=True)
    prompt_tokens = db.Column(db.Integer, nullable=True)
    completion_tokens = db.Column(db.Integer, nullable=True)
    total_tokens = db.Column(db.Integer, nullable=True)
    finish_reason = db.Column(db.String(40), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    agent = db.relationship(
        "Agent", backref=db.backref("generated_websites", passive_deletes=True)
    )
    agent_run = db.relationship(
        "AgentRun", backref=db.backref("generated_websites", passive_deletes=True)
    )

    def to_public_dict(self):
        """The sanctioned public/API-facing view: no provenance, ever.

        Only ``url``, ``hostname``, and ``page_name`` are safe to return from
        a public route or API payload. Do not add ``source_description``,
        ``api_url_snapshot``, ``model_snapshot``, ``request_id``, token
        counts, ``finish_reason``, or ``storage_path`` here - those are
        private provenance and leaking them here would defeat Phase 4's
        redaction work.
        """
        return {
            "url": f"/out/{self.public_path}",
            "hostname": self.hostname,
            "page_name": self.page_name,
        }


Post.website = db.relationship(
    "GeneratedWebsite", backref="post", uselist=False, cascade="all, delete-orphan"
)
