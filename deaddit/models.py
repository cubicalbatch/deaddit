import json
from datetime import datetime
from enum import Enum

from deaddit.extensions import db


class Subdeaddit(db.Model):
    name = db.Column(db.String(50), primary_key=True)
    description = db.Column(db.Text)
    post_types = db.Column(db.Text)

    def get_post_types(self):
        return json.loads(self.post_types) if self.post_types else []

    def set_post_types(self, post_types_list):
        self.post_types = json.dumps(post_types_list)


class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    upvote_count = db.Column(db.Integer, default=0)
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
        db.Index(
            "ix_post_subdeaddit_name_created_at", "subdeaddit_name", "created_at"
        ),
        db.Index("ix_post_model_created_at", "model", "created_at"),
    )


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
    user = db.Column(
        db.String(50), db.ForeignKey("user.username"), nullable=False, index=True
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    model = db.Column(db.String(100), index=True)

    post = db.relationship("Post", back_populates="comments")

    __table_args__ = (
        db.Index("ix_comment_post_id_created_at", "post_id", "created_at"),
    )


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

    posts = db.relationship("Post", backref="author", lazy="dynamic")
    comments = db.relationship("Comment", backref="author", lazy="dynamic")

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

    __table_args__ = (
        db.Index("ix_job_status_priority", "status", "priority"),
    )

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
