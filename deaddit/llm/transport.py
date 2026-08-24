"""Shared HTTP transport for OpenAI-compatible chat completions.

Holds the only `requests.post` call added by this phase, plus session pooling
and the retry policy (3 attempts, full-jitter backoff).
"""

from __future__ import annotations

import logging
import random
import threading
import time

import requests

from deaddit.llm.errors import LLMError, PermanentLLMError, TransientLLMError

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 120
_MAX_ATTEMPTS = 3

# HTTP statuses worth retrying: timeout-ish, throttled, server-side trouble.
_RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}

_session: requests.Session | None = None
_last_attempts = threading.local()


def last_attempts() -> int:
    """Attempts made by the most recent post_chat call on this thread."""
    return getattr(_last_attempts, "value", 0)
def get_session() -> requests.Session:
    """Return the process-wide requests session (created lazily)."""
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def post_chat(
    api_url: str,
    payload: dict,
    api_key: str | None,
    request_id: str,
    connect_timeout: float = _CONNECT_TIMEOUT,
    read_timeout: float = _READ_TIMEOUT,
) -> dict:
    """POST a chat completion request with retries; return parsed JSON of a 200.

    Raises PermanentLLMError immediately on non-retryable statuses;
    TransientLLMError once the retry budget is exhausted.
    """
    url = f"{api_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    last_error: Exception | None = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        scoped_id = f"{request_id}-{attempt}"
        attempt_headers = {**headers, "X-Request-Id": scoped_id}
        try:
            response = get_session().post(
                url,
                json=payload,
                headers=attempt_headers,
                timeout=(connect_timeout, read_timeout),
            )
        except requests.RequestException as exc:
            last_error = exc
            logger.warning(
                "LLM attempt %d/%d failed (request_id=%s): %s",
                attempt,
                _MAX_ATTEMPTS,
                scoped_id,
                exc,
            )
            if attempt < _MAX_ATTEMPTS:
                time.sleep(random.uniform(0, min(2 ** (attempt - 1), 8)))
            continue

        if response.status_code == 200:
            _last_attempts.value = attempt
            return response.json()

        excerpt = response.text[:200] if response.text else ""
        if response.status_code in _RETRYABLE_STATUSES:
            last_error = LLMError(f"HTTP {response.status_code}: {excerpt}")
            logger.warning(
                "LLM attempt %d/%d failed (request_id=%s): HTTP %d %s",
                attempt,
                _MAX_ATTEMPTS,
                scoped_id,
                response.status_code,
                excerpt,
            )
            if attempt < _MAX_ATTEMPTS:
                time.sleep(random.uniform(0, min(2 ** (attempt - 1), 8)))
            continue


        raise PermanentLLMError(
            f"HTTP {response.status_code} from {url}: {excerpt}"
        )

    raise TransientLLMError(
        f"LLM request {request_id} failed after {_MAX_ATTEMPTS} attempts: {last_error}"
    ) from last_error
