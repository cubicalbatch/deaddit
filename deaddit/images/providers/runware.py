"""Runware adapter: Bearer-authenticated text-to-image generation and model search.

Implements deaddit.images.client.ImageAdapter with direct REST calls (no
Runware SDK) against ``https://api.runware.ai/v1``. Every request body is a
JSON array holding one task object; every response is a JSON object with a
top-level ``data`` array (acknowledged/in-flight/completed tasks) and an
``errors`` array (per-task failures, which the API can return alongside an
overall HTTP 200 for batch requests).

Generation submits one ``imageInference`` task with ``deliveryMethod``
``"async"`` and polls ``getResponse`` under the caller's deadline, minting
exactly one UUIDv4 ``taskUUID`` per call and reusing it for the submission
and every poll (and for any transport-level retry of either), so a resent
request can never be mistaken by the server for a second billable task. A
local timeout stops polling, not the server-side job: the raised
ImageTimeoutError says a billed task may still complete without echoing any
raw provider response back to the caller.

Model search uses the official ``modelSearch`` task and keeps only
"checkpoint" entries with a well-formed AIR identifier
(``provider:model@version``) as model IDs; validate_model reuses
``modelSearch``, requiring an exact AIR match in the "checkpoint" category.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import requests

from deaddit.images.types import (
    Deadline,
    ImageAuthError,
    ImageContentPolicyError,
    ImageGenerationResult,
    ImageProviderError,
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

PROVIDER_TYPE = "runware"

_BASE_URL = "https://api.runware.ai/v1"

_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 30.0
_POLL_INTERVAL = 1.5
_MAX_TRANSPORT_ATTEMPTS = 3
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

# Common SDXL/Flux-class default; Runware requires width/height but the
# fetched docs do not spell out a default, so this normalized request always
# asks for a square, widely-supported size.
_DEFAULT_WIDTH = 1024
_DEFAULT_HEIGHT = 1024
_OUTPUT_FORMAT = "PNG"
_MIME_BY_OUTPUT_FORMAT = {
    "PNG": "image/png",
    "JPG": "image/jpeg",
    "WEBP": "image/webp",
}

_MODEL_SEARCH_LIMIT = 20
_COMPACT_METADATA_KEYS = (
    "architecture",
    "capabilities",
    "source",
    "provider",
    "shortDescription",
)
_AIR_PATTERN = re.compile(r"^[^:@\s]+:[^:@\s]+@[^:@\s]+$")

# Named explicitly in Runware's error documentation as provider/capacity
# issues rather than request-shape problems.
_TRANSIENT_TASK_ERROR_CODES = {"timeoutProvider", "providerRateLimitExceeded"}
_CONTENT_POLICY_TERMS = ("nsfw", "moderation", "content polic", "safety")
_TRANSIENT_TERMS = ("unavailable", "capacity", "provider timeout")


def _default_transport(method: str, url: str, **kwargs: Any) -> Any:
    return requests.request(method, url, **kwargs)


class RunwareAdapter:
    """ImageAdapter implementation backed by Runware's REST API."""

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

    # -- catalog --------------------------------------------------------

    def search_models(
        self,
        provider: ImageProvider,
        credential: str,
        query: str,
        cursor: str | None,
    ) -> ModelSearchResult:
        offset = self._parse_offset(cursor)
        task_uuid = str(uuid.uuid4())
        body = [
            {
                "taskType": "modelSearch",
                "taskUUID": task_uuid,
                "search": query or "",
                "category": "checkpoint",
                "limit": _MODEL_SEARCH_LIMIT,
                "offset": offset,
            }
        ]
        payload = self._submit(credential, body, task_uuid)
        result_entry = self._require_entry(payload, task_uuid, "modelSearch")
        results = result_entry.get("results")
        if not isinstance(results, list):
            raise MalformedImageResultError(
                "runware modelSearch response is missing a results list"
            )

        options = [
            option
            for option in (self._compatible_option(entry) for entry in results)
            if option is not None
        ]
        total = result_entry.get("totalResults")
        next_cursor = None
        if isinstance(total, int) and offset + _MODEL_SEARCH_LIMIT < total:
            next_cursor = str(offset + _MODEL_SEARCH_LIMIT)
        return ModelSearchResult(options=options, next_cursor=next_cursor)

    def validate_model(
        self,
        provider: ImageProvider,
        credential: str,
        model_id: str,
    ) -> ModelValidation:
        if not isinstance(model_id, str) or not _AIR_PATTERN.match(model_id):
            return ModelValidation(
                compatible=False,
                reason="not a well-formed AIR identifier (provider:model@version)",
            )

        task_uuid = str(uuid.uuid4())
        body = [
            {
                "taskType": "modelSearch",
                "taskUUID": task_uuid,
                "search": model_id,
                "category": "checkpoint",
                "limit": _MODEL_SEARCH_LIMIT,
                "offset": 0,
            }
        ]
        payload = self._submit(credential, body, task_uuid)
        result_entry = self._require_entry(payload, task_uuid, "modelSearch")
        results = result_entry.get("results")
        if not isinstance(results, list):
            return ModelValidation(
                compatible=False, reason="model search response was malformed"
            )

        for entry in results:
            if not isinstance(entry, dict) or entry.get("air") != model_id:
                continue
            if entry.get("category") != "checkpoint":
                return ModelValidation(
                    compatible=False,
                    reason=f"model category is {entry.get('category')!r}, "
                    "not checkpoint",
                )
            return ModelValidation(compatible=True)
        return ModelValidation(
            compatible=False, reason="model not found in runware catalog"
        )

    def _parse_offset(self, cursor: str | None) -> int:
        if not cursor:
            return 0
        try:
            return max(0, int(cursor))
        except ValueError:
            return 0

    def _compatible_option(self, entry: Any) -> ModelOption | None:
        if not isinstance(entry, dict):
            return None
        air = entry.get("air")
        if not isinstance(air, str) or not _AIR_PATTERN.match(air):
            return None
        if entry.get("category") != "checkpoint":
            return None

        compact_metadata = {
            key: entry[key] for key in _COMPACT_METADATA_KEYS if key in entry
        }
        return ModelOption(
            model_id=air,
            display_name=str(entry.get("name") or air),
            category=entry.get("category"),
            metadata=compact_metadata,
        )

    def _submit(
        self, credential: str, body: list[dict[str, Any]], task_uuid: str
    ) -> dict[str, Any]:
        response = self._call(
            "POST", _BASE_URL, headers=self._headers(credential), json_body=body
        )
        if response.status_code != 200:
            self._raise_for_status_error(response)
        payload = self._json(response)
        error_entry = self._find_entry(payload.get("errors"), task_uuid)
        if error_entry is not None:
            raise self._classify_task_error(error_entry)
        return payload

    def _require_entry(
        self, payload: dict[str, Any], task_uuid: str, task_type: str
    ) -> dict[str, Any]:
        entry = self._find_entry(payload.get("data"), task_uuid)
        if entry is None:
            raise MalformedImageResultError(
                f"runware {task_type} response is missing task data"
            )
        return entry

    # -- generation -------------------------------------------------------

    def generate(
        self,
        provider: ImageProvider,
        credential: str,
        model_id: str,
        prompt: str,
        deadline: Deadline,
    ) -> ImageGenerationResult:
        task_uuid = str(uuid.uuid4())
        headers = self._headers(credential)
        submit_body = [
            {
                "taskType": "imageInference",
                "taskUUID": task_uuid,
                "model": model_id,
                "positivePrompt": prompt,
                "width": _DEFAULT_WIDTH,
                "height": _DEFAULT_HEIGHT,
                "numberResults": 1,
                "outputType": "URL",
                "outputFormat": _OUTPUT_FORMAT,
                "includeCost": True,
                "checkNSFWContent": True,
                "deliveryMethod": "async",
            }
        ]
        response = self._call(
            "POST",
            _BASE_URL,
            headers=headers,
            json_body=submit_body,
            deadline=deadline,
        )
        if response.status_code != 200:
            self._raise_for_status_error(response)
        payload = self._json(response)

        entry = self._await_completion(task_uuid, headers, deadline, payload)
        return self._normalize_result(task_uuid, entry)

    def _await_completion(
        self,
        task_uuid: str,
        headers: dict[str, str],
        deadline: Deadline,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        while True:
            error_entry = self._find_entry(payload.get("errors"), task_uuid)
            if error_entry is not None:
                raise self._classify_task_error(error_entry)

            data_entry = self._find_entry(payload.get("data"), task_uuid)
            if data_entry is not None:
                if data_entry.get("imageURL"):
                    return data_entry
                status = str(data_entry.get("status") or "").lower()
                if status not in ("", "processing", "pending", "queued"):
                    raise MalformedImageResultError(
                        f"unrecognized runware task status: {status!r}"
                    )

            if deadline.expired():
                raise ImageTimeoutError(
                    "runware generation did not complete before the deadline; "
                    "the task may still be processing and billed on the server"
                )

            sleep_for = min(self._poll_interval, deadline.remaining())
            if sleep_for > 0:
                self._sleep(sleep_for)

            response = self._call(
                "POST",
                _BASE_URL,
                headers=headers,
                json_body=[{"taskType": "getResponse", "taskUUID": task_uuid}],
                deadline=deadline,
            )
            if response.status_code != 200:
                self._raise_for_status_error(response)
            payload = self._json(response)

    def _normalize_result(
        self, task_uuid: str, entry: dict[str, Any]
    ) -> ImageGenerationResult:
        image_url = entry.get("imageURL")
        if not image_url or not isinstance(image_url, str):
            raise MalformedImageResultError("runware result is missing imageURL")

        image_uuid = entry.get("imageUUID")
        request_id = str(image_uuid) if image_uuid else task_uuid

        safety_verdict = "unknown"
        nsfw = entry.get("NSFWContent")
        if isinstance(nsfw, bool):
            safety_verdict = "flagged" if nsfw else "passed"
            if nsfw:
                raise ImageContentPolicyError(
                    "runware flagged the generated image as unsafe"
                )

        width = entry.get("width")
        height = entry.get("height")
        seed = entry.get("seed")
        cost = entry.get("cost")
        return ImageGenerationResult(
            request_id=request_id,
            image_url=image_url,
            image_bytes=None,
            mime_type=_MIME_BY_OUTPUT_FORMAT.get(_OUTPUT_FORMAT),
            width=width if isinstance(width, int) else _DEFAULT_WIDTH,
            height=height if isinstance(height, int) else _DEFAULT_HEIGHT,
            seed=seed if isinstance(seed, int) else None,
            cost=cost if isinstance(cost, int | float) else None,
            safety_verdict=safety_verdict,
        )

    # -- error classification ----------------------------------------------

    def _classify_task_error(self, entry: dict[str, Any]) -> ImageProviderError:
        code = str(entry.get("code") or "")
        message = str(entry.get("message") or "") or (
            f"runware task failed (code={code or 'unknown'})"
        )
        lowered = f"{code} {message}".lower()
        if any(term in lowered for term in _CONTENT_POLICY_TERMS):
            return ImageContentPolicyError(message)
        if code in _TRANSIENT_TASK_ERROR_CODES or any(
            term in lowered for term in _TRANSIENT_TERMS
        ):
            return ImageProviderTransientError(message)
        return ImageValidationError(message)

    def _find_entry(self, entries: Any, task_uuid: str) -> dict[str, Any] | None:
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if isinstance(entry, dict) and entry.get("taskUUID") == task_uuid:
                return entry
        return None

    # -- transport ----------------------------------------------------------

    def _headers(self, credential: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _call(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: list[dict[str, Any]],
        deadline: Deadline | None = None,
    ) -> Any:
        """Issue one request, retrying transport failures and 5xx/429.

        Every retry resends the identical *json_body* - and therefore the
        identical ``taskUUID`` - so a resend can never be mistaken by the
        server for a second billable task.
        """
        last_error: Exception | None = None
        for attempt in range(1, _MAX_TRANSPORT_ATTEMPTS + 1):
            read_timeout = _READ_TIMEOUT
            if deadline is not None:
                remaining = deadline.remaining()
                if remaining <= 0:
                    raise ImageTimeoutError("runware request deadline elapsed")
                read_timeout = min(_READ_TIMEOUT, remaining)

            try:
                response = self._transport(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    timeout=(_CONNECT_TIMEOUT, read_timeout),
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= _MAX_TRANSPORT_ATTEMPTS or (
                    deadline is not None and deadline.expired()
                ):
                    raise ImageProviderTransientError(
                        f"runware request to {url} failed: {exc}"
                    ) from exc
                self._sleep(min(2 ** (attempt - 1), 4))
                continue

            if (
                response.status_code in _RETRYABLE_STATUSES
                and attempt < _MAX_TRANSPORT_ATTEMPTS
                and not (deadline is not None and deadline.expired())
            ):
                last_error = ImageProviderTransientError(
                    f"runware returned a retryable status: HTTP {response.status_code}"
                )
                self._sleep(min(2 ** (attempt - 1), 4))
                continue
            return response

        raise ImageProviderTransientError(
            f"runware request to {url} failed after {_MAX_TRANSPORT_ATTEMPTS} "
            f"attempts: {last_error}"
        )

    def _json(self, response: Any) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise MalformedImageResultError(
                "runware response was not valid JSON"
            ) from exc
        if not isinstance(data, dict):
            raise MalformedImageResultError("runware response was not a JSON object")
        return data

    def _first_error(self, response: Any) -> dict[str, Any] | None:
        try:
            body = response.json()
        except ValueError:
            return None
        errors = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return errors[0]
        return None

    def _raise_for_status_error(self, response: Any) -> None:
        status = response.status_code
        excerpt = (response.text or "")[:200] if hasattr(response, "text") else ""
        detail = self._first_error(response)

        if status == 400:
            if detail is not None:
                raise self._classify_task_error(detail)
            raise ImageValidationError(
                f"runware rejected the request: HTTP 400: {excerpt}"
            )

        if status in (401, 402, 403):
            message = (detail.get("message") if detail else None) or excerpt
            raise ImageAuthError(
                f"runware rejected the configured credential or account "
                f"(HTTP {status}): {message}"
            )

        if status in _RETRYABLE_STATUSES:
            message = (detail.get("message") if detail else None) or excerpt
            raise ImageProviderTransientError(
                f"runware returned a retryable error: HTTP {status}: {message}"
            )

        raise ImageValidationError(f"runware request failed: HTTP {status}: {excerpt}")


__all__ = ["PROVIDER_TYPE", "RunwareAdapter"]
