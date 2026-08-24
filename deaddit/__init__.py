import logging
import os

from flask import Flask, jsonify, request
from flask_caching import Cache
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__, static_folder="static")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///deaddit.db"

# Configure caching
app.config["CACHE_TYPE"] = "simple"  # Use simple in-memory cache for single-user app
app.config["CACHE_DEFAULT_TIMEOUT"] = 300  # 5 minutes default timeout

db = SQLAlchemy(app)
cache = Cache(app)
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    ping_timeout=60,
    ping_interval=25,
    logger=False,
    engineio_logger=False,
    allow_upgrades=False,
    transports=["polling"],
)

# Get the API token from environment variable (will be updated to use Config after database is ready)
API_TOKEN = os.environ.get("API_TOKEN")

# Set up logging
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# Initial warning based on environment variable (will be checked again after Config is loaded)
if not API_TOKEN:
    logger.warning(
        "No API_TOKEN set in environment. API routes will be publicly accessible."
    )


@app.before_request
def authenticate():
    if request.path.startswith("/api/ingest"):
        token = request.headers.get("Authorization")
        # Use Config to get API_TOKEN (database first, then environment)
        api_token = None
        try:
            from .config import Config

            api_token = Config.get("API_TOKEN")
        except Exception:
            # Fallback to environment if Config isn't available yet
            api_token = API_TOKEN

        if api_token and (not token or token != f"Bearer {api_token}"):
            return jsonify({"error": "Unauthorized"}), 401


# Import config after app is created to avoid circular imports
from .config import Config  # noqa: E402


# Template context processor to make config available in templates
@app.context_processor
def inject_config():
    return {
        "config": {
            "api_token_set": Config.is_api_token_set(),
            "api_base_url": Config.get("API_BASE_URL"),
            "openai_api_url": Config.get("OPENAI_API_URL"),
            "openai_model": Config.get("OPENAI_MODEL"),
            "openai_key_set": bool(Config.get("OPENAI_KEY")),
        },
        "PRODUCTION": Config.get("PRODUCTION", "false").lower() == "true"
    }


with app.app_context():
    db.create_all()
    # Set SECRET_KEY from config system
    app.config["SECRET_KEY"] = Config.get("SECRET_KEY")
    # Configure session settings for admin authentication
    app.config["PERMANENT_SESSION_LIFETIME"] = 24 * 60 * 60  # 24 hours
    # Initialize default settings if database is empty
    Config.initialize_defaults()

    # Check API_TOKEN status using Config (database first, then environment)
    if not Config.is_api_token_set():
        logger.warning(
            "No API_TOKEN set in database or environment. Admin and API routes will be publicly accessible."
        )
    
    # Restart any pending jobs after app restart
    try:
        from .jobs import restart_pending_jobs
        restart_pending_jobs()
    except Exception as e:
        logger.error(f"Failed to restart pending jobs: {e}")


# Error handlers
@app.errorhandler(404)
def not_found_error(error):
    return jsonify({"error": "Resource not found"}), 404


@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    logger.error(f"Internal server error: {str(error)}")
    return jsonify({"error": "Internal server error"}), 500


@app.errorhandler(Exception)
def handle_exception(e):
    db.session.rollback()
    logger.error(f"Unhandled exception: {str(e)}")
    return jsonify({"error": "An unexpected error occurred"}), 500


# Import routes and handlers after app/db initialization
from . import websocket  # noqa: E402, F401
from .admin import admin_bp  # noqa: E402
from .api import *  # noqa: E402, F403
from .routes import *  # noqa: E402, F403

# Register admin blueprint
app.register_blueprint(admin_bp)
