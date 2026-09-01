"""Configuration management for Deaddit.

Non-secret settings resolve database first, then environment variables, then
built-in defaults (DB > env > defaults), served through the process-local TTL
cache in :mod:`deaddit.settings.service`.

Secret keys (``API_TOKEN``, ``SECRET_KEY``, ``OPENAI_KEY`` and every
``API_KEY_*`` endpoint key) are environment-only: :meth:`Config.set` refuses to
persist them, and they resolve strictly from the environment or defaults.
"""

import logging
import os
from datetime import datetime

from deaddit.models import Setting
from deaddit.settings.service import SecretNotPersistable, cached, invalidate

logger = logging.getLogger(__name__)

SECRET_KEYS = frozenset({"API_TOKEN", "SECRET_KEY", "OPENAI_KEY"})

# Sentinel: no DB/env/DEFAULTS layer answered for a non-secret key.
_UNSET = object()


def is_secret_key(key: str) -> bool:
    """True when the setting holds a credential and must never be persisted."""
    return key in SECRET_KEYS or key.startswith("API_KEY_")


class Config:
    """Configuration manager that loads from database with environment fallbacks."""

    # Default values for configuration. Secret keys stay None: credentials are
    # never defaulted into the database and only ever come from the environment.
    DEFAULTS = {
        "OPENAI_API_URL": "http://localhost/v1",
        "OPENAI_KEY": None,
        "OPENAI_MODEL": "llama3",
        "SECRET_KEY": "dev-secret-key-change-in-production",
        "API_TOKEN": None,
        "PRODUCTION": "false",
        "SEED_VOTE_MAX": "150",
        "SEED_VOTE_PROBABILITY": "1.0",
        "SEED_DECAY_DAYS": "30",
        "SEED_ANCHOR_AT": None,
        "AGENT_RUNTIME_ENABLED": "false",
        "NIGHT_LULL_ENABLED": "true",
        "NIGHT_LULL_START_HOUR": "1",
        "NIGHT_LULL_END_HOUR": "7",
        "NIGHT_LULL_DELAY_MULTIPLIER": "4.0",
        "WEBSITE_MAX_OUTPUT_TOKENS": "32768",
        "WEBSITE_GENERATION_TIMEOUT_SECONDS": "300",
        "WEBSITE_MAX_HTML_BYTES": "1048576",
        "WEBSITE_BANNED_WORDS": "ledger",
        "TROLL_USER_CHANCE": "0.1",
    }

    # Descriptions for each setting
    DESCRIPTIONS = {
        "OPENAI_API_URL": "Base URL for AI API service",
        "OPENAI_KEY": "API authentication key for AI service (environment-only)",
        "OPENAI_MODEL": "Default AI model to use for content generation",
        "SECRET_KEY": "Flask secret key for session management",
        "PRODUCTION": "Production mode - disables admin interface and ingestion endpoints (true/false)",
        "API_TOKEN": "Security token for admin access (minimum 3 characters; environment-only)",
        "AGENT_RUNTIME_ENABLED": "Whether the autonomous agent runtime is enabled (true/false); manual run-once is always allowed",
        "NIGHT_LULL_ENABLED": "Night-time lull for agent wakes (true/false); when enabled, wake delays are stretched during local night hours so agents sleep at night like humans (never zero wakes)",
        "NIGHT_LULL_START_HOUR": "Local hour (0-23, server timezone) the night lull begins; full wake rate resumes after NIGHT_LULL_END_HOUR",
        "NIGHT_LULL_END_HOUR": "Local hour (0-23, server timezone) the night lull ends",
        "NIGHT_LULL_DELAY_MULTIPLIER": "Multiplier applied to agent wake delays during the night lull (e.g. 4 = agents wake 4x less often at night)",
        "SEED_VOTE_PROBABILITY": "Base probability an item receives synthetic attention during history seeding (0-1)",
        "SEED_DECAY_DAYS": "Days over which seed vote probability decays linearly to zero from the anchor",
        "SEED_ANCHOR_AT": "ISO timestamp anchor for seed decay; written on first non-dry-run history seed",
        "WEBSITE_MAX_OUTPUT_TOKENS": "Requested max output tokens for create_website HTML generation (floor 32768; a configured value below the floor is raised to it, not honored)",
        "WEBSITE_GENERATION_TIMEOUT_SECONDS": "Read timeout in seconds for the nested create_website HTML-generation request",
        "WEBSITE_MAX_HTML_BYTES": "Byte ceiling for one stored generated-website HTML document",
        "WEBSITE_BANNED_WORDS": "Words banned from create_website proposals: if the title, body, site brief, or hostname/page hints contain one (word-prefix match, case-insensitive), the tool refuses before any HTML generation and the agent must propose a different idea",
        "TROLL_USER_CHANCE": "Probability (0-1) that a newly generated persona is a troll; applies only when troll_mode is 'chance'",
    }

    @classmethod
    def get(cls, key: str, default: str | None = None) -> str | None:
        """Get a configuration value.

        Non-secrets — priority order, served through the process-local TTL
        cache (the final resolved value per key is cached, so environment
        changes need a restart to show up):
        1. Database setting (if available)
        2. Environment variable (as fallback)
        3. Default value from DEFAULTS
        4. Provided default parameter

        Secrets — the environment is authoritative; a stale database row is
        served as a grace path with a once-per-process warning.
        """
        if is_secret_key(key):
            return cls._get_secret(key, default)

        def _resolve() -> object:
            try:
                db_value = Setting.get_value(key)
            except Exception:
                db_value = None
            if db_value is not None:
                return db_value
            env_value = os.environ.get(key)
            if env_value is not None:
                return env_value
            default_value = cls.DEFAULTS.get(key)
            if default_value is not None:
                return default_value
            return _UNSET

        value = cached(key, _resolve)
        return default if value is _UNSET else value

    @classmethod
    def _get_secret(cls, key: str, default: str | None = None) -> str | None:
        """Resolve a secret: environment first, then built-in default."""
        env_value = os.environ.get(key)
        if env_value is not None:
            return env_value
        default_value = cls.DEFAULTS.get(key)
        if default_value is not None:
            return default_value
        return default

    @classmethod
    def set(cls, key: str, value: str) -> None:
        """Set a configuration value in the database (secrets are refused)."""
        if is_secret_key(key):
            raise SecretNotPersistable(
                f"Refusing to store secret '{key}' in the database (env-only since A6). "
                "Set it via the environment or .env."
            )
        description = cls.DESCRIPTIONS.get(key)
        Setting.set_value(key, value, description)
        invalidate(key)

    @classmethod
    def set_many(cls, mapping: dict[str, str]) -> None:
        """Set multiple configuration values atomically in one transaction.

        Validates that no secret keys are included, upserts all Setting rows,
        commits the transaction, and invalidates cache entries only after
        a successful commit.
        """
        for key in mapping:
            if is_secret_key(key):
                raise SecretNotPersistable(
                    f"Refusing to store secret '{key}' in the database (env-only since A6). "
                    "Set it via the environment or .env."
                )
        from deaddit.extensions import db

        for key, value in mapping.items():
            description = cls.DESCRIPTIONS.get(key)
            setting = db.session.get(Setting, key)
            if setting:
                setting.value = str(value)
                if description:
                    setting.description = description
                setting.updated_at = datetime.utcnow()
            else:
                setting = Setting(key=key, value=str(value), description=description)
                db.session.add(setting)
        db.session.commit()
        for key in mapping:
            invalidate(key)

    @staticmethod
    def _has_value(value: str | None) -> bool:
        return value is not None and str(value).strip() != ""

    @classmethod
    def get_all_settings(cls) -> dict:
        """Get all configuration settings; every secret key is masked."""
        settings = {}

        for key in cls.DEFAULTS.keys():
            source = cls._get_source(key)
            if is_secret_key(key):
                value = (
                    "***set***"
                    if cls._has_value(cls._get_secret(key))
                    else "***not set***"
                )
            else:
                value = cls.get(key)
            settings[key] = {
                "value": value,
                "description": cls.DESCRIPTIONS.get(key, ""),
                "source": source,
            }

        return settings

    @classmethod
    def _get_source(cls, key: str) -> str:
        """Determine the source of a configuration value."""
        if is_secret_key(key):
            if os.environ.get(key) is not None:
                return "environment"
            if cls.DEFAULTS.get(key) is not None:
                return "default"
            return "none"

        try:
            db_value = Setting.get_value(key)
            if db_value is not None:
                return "database"
        except Exception:
            pass

        env_value = os.environ.get(key)
        if env_value is not None:
            return "environment"

        if key in cls.DEFAULTS:
            return "default"

        return "none"

    @classmethod
    def is_api_token_set(cls) -> bool:
        """Check if API_TOKEN is set (either in database or environment)."""
        token = cls.get("API_TOKEN")
        return token is not None and len(token.strip()) > 0

    @classmethod
    def initialize_defaults(cls) -> None:
        """Initialize database with default values if not already set.

        Secret keys are skipped entirely: they are never written as rows.
        """
        try:
            for key, default_value in cls.DEFAULTS.items():
                if is_secret_key(key):
                    continue
                # Only set if not already in database
                if Setting.get_value(key) is None:
                    description = cls.DESCRIPTIONS.get(key)
                    Setting.set_value(key, default_value, description)
        except Exception:
            # Database might not be ready yet
            pass

    @classmethod
    def get_api_key_for_endpoint(cls, endpoint_url: str) -> str | None:
        """Get API key for a specific endpoint URL, checking LLMProvider first."""
        try:
            from deaddit.models import LLMProvider

            if endpoint_url:
                norm_url = endpoint_url.rstrip("/")
                provider = LLMProvider.query.filter(
                    (LLMProvider.api_url == norm_url)
                    | (LLMProvider.api_url == endpoint_url)
                ).first()
                if provider and provider.api_key and provider.api_key.strip():
                    return provider.api_key.strip()
            else:
                default_p = LLMProvider.get_default()
                if default_p and default_p.api_key and default_p.api_key.strip():
                    return default_p.api_key.strip()
        except Exception:
            pass

        if not endpoint_url:
            return cls.get("OPENAI_KEY")

        # Create a key based on the endpoint
        key = cls._endpoint_to_key(endpoint_url)

        # Try to get endpoint-specific key first
        endpoint_key = cls.get(f"API_KEY_{key}")
        if endpoint_key:
            return endpoint_key

        # Check default provider's key if available
        try:
            from deaddit.models import LLMProvider

            default_p = LLMProvider.get_default()
            if default_p and default_p.api_key and default_p.api_key.strip():
                return default_p.api_key.strip()
        except Exception:
            pass

        # Fall back to default OPENAI_KEY
        return cls.get("OPENAI_KEY")

    @classmethod
    def set_api_key_for_endpoint(cls, endpoint_url: str, api_key: str) -> None:
        """Set API key for a specific endpoint URL.

        Raises SecretNotPersistable: endpoint keys are environment-only since A6.
        """
        if not endpoint_url:
            cls.set("OPENAI_KEY", api_key)
            return

        # Create a key based on the endpoint
        key = cls._endpoint_to_key(endpoint_url)

        # Set endpoint-specific key
        cls.set(f"API_KEY_{key}", api_key)

        # Also update the current default if this is the current endpoint
        current_endpoint = cls.get("OPENAI_API_URL")
        if current_endpoint == endpoint_url:
            cls.set("OPENAI_KEY", api_key)

    @classmethod
    def _endpoint_to_key(cls, endpoint_url: str) -> str:
        """Convert endpoint URL to a safe key name."""
        import re

        # Extract the domain from the URL
        if "openai.com" in endpoint_url:
            return "OPENAI"
        elif "groq.com" in endpoint_url:
            return "GROQ"
        elif "openrouter.ai" in endpoint_url:
            return "OPENROUTER"
        else:
            # For custom endpoints, create a safe key from the URL
            safe_key = re.sub(
                r"[^a-zA-Z0-9]",
                "_",
                endpoint_url.replace("https://", "").replace("http://", ""),
            )
            return safe_key.upper()[:50]  # Limit length
