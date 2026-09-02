"""Shared HTTP transport for OpenAI-compatible chat completions.

Holds the only `requests.post` call added by this phase, plus session pooling
and the retry policy (3 attempts, full-jitter backoff).
"""

import json
import logging
import random
import threading
import time
from collections.abc import Iterator

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


def _notify(on_attempt, attempt: int, scoped_id: str, outcome) -> None:
    if on_attempt is None:
        return
    try:
        on_attempt(attempt, scoped_id, outcome)
    except Exception:
        logger.warning("on_attempt callback failed", exc_info=True)


def get_session() -> requests.Session:
    """Return the process-wide requests session (created lazily)."""
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def _unwrap_envelope(data: dict) -> dict:
    """Unwrap proxies that nest the OpenAI response under ``data``.

    e.g. cline returns ``{"success": true, "data": {choices: [...]}}``;
    every OpenAI-shaped consumer reads ``choices`` off the top level.
    """
    inner = data.get("data")
    if not data.get("choices") and isinstance(inner, dict) and inner.get("choices"):
        return inner
    return data


def post_chat(
    api_url: str,
    payload: dict,
    api_key: str | None,
    request_id: str,
    connect_timeout: float = _CONNECT_TIMEOUT,
    read_timeout: float = _READ_TIMEOUT,
    *,
    on_attempt=None,
) -> dict:
    """POST a chat completion request with retries; return parsed JSON of a 200.

    Raises PermanentLLMError immediately on non-retryable statuses;
    TransientLLMError once the retry budget is exhausted.

    ``on_attempt(attempt, scoped_id, outcome)`` fires once per loop
    iteration: outcome is the parsed response dict on success (just
    before return) or that iteration's raised/last Exception.
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
            _notify(on_attempt, attempt, scoped_id, exc)
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
            data = _unwrap_envelope(response.json())
            _notify(on_attempt, attempt, scoped_id, data)
            _last_attempts.value = attempt
            return data

        excerpt = response.text[:200] if response.text else ""
        if response.status_code in _RETRYABLE_STATUSES:
            last_error = LLMError(f"HTTP {response.status_code}: {excerpt}")
            _notify(on_attempt, attempt, scoped_id, last_error)
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

        permanent = PermanentLLMError(
            f"HTTP {response.status_code} from {url}: {excerpt}"
        )
        _notify(on_attempt, attempt, scoped_id, permanent)
        raise permanent

    raise TransientLLMError(
        f"LLM request {request_id} failed after {_MAX_ATTEMPTS} attempts: {last_error}"
    ) from last_error


def _sse_chunks(response: requests.Response) -> Iterator[dict]:
    """Decode an OpenAI SSE body into chunk dicts.

    Yields each parsed ``data: <json>`` line; blank lines, ``:`` comments
    and keep-alives are skipped; the ``data: [DONE]`` sentinel ends the
    stream. Raises whatever iter_lines raises on a broken connection —
    the caller decides whether that is retryable (it is not).
    """
    for raw_line in response.iter_lines():
        line = (
            raw_line.decode("utf-8", errors="replace")
            if isinstance(raw_line, bytes)
            else raw_line
        )
        line = (line or "").strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            logger.warning("Skipping unparseable SSE data line: %.120s", data)


def stream_chat(
    api_url: str,
    payload: dict,
    api_key: str | None,
    request_id: str,
    connect_timeout: float = _CONNECT_TIMEOUT,
    read_timeout: float = _READ_TIMEOUT,
    *,
    on_attempt=None,
) -> Iterator[dict]:
    """POST a streaming chat completion; yield OpenAI chunk dicts as they arrive.

    Sets ``payload['stream'] = True`` itself. Retry policy is identical to
    :func:`post_chat` (3 attempts, full-jitter backoff) but retries only
    happen BEFORE the first byte: once a 200 body is being decoded, any
    failure surfaces as :class:`TransientLLMError` with no further attempts.

    ``on_attempt(attempt, scoped_id, outcome)`` fires like post_chat's:
    per-iteration exceptions during connect/retry, and exactly one success
    notification when the stream completes cleanly, with outcome shaped
    ``{'usage': <accumulated usage dict or {}>}`` so
    accounting.AttemptRecorder works unchanged. Sets last_attempts().
    """
    url = f"{api_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload["stream"] = True

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
                stream=True,
            )
        except requests.RequestException as exc:
            last_error = exc
            _notify(on_attempt, attempt, scoped_id, exc)
            logger.warning(
                "LLM stream attempt %d/%d failed (request_id=%s): %s",
                attempt,
                _MAX_ATTEMPTS,
                scoped_id,
                exc,
            )
            if attempt < _MAX_ATTEMPTS:
                time.sleep(random.uniform(0, min(2 ** (attempt - 1), 8)))
            continue

        if response.status_code != 200:
            excerpt = response.text[:200] if response.text else ""
            if response.status_code in _RETRYABLE_STATUSES:
                last_error = LLMError(f"HTTP {response.status_code}: {excerpt}")
                _notify(on_attempt, attempt, scoped_id, last_error)
                logger.warning(
                    "LLM stream attempt %d/%d failed (request_id=%s): HTTP %d %s",
                    attempt,
                    _MAX_ATTEMPTS,
                    scoped_id,
                    response.status_code,
                    excerpt,
                )
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(random.uniform(0, min(2 ** (attempt - 1), 8)))
                continue
            permanent = PermanentLLMError(
                f"HTTP {response.status_code} from {url}: {excerpt}"
            )
            _notify(on_attempt, attempt, scoped_id, permanent)
            raise permanent

        # First byte reached: the retry budget is spent, this attempt must run.
        usage: dict = {}
        try:
            for chunk in _sse_chunks(response):
                chunk_usage = chunk.get("usage")
                if isinstance(chunk_usage, dict):
                    usage.update(chunk_usage)
                yield chunk
        except requests.RequestException as exc:
            transient = TransientLLMError(
                f"SSE stream broke mid-flight (request_id={scoped_id}): {exc}"
            )
            _notify(on_attempt, attempt, scoped_id, transient)
            _last_attempts.value = attempt
            raise transient from exc
        except GeneratorExit:
            raise

        _notify(on_attempt, attempt, scoped_id, {"usage": usage})
        _last_attempts.value = attempt
        return

    raise TransientLLMError(
        f"LLM stream request {request_id} failed after {_MAX_ATTEMPTS} "
        f"attempts: {last_error}"
    ) from last_error
