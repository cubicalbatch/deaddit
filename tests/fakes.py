"""Deterministic fakes for LLM transport tests.

FakeProvider is signature-compatible with deaddit.llm.transport.post_chat;
register it via deaddit.llm.provider.set_provider() (see tests/conftest.py
fake_llm fixture). Responses are OpenAI-shaped dicts returned verbatim.
"""

from __future__ import annotations


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

    def enqueue_tool_calls(
        self, calls: list[dict], content: str | None = None
    ) -> None:
        message: dict = {"role": "assistant", "tool_calls": calls}
        if content is not None:
            message["content"] = content
        self.enqueue({"choices": [{"message": message}], "usage": {}})

    def enqueue_error(self, exc: Exception) -> None:
        self._queue.append(exc)

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
        if not self._queue:
            raise AssertionError(
                "FakeProvider queue is empty: enqueue a response before "
                "calling code that hits the LLM transport"
            )
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
