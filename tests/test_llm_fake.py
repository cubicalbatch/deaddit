"""Tests for the LLM provider seam and FakeProvider-driven client paths."""

from __future__ import annotations

import pytest

from deaddit.llm import ChatRequest, ChatResult, LLMClient, reset_provider
from deaddit.llm.errors import PermanentLLMError
from tests.conftest import load_fixture


def _request(model: str = "test-model") -> ChatRequest:
    return ChatRequest(
        system_prompt="You are a helpful assistant.",
        user_prompt="Say something.",
        model=model,
        api_url="http://localhost/v1",
        api_key="sk-test",
    )


class TestClientWithFakeProvider:
    def test_content_response_fields(self, fake_llm):
        fake_llm.enqueue_content("Hello from the fake model.")
        result = LLMClient().complete(_request(model="fake-model"))

        assert isinstance(result, ChatResult)
        assert result.content == "Hello from the fake model."
        assert result.model == "fake-model"
        assert result.attempts >= 0  # transport-reported; fakes don't set it
        assert isinstance(result.request_id, str) and result.request_id
        assert result.tool_calls is None
        assert result.usage["total_tokens"] == 2

    def test_request_id_passthrough(self, fake_llm):
        fake_llm.enqueue_content("ok")
        req = _request()
        req.request_id = "custom-id-42"
        result = LLMClient().complete(req)
        assert result.request_id == "custom-id-42"

    def test_tool_calls_surfaced(self, fake_llm):
        calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "create_post",
                    "arguments": '{"title": "Hi"}',
                },
            }
        ]
        fake_llm.enqueue_tool_calls(calls)
        result = LLMClient().complete(_request())
        assert result.tool_calls == calls
        assert result.content == ""

    def test_typed_error_propagates(self, fake_llm):
        fake_llm.enqueue_error(PermanentLLMError("bad response shape"))
        with pytest.raises(PermanentLLMError):
            LLMClient().complete(_request())

    def test_recorded_payload_contains_model_and_messages(self, fake_llm):
        fake_llm.enqueue_content("recorded")
        LLMClient().complete(_request(model="payload-check"))

        assert len(fake_llm.requests) == 1
        recorded = fake_llm.requests[0]
        assert recorded["api_url"] == "http://localhost/v1"
        assert recorded["api_key"] == "sk-test"
        assert recorded["payload"]["model"] == "payload-check"
        roles = [m["role"] for m in recorded["payload"]["messages"]]
        assert roles == ["system", "user"]


class TestGenerationPaths:
    """Legacy callers construct LLMClient() internally; the seam serves them."""

    def test_jobs_send_openai_request(self, fake_llm):
        import deaddit.jobs as jobs

        fake_llm.enqueue_content("canned jobs content")
        content, model = jobs._send_openai_request("sys", "user prompt", "m1")

        assert content == "canned jobs content"
        assert model == "m1"
        assert len(fake_llm.requests) == 1
        assert fake_llm.requests[0]["payload"]["model"] == "m1"

    def test_loader_send_request(self, fake_llm):
        import deaddit.loader as loader

        fake_llm.enqueue_content("canned loader content")
        result = loader.send_request("sys", "user prompt")

        assert result is not None
        response, model = result
        assert response.choices[0].message.content == "canned loader content"
        assert isinstance(model, str) and model
        assert len(fake_llm.requests) == 1


class TestFixtureDrivenResponses:
    def test_tool_call_json_fixture(self, fake_llm):
        response = load_fixture("tool_call_response.json")
        fake_llm.enqueue(response)

        result = LLMClient().complete(_request())

        assert result.tool_calls is not None
        call = result.tool_calls[0]
        assert call["function"]["name"] == "create_post"
        # Schema-valid args: the JSON parses and carries the expected keys.
        import json

        args = json.loads(call["function"]["arguments"])
        assert set(args) == {"subdeaddit", "title", "content"}
        assert result.content == ""


@pytest.mark.usefixtures("fake_llm")
def test_reset_provider_restores_transport_fallback():
    # After fixture teardown, get_provider() must fall back to transport.
    from deaddit.llm.provider import get_provider

    try:
        reset_provider()
        provider = get_provider()
        assert callable(provider)
        assert provider.__module__ == "deaddit.llm.transport"
        assert provider.__name__ == "post_chat"
    finally:
        reset_provider()
