"""fal.ai adapter: queue-based text-to-image generation and model catalog.

Implements deaddit.images.client.ImageAdapter with direct REST calls (no fal
SDK). Authenticates with ``Authorization: Key <credential>``. Catalog search
fetches expanded OpenAPI data from fal transiently, to decide whether an
endpoint accepts a ``prompt`` input and returns an ``images`` output
collection, but never stores that OpenAPI document in a cached ModelOption -
only compact metadata is kept.

Generation submits to the queue, polls status under the caller's deadline,
and normalizes the first entry of the result's ``images`` array. A timeout
best-effort cancels the queued request before raising ImageTimeoutError.
Validation and content-policy failures are permanent; only transport-level
failures and retryable HTTP statuses (429/500/502/503/504) are retried, since
fal's own queue infrastructure already absorbs other transient failures.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

import requests

from deaddit.images.types import (
    Deadline,
    ImageAuthError,
    ImageContentPolicyError,
    ImageGenerationResult,
    ImageProviderTransientError,
    ImageTimeoutError,
    ImageValidationError,
    MalformedImageResultError,
    ModelOption,
    ModelSearchResult,
    ModelValidation,
)

if TYPE_CHECKING:
    from deaddit.models import ImageProvider

PROVIDER_TYPE = "fal"

_MODELS_URL = "https://api.fal.ai/v1/models"
_QUEUE_BASE = "https://queue.fal.run"

_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 30.0
_POLL_INTERVAL = 1.5
_MAX_TRANSPORT_ATTEMPTS = 3
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_COMPACT_METADATA_KEYS = ("description", "tags", "thumbnail_url", "updated_at")


def _default_transport(method: str, url: str, **kwargs: Any) -> Any:
    return requests.request(method, url, **kwargs)


def _iter_schemas(openapi: dict[str, Any]) -> Iterator[dict[str, Any]]:
    components = openapi.get("components")
    schemas = components.get("schemas") if isinstance(components, dict) else None
    if isinstance(schemas, dict):
        yield from (schema for schema in schemas.values() if isinstance(schema, dict))


def _openapi_accepts_prompt(openapi: dict[str, Any]) -> bool:
    for schema in _iter_schemas(openapi):
        properties = schema.get("properties")
        if isinstance(properties, dict) and "prompt" in properties:
            return True
    return False


def _openapi_returns_images(openapi: dict[str, Any]) -> bool:
    for schema in _iter_schemas(openapi):
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            continue
        images_schema = properties.get("images")
        if isinstance(images_schema, dict) and images_schema.get("type") == "array":
            return True
    return False


class FalAdapter:
    """ImageAdapter implementation backed by fal.ai's REST/queue API."""

    def __init__(
        self,
        *,
        transport: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] | None = None,
        poll_interval: float = _POLL_INTERVAL,
    ) -> None:
        self._transport = transport or _default_transport
        self._sleep = sleep or time.sleep
        self._poll_interval = poll_interval

    # -- catalog ----------------------------------------------------------

    def search_models(
        self,
        provider: ImageProvider,
        credential: str,
        query: str,
        cursor: str | None,
    ) -> ModelSearchResult:
        params: dict[str, str] = {
            "category": "text-to-image",
            "status": "active",
            "expand": "openapi-3.0",
        }
        if query:
            params["q"] = query
        if cursor:
            params["cursor"] = cursor

        response = self._call(
            "GET", _MODELS_URL, headers=self._headers(credential), params=params
        )
        if response.status_code != 200:
            self._raise_for_status_error(response)
        payload = self._json(response)
        entries = payload.get("models")
        if not isinstance(entries, list):
            raise MalformedImageResultError(
                "fal model catalog response is missing a models list"
            )

        options = [
            option
            for option in (self._compatible_option(entry) for entry in entries)
            if option is not None
        ]
        next_cursor = payload.get("next_cursor")
        return ModelSearchResult(
            options=options,
            next_cursor=next_cursor if isinstance(next_cursor, str) else None,
        )

    def validate_model(
        self,
        provider: ImageProvider,
        credential: str,
        model_id: str,
    ) -> ModelValidation:
        params = {"endpoint_id": model_id, "expand": "openapi-3.0"}
        response = self._call(
            "GET", _MODELS_URL, headers=self._headers(credential), params=params
        )
        if response.status_code != 200:
            self._raise_for_status_error(response)
        payload = self._json(response)
        entries = payload.get("models")
        if not isinstance(entries, list) or not entries:
            return ModelValidation(
                compatible=False, reason="model not found in fal catalog"
            )

        entry = entries[0]
        if not isinstance(entry, dict):
            return ModelValidation(
                compatible=False, reason="malformed fal catalog entry"
            )
        metadata = entry.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        status = metadata.get("status")
        if status not in (None, "active"):
            return ModelValidation(
                compatible=False, reason=f"model status is {status!r}"
            )

        openapi = entry.get("openapi")
        if not isinstance(openapi, dict):
            return ModelValidation(
                compatible=False, reason="model schema is unavailable"
            )
        if not _openapi_accepts_prompt(openapi):
            return ModelValidation(
                compatible=False, reason="model does not accept a prompt input"
            )
        if not _openapi_returns_images(openapi):
            return ModelValidation(
                compatible=False,
                reason="model does not return an images output collection",
            )
        return ModelValidation(compatible=True)

    def _compatible_option(self, entry: Any) -> ModelOption | None:
        if not isinstance(entry, dict):
            return None
        endpoint_id = entry.get("endpoint_id")
        metadata = entry.get("metadata")
        openapi = entry.get("openapi")
        if (
            not endpoint_id
            or not isinstance(metadata, dict)
            or not isinstance(openapi, dict)
        ):
            return None
        if metadata.get("status") != "active":
            return None
        if metadata.get("category") != "text-to-image":
            return None
        if not _openapi_accepts_prompt(openapi) or not _openapi_returns_images(openapi):
            return None

        compact_metadata = {
            key: metadata[key] for key in _COMPACT_METADATA_KEYS if key in metadata
        }
        return ModelOption(
            model_id=str(endpoint_id),
            display_name=str(metadata.get("display_name") or endpoint_id),
            category=metadata.get("category"),
            metadata=compact_metadata,
        )

    # -- generation ---------------------------------------------------------

    def generate(
        self,
        provider: ImageProvider,
        credential: str,
        model_id: str,
        prompt: str,
        deadline: Deadline,
    ) -> ImageGenerationResult:
        headers = self._headers(credential)
        submit_url = f"{_QUEUE_BASE}/{model_id}"
        response = self._call(
            "POST",
            submit_url,
            headers=headers,
            json_body={"prompt": prompt},
            deadline=deadline,
        )
        if response.status_code not in (200, 202):
            self._raise_for_status_error(response)
        submitted = self._json(response)
        request_id = submitted.get("request_id")
        if not request_id:
            raise MalformedImageResultError(
                "fal queue submission response is missing request_id"
            )
        request_id = str(request_id)
        status_url = submitted.get(
            "status_url", f"{_QUEUE_BASE}/{model_id}/requests/{request_id}/status"
        )
        cancel_url = submitted.get(
            "cancel_url", f"{_QUEUE_BASE}/{model_id}/requests/{request_id}/cancel"
        )
        response_url = submitted.get(
            "response_url", f"{_QUEUE_BASE}/{model_id}/requests/{request_id}"
        )

        try:
            self._await_completion(status_url, headers, deadline)
        except ImageTimeoutError:
            self._cancel_best_effort(cancel_url, headers)
            raise

        result_response = self._call(
            "GET", response_url, headers=headers, deadline=deadline
        )
        if result_response.status_code != 200:
            self._raise_for_status_error(result_response)
        result = self._json(result_response)
        return self._normalize_result(request_id, result)

    def _await_completion(
        self, status_url: str, headers: dict[str, str], deadline: Deadline
    ) -> None:
        while True:
            if deadline.expired():
                raise ImageTimeoutError(
                    "fal generation did not complete before the deadline"
                )
            response = self._call(
                "GET",
                status_url,
                headers=headers,
                params={"logs": "0"},
                deadline=deadline,
            )
            if response.status_code != 200:
                self._raise_for_status_error(response)
            status_body = self._json(response)
            status = str(status_body.get("status", "")).upper()
            if status == "COMPLETED":
                return
            if status in {"IN_QUEUE", "IN_PROGRESS"}:
                sleep_for = min(self._poll_interval, deadline.remaining())
                if sleep_for > 0:
                    self._sleep(sleep_for)
                continue
            raise MalformedImageResultError(
                f"unrecognized fal queue status: {status!r}"
            )

    def _cancel_best_effort(self, cancel_url: str, headers: dict[str, str]) -> None:
        try:
            self._transport(
                "PUT",
                cancel_url,
                headers=headers,
                timeout=(_CONNECT_TIMEOUT, _CONNECT_TIMEOUT),
            )
        except Exception:
            pass

    def _normalize_result(
        self, request_id: str, result: dict[str, Any]
    ) -> ImageGenerationResult:
        images = result.get("images")
        if not isinstance(images, list) or not images:
            raise MalformedImageResultError("fal result contained no images")
        first = images[0]
        if not isinstance(first, dict):
            raise MalformedImageResultError("fal image entry was not an object")
        image_url = first.get("url")
        if not image_url or not isinstance(image_url, str):
            raise MalformedImageResultError("fal image entry is missing a url")

        safety_verdict = "unknown"
        has_nsfw = result.get("has_nsfw_concepts")
        if isinstance(has_nsfw, list) and has_nsfw:
            flagged = bool(has_nsfw[0])
            safety_verdict = "flagged" if flagged else "passed"
            if flagged:
                raise ImageContentPolicyError(
                    "fal flagged the generated image as unsafe"
                )

        width = first.get("width")
        height = first.get("height")
        seed = result.get("seed")
        return ImageGenerationResult(
            request_id=request_id,
            image_url=image_url,
            image_bytes=None,
            mime_type=first.get("content_type"),
            width=width if isinstance(width, int) else None,
            height=height if isinstance(height, int) else None,
            seed=seed if isinstance(seed, int) else None,
            cost=None,
            safety_verdict=safety_verdict,
        )

    # -- transport ------------------------------------------------------------

    def _headers(self, credential: str) -> dict[str, str]:
        return {
            "Authorization": f"Key {credential}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _call(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        deadline: Deadline | None = None,
    ) -> Any:
        """Issue one request, retrying transport failures and 5xx/429/etc.

        Validation, auth, and content-policy responses are returned as-is
        for the caller to interpret via ``_raise_for_status_error`` - only
        transport exceptions and retryable statuses are retried here, since
        fal's own queue already absorbs most other transient failures.
        """
        last_error: Exception | None = None
        for attempt in range(1, _MAX_TRANSPORT_ATTEMPTS + 1):
            read_timeout = _READ_TIMEOUT
            if deadline is not None:
                remaining = deadline.remaining()
                if remaining <= 0:
                    raise ImageTimeoutError("fal request deadline elapsed")
                read_timeout = min(_READ_TIMEOUT, remaining)

            try:
                response = self._transport(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=(_CONNECT_TIMEOUT, read_timeout),
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= _MAX_TRANSPORT_ATTEMPTS or (
                    deadline is not None and deadline.expired()
                ):
                    raise ImageProviderTransientError(
                        f"fal request to {url} failed: {exc}"
                    ) from exc
                self._sleep(min(2 ** (attempt - 1), 4))
                continue

            if (
                response.status_code in _RETRYABLE_STATUSES
                and attempt < _MAX_TRANSPORT_ATTEMPTS
                and not (deadline is not None and deadline.expired())
            ):
                last_error = ImageProviderTransientError(
                    f"fal returned a retryable status: HTTP {response.status_code}"
                )
                self._sleep(min(2 ** (attempt - 1), 4))
                continue
            return response

        raise ImageProviderTransientError(
            f"fal request to {url} failed after {_MAX_TRANSPORT_ATTEMPTS} attempts: "
            f"{last_error}"
        )

    def _json(self, response: Any) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise MalformedImageResultError("fal response was not valid JSON") from exc
        if not isinstance(data, dict):
            raise MalformedImageResultError("fal response was not a JSON object")
        return data

    def _first_detail(self, response: Any) -> dict[str, Any] | None:
        try:
            body = response.json()
        except ValueError:
            return None
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, list) and detail and isinstance(detail[0], dict):
            return detail[0]
        return None

    def _raise_for_status_error(self, response: Any) -> None:
        status = response.status_code
        excerpt = (response.text or "")[:200] if hasattr(response, "text") else ""

        if status in (401, 403):
            raise ImageAuthError(
                f"fal rejected the configured credential (HTTP {status})"
            )

        if status == 422:
            detail = self._first_detail(response)
            error_type = str(detail.get("type", "")) if detail else ""
            message = (detail.get("msg") if detail else None) or excerpt
            if "content_policy" in error_type or "safety" in error_type:
                raise ImageContentPolicyError(
                    message or "fal rejected the request on safety grounds"
                )
            raise ImageValidationError(
                message or f"fal rejected the request: HTTP 422: {excerpt}"
            )

        if status in _RETRYABLE_STATUSES:
            raise ImageProviderTransientError(
                f"fal returned a retryable error: HTTP {status}: {excerpt}"
            )

        raise ImageValidationError(f"fal request failed: HTTP {status}: {excerpt}")


__all__ = ["PROVIDER_TYPE", "FalAdapter"]
