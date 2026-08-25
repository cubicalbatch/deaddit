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


_registry: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> None:
    """Add a tool spec to the module-level registry (replaces same-name)."""
    _registry[spec.name] = spec


def get(name: str) -> ToolSpec:
    """Look up a registered spec by name; KeyError if missing."""
    return _registry[name]


def all_specs() -> list[ToolSpec]:
    return list(_registry.values())


def clear_registry() -> None:
    """Empty the registry (for tests)."""
    _registry.clear()


def validate_tool_args(spec_or_name: ToolSpec | str, arguments: dict | str) -> dict:
    """Validate raw tool arguments against a spec's pydantic model.

    Accepts a parsed dict or a JSON string; returns the validated, coerced
    dict. Raises SchemaValidationError (chained from the pydantic error) on
    missing, extra, or mistyped fields.
    """
    spec = spec_or_name if isinstance(spec_or_name, ToolSpec) else get(spec_or_name)
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


def build_tool_results(tool_calls: list[dict]) -> list[dict]:
    """Build OpenAI-shaped tool-role messages for assistant tool_calls.

    Valid calls produce the validated arguments as JSON content; invalid
    arguments or unknown tool names produce a JSON ``{"error": "<why>"}``
    content instead, so the model can see and correct the failure.
    """
    results: list[dict] = []
    for call in tool_calls:
        call_id = call.get("id")
        function = call.get("function") or {}
        name = function.get("name")
        try:
            spec = get(name)
        except KeyError:
            content = json.dumps({"error": f"Unknown tool: {name!r}"})
        else:
            try:
                args = validate_tool_args(spec, function.get("arguments"))
            except SchemaValidationError as exc:
                content = json.dumps({"error": str(exc)})
            else:
                content = json.dumps(args)
        results.append({"role": "tool", "tool_call_id": call_id, "content": content})
    return results
