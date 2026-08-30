"""Tests for native tool-call support: specs, arg validation, gating."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from deaddit.llm.client import ChatRequest, LLMClient
from deaddit.llm.errors import (
    CapabilityError,
    PermanentLLMError,
    SchemaValidationError,
)
from deaddit.llm.tools import (
    ToolSpec,
    validate_tool_args,
)

API_URL = "http://llm.test/v1"


class EchoArgs(BaseModel):
    message: str
    count: int = 1


ECHO = ToolSpec(name="echo", description="Echo a message", parameters_model=EchoArgs)


def _request(**overrides) -> ChatRequest:
    kwargs: dict = {
        "system_prompt": "sys",
        "user_prompt": "hi",
        "model": "m1",
        "api_url": API_URL,
    }
    kwargs.update(overrides)
    return ChatRequest(**kwargs)


# --- 1. ToolSpec.to_openai_tool() shape --------------------------------------


def test_to_openai_tool_shape():
    tool = ECHO.to_openai_tool()
    assert tool["type"] == "function"
    function = tool["function"]
    assert function["name"] == "echo"
    assert function["description"] == "Echo a message"
    schema = function["parameters"]
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"message", "count"}
    assert schema["properties"]["message"]["type"] == "string"
    assert schema["properties"]["count"]["type"] == "integer"
    assert "message" in schema.get("required", [])


# --- 2. validate_tool_args ----------------------------------------------------


def test_validate_accepts_dict_and_json_str():
    assert validate_tool_args(ECHO, {"message": "hi", "count": "3"}) == {
        "message": "hi",
        "count": 3,
    }
    assert validate_tool_args(ECHO, '{"message": "yo"}') == {
        "message": "yo",
        "count": 1,
    }


@pytest.mark.parametrize(
    ("bad_args", "why"),
    [
        ('{"count": 2}', "missing required field"),
        ('{"message": "x", "extra": true}', "extra field"),
        ('{"message": ["not", "a", "string"]}', "wrong type"),
    ],
)
def test_validate_rejects_bad_arguments(bad_args, why):
    with pytest.raises(SchemaValidationError) as excinfo:
        validate_tool_args(ECHO, bad_args)
    if why == "extra field":
        # Rejected against the declared schema before pydantic runs.
        assert "unexpected arguments" in str(excinfo.value)
    else:
        # SchemaValidationError is chained from the pydantic error.
        assert isinstance(excinfo.value.__cause__, ValidationError)


def test_validate_rejects_non_json_string():
    with pytest.raises(SchemaValidationError):
        validate_tool_args(ECHO, "{not json")


# --- 4. payload serialization --------------------------------------------------


def test_payload_contains_serialized_tools(app, fake_llm):
    fake_llm.enqueue_content("ok")
    LLMClient().complete(_request(tools=[ECHO]))
    assert fake_llm.requests[0]["payload"]["tools"] == [ECHO.to_openai_tool()]


# --- 5. gating on supports_tools=False ------------------------------------------


def test_gating_blocks_before_provider(app, db_session, fake_llm):
    from deaddit.models import EndpointCapability

    db_session.add(
        EndpointCapability(
            api_url=API_URL, model_name="blocked-model", supports_tools=False
        )
    )
    db_session.commit()

    with pytest.raises(CapabilityError):
        LLMClient().complete(
            _request(api_url=API_URL, model="blocked-model", tools=[ECHO])
        )
    # The provider never saw a request.
    assert fake_llm.requests == []


# --- 6. tools-shaped HTTP 400 -> CapabilityError + stale marking -----------------


def test_http400_with_tools_becomes_capability_error(app, fake_llm):
    fake_llm.enqueue_error(
        PermanentLLMError(
            'HTTP 400 from url http://x/v1/chat/completions: "tools" is not supported'
        )
    )
    with pytest.raises(CapabilityError) as excinfo:
        LLMClient().complete(_request(tools=[ECHO]))
    assert isinstance(excinfo.value.__cause__, PermanentLLMError)
    assert excinfo.value.api_url == API_URL
    assert excinfo.value.model == "m1"
    assert len(fake_llm.requests) == 1


# --- 7. other permanent errors propagate unchanged -------------------------------


@pytest.mark.parametrize(
    ("message",),
    [
        ("HTTP 401 unauthorized",),
        ('HTTP 400 bad request: malformed "payload" section',),
    ],
)
def test_other_permanent_errors_propagate(app, fake_llm, message):
    fake_llm.enqueue_error(PermanentLLMError(message))
    with pytest.raises(PermanentLLMError) as excinfo:
        LLMClient().complete(_request(tools=[ECHO]))
    assert not isinstance(excinfo.value, CapabilityError)
