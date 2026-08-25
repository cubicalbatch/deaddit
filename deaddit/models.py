import json
from datetime import datetime
from enum import Enum

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
    upvote_count = db.Column(db.Integer, default=0)
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
    post_type = db.Column(db.String(50), index=True)

    subdeaddit = db.relationship("Subdeaddit", backref=db.backref("posts", lazy=True))
    comments = db.relationship("Comment", back_populates="post", lazy="dynamic")

    __table_args__ = (
        db.Index("ix_post_subdeaddit_name_created_at", "subdeaddit_name", "created_at"),
        db.Index("ix_post_model_created_at", "model", "created_at"),
    )
    # --- Phase D4 moderation: soft removal ---
    removed = db.Column(db.Boolean, default=False, index=True)
    removed_by = db.Column(
        db.String(50), db.ForeignKey("user.username"), nullable=True
    )
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
    upvote_count = db.Column(db.Integer, default=0, index=True)
    score = db.Column(db.Integer, nullable=False, server_default="0")
    vote_count = db.Column(db.Integer, nullable=False, server_default="0")
    user = db.Column(
        db.String(50), db.ForeignKey("user.username"), nullable=False, index=True
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    model = db.Column(db.String(100), index=True)

    post = db.relationship("Post", back_populates="comments")

    __table_args__ = (
        db.Index("ix_comment_post_id_created_at", "post_id", "created_at"),
    )
    # --- Phase D4 moderation: soft removal ---
    removed = db.Column(db.Boolean, default=False, index=True)
    removed_by = db.Column(
        db.String(50), db.ForeignKey("user.username"), nullable=True
    )
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
    model = db.Column(db.String(100))
    post_karma = db.Column(db.Integer, nullable=False, server_default="0")
    comment_karma = db.Column(db.Integer, nullable=False, server_default="0")
    created_at = db.Column(db.DateTime)  # Phase D5: history seeding

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
    CREATE_SUBDEADDIT = "create_subdeaddit"
    CREATE_USER = "create_user"
    CREATE_POST = "create_post"
    CREATE_COMMENT = "create_comment"
    BATCH_OPERATION = "batch_operation"
    SCHEDULED_TASK = "scheduled_task"
    CONTENT_CLEANUP = "content_cleanup"


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


class GenerationTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    type = db.Column(db.Enum(JobType), nullable=False)
    parameters = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "type": self.type.value if self.type else None,
            "parameters": self.parameters,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
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
    """

    api_url = db.Column(db.String(255), primary_key=True)
    model_name = db.Column(db.String(100), primary_key=True)
    supports_tools = db.Column(db.Boolean, nullable=False)
    supports_streaming = db.Column(db.Boolean, nullable=True)
    context_tokens = db.Column(db.Integer, nullable=True)
    probed_at = db.Column(db.DateTime)
    probe_method = db.Column(db.String(20))  # 'probe' | 'declared' | 'manual'


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
        setting = Setting.query.get(key)
        return setting.value if setting else default

    @staticmethod
    def set_value(key, value, description=None):
        """Set a setting value, creating or updating as needed."""
        setting = Setting.query.get(key)
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
    api_url = db.Column(
        db.String(255), nullable=True
    )  # NULL -> Config OPENAI_API_URL
    model_name = db.Column(db.String(120), nullable=False)
    priority = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


# --- AgenticCore agent runtime ---
class Agent(db.Model):
    """An autonomous agent bound to exactly one user account.

    ``config`` holds static settings (schedule, model prefs, tool allowlist);
    ``state`` is scratch space the runtime mutates across runs (cursors,
    backoff bookkeeping). Rows are created disabled by default so a user must
    explicitly enable an agent before the scheduler picks it up.
    """

    id = db.Column(db.Integer, primary_key=True)
    user_username = db.Column(
        db.String(50), db.ForeignKey("user.username"), unique=True, nullable=False
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

    runs = db.relationship("AgentRun", backref="agent", lazy="dynamic")


class AgentRun(db.Model):
    """One execution of an agent, from trigger to terminal status."""

    __tablename__ = "agent_run"

    id = db.Column(db.Integer, primary_key=True)
    agent_id = db.Column(
        db.Integer, db.ForeignKey("agent.id"), nullable=False, index=True
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

    turns = db.relationship("AgentTurn", backref="run", lazy="dynamic")
    tool_calls = db.relationship("ToolCall", backref="run", lazy="dynamic")


class AgentTurn(db.Model):
    """A single LLM request/response exchange within a run."""

    __tablename__ = "agent_turn"

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(
        db.Integer, db.ForeignKey("agent_run.id"), nullable=False, index=True
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
    turn_id = db.Column(db.Integer, db.ForeignKey("agent_turn.id"), nullable=True)
    run_id = db.Column(
        db.Integer, db.ForeignKey("agent_run.id"), nullable=False, index=True
    )
    name = db.Column(db.String(100), nullable=False)
    arguments = db.Column(db.JSON)
    result = db.Column(db.JSON)
    ok = db.Column(db.Boolean, nullable=False, default=True)
    error = db.Column(db.Text)
    duration_ms = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class AgentMemory(db.Model):
    """Long-lived note attached to an agent (episodes, facts, summaries)."""

    __tablename__ = "agent_memory"

    id = db.Column(db.Integer, primary_key=True)
    agent_id = db.Column(
        db.Integer, db.ForeignKey("agent.id"), nullable=False, index=True
    )
    kind = db.Column(db.String(20), nullable=False, default="episode")
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


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
    )  # 'agent'|'human'|'backfill'
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        db.CheckConstraint("value IN (1, -1)"),
        db.CheckConstraint("(post_id IS NULL) != (comment_id IS NULL)"),
        db.UniqueConstraint("voter", "post_id", name="uq_vote_post"),
        db.UniqueConstraint("voter", "comment_id", name="uq_vote_comment"),
    )


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
    job_id = db.Column(
        db.Integer, db.ForeignKey("job.id"), nullable=False, index=True
    )
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

    ``target_kind`` is 'agent' (target_key = agent username) or 'cohort'
    (target_key = cohort name). One row per target; re-pinning updates
    the row in place — render history keeps the audit trail.
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
