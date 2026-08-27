"""Deterministic fakes for LLM transport and image-provider tests.

FakeProvider is signature-compatible with deaddit.llm.transport.post_chat;
register it via deaddit.llm.provider.set_provider() (see tests/conftest.py
fake_llm fixture). Responses are OpenAI-shaped dicts returned verbatim.

FakeImageAdapter implements deaddit.images.client.ImageAdapter; register it
via deaddit.images.client.register_adapter(provider_type, adapter) so
service/agent tests never dispatch to a real provider.
"""

from __future__ import annotations

import deaddit.llm.transport as _llm_transport


def _content_response(content: str) -> dict:
    return {
        "choices": [
            {"message": {"role": "assistant", "content": content}},
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class FakeProvider:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self._queue: list = []

    def enqueue(self, response: dict) -> None:
        """Queue an OpenAI-shaped response dict, returned verbatim."""
        self._queue.append(response)

    def enqueue_content(self, content: str) -> None:
        self.enqueue(_content_response(content))

    def enqueue_tool_calls(self, calls: list[dict], content: str | None = None) -> None:
        message: dict = {"role": "assistant", "tool_calls": calls}
        if content is not None:
            message["content"] = content
        self.enqueue({"choices": [{"message": message}], "usage": {}})

    def enqueue_error(self, exc: Exception) -> None:
        self._queue.append(exc)

    def enqueue_stream(self, chunks: list[dict], usage: dict | None = None) -> None:
        """Queue a streaming response: OpenAI SSE-chunk-shaped dicts.

        When ``usage`` is given, a final usage-carrying chunk is appended
        before the end of the stream (mirroring real providers that send
        ``stream_options: {include_usage: true}``-style trailers).
        """
        self._queue.append(("stream", list(chunks), usage))

    def enqueue_stream_error(self, exc: Exception) -> None:
        """Queue an exception raised by stream_chat instead of chunks."""
        self._queue.append(exc)

    def _next_item(self):
        if not self._queue:
            raise AssertionError(
                "FakeProvider queue is empty: enqueue a response before "
                "calling code that hits the LLM transport"
            )
        return self._queue.pop(0)

    def post_chat(
        self,
        *,
        api_url,
        payload,
        api_key,
        request_id,
        **kwargs,
    ) -> dict:
        self.requests.append(
            {
                "api_url": api_url,
                "payload": payload,
                "api_key": api_key,
                "request_id": request_id,
            }
        )
        item = self._next_item()
        if isinstance(item, Exception):
            raise item
        return item

    def stream_chat(
        self,
        api_url=None,
        payload=None,
        api_key=None,
        request_id=None,
        **kwargs,
    ):
        """Signature-compatible with deaddit.llm.transport.stream_chat.

        Mirrors the real transport's observable side effects: sets
        ``payload['stream'] = True`` and records one attempt in the
        transport thread-local when the stream completes.
        """
        self.requests.append(
            {
                "api_url": api_url,
                "payload": payload,
                "api_key": api_key,
                "request_id": request_id,
            }
        )
        if payload is not None:
            payload["stream"] = True
        item = self._next_item()
        if isinstance(item, Exception):
            raise item
        _, chunks, usage = item
        if usage is not None:
            chunks = [*chunks, {"choices": [], "usage": usage}]
        yield from chunks
        _llm_transport._last_attempts.value = 1


class FakeImageAdapter:
    """Deterministic ImageAdapter: queue results/errors per method by hand.

    Each dispatch method records its call args and pops the next queued
    item, raising it if it is an exception. An empty queue is a test bug,
    not a silent no-op, so it raises AssertionError immediately.
    """

    def __init__(self) -> None:
        self.search_calls: list[dict] = []
        self.validate_calls: list[dict] = []
        self.generate_calls: list[dict] = []
        self._search_queue: list = []
        self._validate_queue: list = []
        self._generate_queue: list = []

    def enqueue_search(self, result) -> None:
        """Queue a ModelSearchResult returned verbatim by search_models."""
        self._search_queue.append(result)

    def enqueue_validate(self, result) -> None:
        """Queue a ModelValidation returned verbatim by validate_model."""
        self._validate_queue.append(result)

    def enqueue_generate(self, result) -> None:
        """Queue an ImageGenerationResult returned verbatim by generate."""
        self._generate_queue.append(result)

    def enqueue_error(self, exc: Exception, *, method: str = "generate") -> None:
        """Queue *exc* to be raised by the named method's next call."""
        {
            "search_models": self._search_queue,
            "validate_model": self._validate_queue,
            "generate": self._generate_queue,
        }[method].append(exc)

    def _pop(self, queue: list, method: str):
        if not queue:
            raise AssertionError(
                f"FakeImageAdapter.{method} queue is empty: enqueue a result "
                "before calling code that dispatches to the image adapter"
            )
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def search_models(self, provider, credential, query, cursor):
        self.search_calls.append(
            {
                "provider": provider,
                "credential": credential,
                "query": query,
                "cursor": cursor,
            }
        )
        return self._pop(self._search_queue, "search_models")

    def validate_model(self, provider, credential, model_id):
        self.validate_calls.append(
            {"provider": provider, "credential": credential, "model_id": model_id}
        )
        return self._pop(self._validate_queue, "validate_model")

    def generate(self, provider, credential, model_id, prompt, deadline):
        self.generate_calls.append(
            {
                "provider": provider,
                "credential": credential,
                "model_id": model_id,
                "prompt": prompt,
                "deadline": deadline,
            }
        )
        return self._pop(self._generate_queue, "generate")
