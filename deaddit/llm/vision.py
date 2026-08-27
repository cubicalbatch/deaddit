"""Bounded, in-memory image-description requests (plan 5B).

Used by ``agents/tools_read.py`` to turn a stored ``PostImage`` into a short
factual description through the *reading* agent's own already-resolved
vision-capable endpoint. The normalized data URL built here lives only on
the local ``ChatRequest`` built inside :func:`describe_image` - it is never
returned to callers, attached to any persisted row, or logged. Only the
resulting description text (or a raised error) leaves this module.
"""

from __future__ import annotations

import base64
from io import BytesIO

from PIL import Image

from deaddit.llm.client import ChatRequest, LLMClient, Sampling
from deaddit.llm.errors import LLMError

__all__ = ["ImageDescriptionError", "describe_image"]

#: Longest edge, in pixels, of the JPEG actually sent to the vision model.
#: Large enough to support a factual description, small enough to keep the
#: request body bounded regardless of the stored original's dimensions.
MAX_DIMENSION = 768

#: JPEG quality steps tried in order until the encoded size fits under
#: MAX_ENCODED_BYTES. The last step is used even if it still doesn't fit -
#: at MAX_DIMENSION that is not expected to happen for real photos.
_QUALITY_STEPS = (80, 60, 40)

#: Hard cap, in bytes, on the pre-base64 JPEG payload. Base64 adds ~33%
#: overhead, so this keeps the data URL comfortably under ~2 MB.
MAX_ENCODED_BYTES = 1_500_000

DESCRIBE_INSTRUCTION = (
    "Describe this image in 1-3 concise, factual sentences for someone who "
    "cannot see it. Mention the main subject, setting, and any legible "
    "text. Do not speculate about anything outside the frame."
)

DEFAULT_MAX_TOKENS = 200
DEFAULT_READ_TIMEOUT = 30.0


class ImageDescriptionError(LLMError):
    """A nested vision request could not produce a usable description."""


def _normalize_to_jpeg_data_url(image_bytes: bytes) -> str:
    """Resize/re-encode *image_bytes* to a bounded in-memory JPEG data URL."""
    try:
        with Image.open(BytesIO(image_bytes)) as source:
            source.load()
            image = source.convert("RGB")
    except Exception as exc:
        raise ImageDescriptionError("could not decode stored image") from exc

    try:
        image.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.Resampling.LANCZOS)
        encoded = b""
        for quality in _QUALITY_STEPS:
            buf = BytesIO()
            image.save(buf, format="JPEG", quality=quality)
            encoded = buf.getvalue()
            if len(encoded) <= MAX_ENCODED_BYTES:
                break
    finally:
        image.close()

    b64 = base64.b64encode(encoded).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def describe_image(
    image_bytes: bytes,
    *,
    api_url: str,
    model: str,
    api_key: str | None = None,
    agent: str | None = None,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
) -> str:
    """Return a short factual description of *image_bytes* via *model*.

    Builds one OpenAI-compatible content-array request carrying only the
    normalized in-memory data URL (never the original bytes, and never
    stored anywhere) plus the fixed describe instruction. Usage is labeled
    ``action="image_describe"`` so its cost is distinguishable in the
    ledger. Raises :class:`ImageDescriptionError` or any other
    :class:`~deaddit.llm.errors.LLMError` on failure; callers are expected
    to fall back to the stored source prompt rather than propagate it.
    """
    data_url = _normalize_to_jpeg_data_url(image_bytes)
    request = ChatRequest(
        system_prompt="",
        user_prompt=DESCRIBE_INSTRUCTION,
        model=model,
        api_url=api_url,
        api_key=api_key,
        sampling=Sampling(max_tokens=DEFAULT_MAX_TOKENS, temperature=0.2),
        extra_payload={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": DESCRIBE_INSTRUCTION},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ]
        },
        read_timeout=read_timeout,
        action="image_describe",
        agent=agent,
    )
    result = LLMClient().complete(request)
    description = (result.content or "").strip()
    if not description:
        raise ImageDescriptionError("vision model returned an empty description")
    return description
