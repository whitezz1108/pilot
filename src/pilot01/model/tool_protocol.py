"""Provider-neutral types for native tool calling.

The workflow owns the meaning of a tool call; a provider adapter only maps its
wire format into these closed types.  Keeping the types here prevents the
OpenAI-compatible transport, SGLang's parser vocabulary, and the source-tool
implementation from leaking into one another.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "ToolDefinition",
    "ModelToolCall",
    "ToolTransport",
]


ToolTransport = Literal["legacy-text", "native"]


class ToolDefinition(BaseModel):
    """One tool exposed to a model, described with JSON Schema."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    input_schema: dict[str, Any]

    @model_validator(mode="after")
    def _object_schema(self) -> "ToolDefinition":
        if self.input_schema.get("type") != "object":
            raise ValueError("a tool input schema must have type='object'")
        if self.input_schema.get("additionalProperties") is not False:
            raise ValueError("a tool input schema must reject undeclared arguments")
        return self

    def openai_function(self) -> dict[str, Any]:
        """Return the standard Chat Completions function-tool shape."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ModelToolCall(BaseModel):
    """A provider-normalized tool call, valid or malformed.

    ``arguments`` deliberately remains ``Any``.  A provider can return broken
    JSON or a non-object value; preserving it lets the workflow record and
    refuse what the model actually emitted instead of turning a model protocol
    error into a transport error.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: Any
    raw_arguments: str | None = Field(default=None, exclude_if=lambda v: v is None)
