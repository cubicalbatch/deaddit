"""Configuration management for Deaddit.

This module handles loading configuration from the database with fallback
to environment variables. Only API_TOKEN remains as an environment variable.
"""

import os

from deaddit.models import Setting


class Config:
    """Configuration manager that loads from database with environment fallbacks."""

    # Default values for configuration
    DEFAULTS = {
        "OPENAI_API_URL": "http://localhost/v1",
        "OPENAI_KEY": "your_openrouter_api_key",
        "OPENAI_MODEL": "llama3",
        "MODELS": "llama3,gpt-3.5-turbo,gpt-4,claude-3-haiku,mistral-7b",
        "API_BASE_URL": "http://localhost:5000",
        "SECRET_KEY": "dev-secret-key-change-in-production",
        "FLASK_ENV": "development",
        "FLASK_DEBUG": "True",
        "DEFAULT_DATA_LOADED": "false",
        "API_TOKEN": None,
        "PRODUCTION": "false",
        "AGENT_RUNTIME_ENABLED": "false",
    }

    # Descriptions for each setting
    DESCRIPTIONS = {
        "OPENAI_API_URL": "Base URL for AI API service",
        "OPENAI_KEY": "API authentication key for AI service",
        "OPENAI_MODEL": "Default AI model to use for content generation",
        "MODELS": "Comma-separated list of available AI models",
        "API_BASE_URL": "Base URL for the application API",
        "SECRET_KEY": "Flask secret key for session management",
        "FLASK_ENV": "Flask environment (development/production)",
        "FLASK_DEBUG": "Enable Flask debug mode (True/False)",
        "DEFAULT_DATA_LOADED": "Whether default subdeaddits and users have been loaded",
        "PRODUCTION": "Production mode - disables admin interface and ingestion endpoints (true/false)",
        "API_TOKEN": "Security token for admin access (minimum 3 characters)",
        "AGENT_RUNTIME_ENABLED": "Whether the autonomous agent runtime is enabled (true/false); manual run-once is always allowed",
    }

    @classmethod
    def get(cls, key: str, default: str | None = None) -> str | None:
        """Get a configuration value.

        Priority order:
        1. Database setting (if available)
        2. Environment variable (as fallback)
        3. Default value from DEFAULTS
        4. Provided default parameter
        """
        # Special case: API_TOKEN checks database first, then environment
        if key == "API_TOKEN":
            try:
                # Try to get from database first
                db_value = Setting.get_value(key)
                if db_value is not None:
                    return db_value
            except Exception:
                # Database might not be initialized yet, fall back to env
                pass
            # Fall back to environment variable
            return os.environ.get(key)

        try:
            # Try to get from database first
            db_value = Setting.get_value(key)
            if db_value is not None:
                return db_value
        except Exception:
            # Database might not be initialized yet, fall back to env
            pass

        # Fall back to environment variable
        env_value = os.environ.get(key)
        if env_value is not None:
            return env_value

        # Fall back to default value
        default_value = cls.DEFAULTS.get(key)
        if default_value is not None:
            return default_value

        return default

    @classmethod
    def set(cls, key: str, value: str) -> None:
        """Set a configuration value in the database."""
        description = cls.DESCRIPTIONS.get(key)
        Setting.set_value(key, value, description)

    @classmethod
    def get_all_settings(cls) -> dict:
        """Get all configuration settings with their current values."""
        settings = {}

        # Get all defined keys
        for key in cls.DEFAULTS.keys():
            settings[key] = {
                "value": cls.get(key),
                "description": cls.DESCRIPTIONS.get(key, ""),
                "source": cls._get_source(key),
            }

        # Handle API_TOKEN specially - don't expose the actual value
        api_token = cls.get("API_TOKEN")
        settings["API_TOKEN"] = {
            "value": "***set***" if api_token else "***not set***",
            "description": cls.DESCRIPTIONS.get("API_TOKEN", ""),
            "source": cls._get_source("API_TOKEN"),
        }

        return settings

    @classmethod
    def _get_source(cls, key: str) -> str:
        """Determine the source of a configuration value."""
        if key == "API_TOKEN":
            try:
                # Check database first
                db_value = Setting.get_value(key)
                if db_value is not None:
                    return "database"
            except Exception:
                pass
            # Then check environment
            env_value = os.environ.get(key)
            if env_value is not None:
                return "environment"
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
        """Initialize database with default values if not already set."""
        try:
            for key, default_value in cls.DEFAULTS.items():
                # Only set if not already in database
                if Setting.get_value(key) is None:
                    description = cls.DESCRIPTIONS.get(key)
                    Setting.set_value(key, default_value, description)
        except Exception:
            # Database might not be ready yet
            pass

    @classmethod
    def is_configured(cls) -> bool:
        """Check if the application has been configured (has settings in database)."""
        try:
            # Check if we have any settings in the database
            setting_count = Setting.query.count()
            return setting_count > 0
        except Exception:
            # Database might not be ready yet
            return False

    @classmethod
    def get_api_key_for_endpoint(cls, endpoint_url: str) -> str | None:
        """Get API key for a specific endpoint URL."""
        if not endpoint_url:
            return cls.get("OPENAI_KEY")

        # Create a key based on the endpoint
        key = cls._endpoint_to_key(endpoint_url)

        # Try to get endpoint-specific key first
        endpoint_key = cls.get(f"API_KEY_{key}")
        if endpoint_key:
            return endpoint_key

        # Fall back to default OPENAI_KEY
        return cls.get("OPENAI_KEY")

    @classmethod
    def set_api_key_for_endpoint(cls, endpoint_url: str, api_key: str) -> None:
        """Set API key for a specific endpoint URL."""
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

    @classmethod
    def get_all_endpoint_keys(cls) -> dict:
        """Get all endpoint-specific API keys."""
        endpoint_keys = {}

        # Common endpoints
        endpoints = {
            "https://api.openai.com/v1": "OpenAI",
            "https://api.groq.com/openai/v1": "Groq",
            "https://openrouter.ai/api/v1": "OpenRouter",
        }

        for endpoint_url, name in endpoints.items():
            key = cls.get_api_key_for_endpoint(endpoint_url)
            if key:
                endpoint_keys[endpoint_url] = {
                    "name": name,
                    "key": key,
                    "masked": "••••••••••••••••" if key else None,
                }

        return endpoint_keys
