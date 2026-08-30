"""Native tool-call support: tool specs, argument validation, tool results.

Native ``tool_calls`` are the only structured-output mechanism (Resolution 11);
every tool invocation is validated against its declared pydantic parameter
model before the arguments reach application code.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ValidationError

from deaddit.llm.errors import SchemaValidationError


class ToolSpec:
    """A callable tool exposed to the model, backed by a pydantic schema."""

    def __init__(
        self, name: str, description: str, parameters_model: type[BaseModel]
    ) -> None:
        self.name = name
        self.description = description
        self.parameters_model = parameters_model

    def to_openai_tool(self) -> dict:
        """OpenAI function-tool shape derived from the pydantic model."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_model.model_json_schema(),
            },
        }


def validate_tool_args(spec: ToolSpec, arguments: dict | str) -> dict:
    """Validate raw tool arguments against a spec's pydantic model.

    Accepts a parsed dict or a JSON string; returns the validated, coerced
    dict. Raises SchemaValidationError (chained from the pydantic error) on
    missing, extra, or mistyped fields.
    """
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise SchemaValidationError(
                f"Tool {spec.name!r} arguments are not valid JSON: {exc}"
            ) from exc
    if isinstance(arguments, dict):
        allowed = set(spec.parameters_model.model_fields)
        unknown = set(arguments) - allowed
        if unknown:
            names = ", ".join(sorted(repr(k) for k in unknown))
            raise SchemaValidationError(
                f"Tool {spec.name!r} received unexpected arguments: {names}"
            )
    try:
        validated = spec.parameters_model.model_validate(arguments)
    except ValidationError as exc:
        raise SchemaValidationError(
            f"Tool {spec.name!r} received invalid arguments: {exc}"
        ) from exc
    return validated.model_dump()
