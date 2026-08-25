"""Typed errors for the LLM client."""


class LLMError(Exception):
    """Base class for LLM client failures."""


class TransientLLMError(LLMError):
    """Retryable failure; raised after the retry budget is exhausted."""


class PermanentLLMError(LLMError):
    """Non-retryable failure (HTTP 400/401/403/422 or unusable response shape)."""


class SchemaValidationError(LLMError):
    """Tool arguments failed schema validation against their pydantic model."""


class CapabilityError(PermanentLLMError):
    """The endpoint/model explicitly cannot serve this request (e.g. no tools)."""

    def __init__(
        self,
        message: str,
        *,
        api_url: str | None = None,
        model: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.api_url = api_url
        self.model = model
        self.request_id = request_id
