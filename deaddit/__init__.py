"""Deaddit application package.

This module exposes :func:`create_app`, the application factory. Importing
this package performs no I/O: database creation, settings seeding, and job
restarts all happen inside :func:`create_app`.
"""

import logging
import os
from typing import Any

from flask import Flask, jsonify
from flask_migrate import upgrade as db_upgrade

# Import config after extensions are defined to avoid circular imports
from .config import Config  # noqa: E402
from .extensions import cache, db, migrate, socketio
from .logging_config import configure_logging
from .settings.service import clear as clear_settings_cache  # noqa: E402


def create_app(config: Any = None) -> Flask:
    """Construct and configure the Deaddit Flask application.

    ``config`` may be ``None``, a mapping of config overrides, or an object
    exposing a ``get`` method.
    """
    app = Flask(__name__, static_folder="static")

    # Base configuration
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///deaddit.db"

    # Wave-0 ruling: DEADDIT_DB_PATH overrides the base sqlite location;
    # explicit create_app(config=...) overrides below still win.
    db_path_override = os.environ.get("DEADDIT_DB_PATH")
    if db_path_override:
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.abspath(
            db_path_override
        )

    # The per-process settings cache must never leak values across instances
    # (tests create many apps against different databases).
    clear_settings_cache()
    # Default generated images to durable instance storage; explicit config wins.
    app.config.setdefault(
        "GENERATED_IMAGES_ROOT", os.path.join(app.instance_path, "generated_images")
    )
    if config is not None:
        if isinstance(config, dict):
            app.config.update(config)
        elif hasattr(config, "get"):
            for key in dir(config):
                if key.isupper():
                    app.config[key] = getattr(config, key)

    # Initialize extensions with the app
    db.init_app(app)
    migrate.init_app(app, db)
    cache.init_app(app)
    socketio.init_app(app)

    # Set up logging (single stdlib config; see deaddit/logging_config.py)
    configure_logging()
    logger = logging.getLogger(__name__)

    # Import routes after extensions are initialized to avoid circular imports
    from .admin import admin_bp
    from .api import bp as api_bp
    from .live import bp as live_bp
    from .routes import bp as web_bp

    # Register blueprints
    app.register_blueprint(api_bp)
    app.register_blueprint(web_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(live_bp)

    # Import websocket handlers so their @socketio.on decorators register
    from . import websocket  # noqa: F401

    with app.app_context():
        # Seed default settings; schema is owned by Alembic migrations
        # (see migrations/). Also available as `flask init-db`.
        Config.initialize_defaults()

        # Set SECRET_KEY from config system
        app.config["SECRET_KEY"] = Config.get("SECRET_KEY")
        # Configure session settings for admin authentication
        app.config["PERMANENT_SESSION_LIFETIME"] = 24 * 60 * 60  # 24 hours

        # Check API_TOKEN status using Config (database first, then environment)
        if not Config.is_api_token_set():
            logger.warning(
                "No API_TOKEN set in database or environment. Admin and API routes will be publicly accessible."
            )

    # Template context processor: config available in templates
    app.context_processor(inject_config)

    # Error handlers
    app.register_error_handler(404, not_found_error)
    app.register_error_handler(500, internal_error)
    app.register_error_handler(Exception, handle_exception)

    # CLI command: applies Alembic migrations and seeds default settings.
    @app.cli.command("init-db")
    def init_db_command():
        """Run database migrations and seed default settings."""
        init_db()

    return app


# Template context processor to make config available in templates
def inject_config():
    return {
        "config": {
            "api_token_set": Config.is_api_token_set(),
            "api_base_url": Config.get("API_BASE_URL"),
            "openai_api_url": Config.get("OPENAI_API_URL"),
            "openai_model": Config.get("OPENAI_MODEL"),
            "openai_key_set": bool(Config.get("OPENAI_KEY")),
        },
        "PRODUCTION": Config.get("PRODUCTION", "false").lower() == "true",
    }


# Error handlers
def not_found_error(error):
    return jsonify({"error": "Resource not found"}), 404


def internal_error(error):
    db.session.rollback()
    logger = logging.getLogger(__name__)
    logger.error(f"Internal server error: {str(error)}")
    return jsonify({"error": "Internal server error"}), 500


def handle_exception(e):
    db.session.rollback()
    logger = logging.getLogger(__name__)
    logger.error(f"Unhandled exception: {str(e)}")
    return jsonify({"error": "An unexpected error occurred"}), 500


def init_db() -> None:
    """Run database migrations and seed default settings. Used by `flask init-db`.

    Must be called inside an application context.
    """
    db_upgrade()
    Config.initialize_defaults()
