"""The provider-neutral model-call protocol and its request/response records.

Deliberately small. One method, one request type, one response type. A
multi-provider framework is not built here because the codebase does not need
one: the single live adapter in :mod:`pilot01.model.openai_compat` speaks the
OpenAI-compatible chat-completions shape, which every provider this pilot is
likely to use exposes.

What the boundary guarantees, and why each part is here:

* :class:`ModelRequest` carries the **rendered messages**, the **parameters**,
  and the **provenance** of the call (role, prompt version, invocation). It
  carries no hidden experimental data, and there is no field one could be put
  in -- ``extra="forbid"`` closes the obvious route.
* :class:`ModelResponse` carries the raw text plus everything needed to
  reproduce and audit the call: provider, exact model id, timestamps, latency,
  provider request id, token usage.
* Credentials appear in **neither**. A provider credential belongs to the
  transport object, is read from the environment, and is never copied into a
  request or a response -- so it cannot reach the raw-call log even by accident.
"""

from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .tool_protocol import ModelToolCall, ToolDefinition

__all__ = [
    "ModelClientError",
    "ModelOutputError",
    "ModelParams",
    "PreparationModelParams",
    "ModelMessage",
    "ModelToolCall",
    "ToolDefinition",
    "TokenUsage",
    "ModelRequest",
    "ModelResponse",
    "ModelClient",
    "prepare_model_request",
    "estimate_model_request_tokens",
]


class ModelClientError(RuntimeError):
    """The provider could not be reached, or refused the request.

    A transport failure. The workflow layer converts it into a
    :class:`~pilot01.workflow.transitions.ProtocolError` so the run aborts and
    the failure is recorded, rather than being smoothed over.
    """


class ModelOutputError(RuntimeError):
    """A model response could not be turned into the required schema.

    Raised only after the single permitted format-only repair attempt has also
    failed. It means the call produced no usable observation -- never that
    Python supplied a value the model did not.
    """


class ModelParams(BaseModel):
    """Everything that determines a model's output, recorded per call.

    This is the reproducibility record. Two runs that differ in any field here
    are not comparable, so the field set is closed and every field is logged.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    """Adapter identity, e.g. ``openai-compatible``. Not the model vendor."""
    model_id: str
    """The exact model identifier sent to the provider."""
    temperature: float = Field(ge=0.0, le=2.0)
    max_output_tokens: int = Field(gt=0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    """Sent only when set; ``None`` means "provider default", not 1.0."""
    top_k: int | None = Field(default=None, ge=1, exclude_if=lambda value: value is None)
    min_p: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        exclude_if=lambda value: value is None,
    )
    presence_penalty: float | None = Field(
        default=None,
        ge=-2.0,
        le=2.0,
        exclude_if=lambda value: value is None,
    )
    repetition_penalty: float | None = Field(
        default=None,
        gt=0.0,
        exclude_if=lambda value: value is None,
    )
    seed: int | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)
    timeout_seconds: float = Field(default=60.0, gt=0.0)
    reasoning_effort: str | None = Field(default=None, min_length=1)

    def fingerprint(self) -> str:
        """A stable one-line identity for this parameter set."""
        top_p = "default" if self.top_p is None else f"{self.top_p:g}"
        fingerprint = (
            f"{self.provider}:{self.model_id}"
            f"/T={self.temperature:g}/top_p={top_p}/max={self.max_output_tokens}"
            + (f"/reasoning={self.reasoning_effort}" if self.reasoning_effort is not None else "")
        )
        for name in (
            "top_k",
            "min_p",
            "presence_penalty",
            "repetition_penalty",
            "seed",
        ):
            value = getattr(self, name)
            if value is not None:
                fingerprint += f"/{name}={value:g}"
        return fingerprint


class PreparationModelParams(ModelParams):
    """Offline material preparation has its own provider parameters.

    None omits the client output cap; provider defaults and limits still apply.
    Experiment ModelParams continues to require an explicit positive cap.
    """

    max_output_tokens: int | None = Field(default=None, gt=0)

    def fingerprint(self) -> str:
        if self.max_output_tokens is not None:
            return super().fingerprint()
        top_p = "default" if self.top_p is None else f"{self.top_p:g}"
        fingerprint = f"{self.provider}:{self.model_id}/T={self.temperature:g}/top_p={top_p}/max=provider-default"
        for name in (
            "top_k",
            "min_p",
            "presence_penalty",
            "repetition_penalty",
            "seed",
        ):
            value = getattr(self, name)
            if value is not None:
                fingerprint += f"/{name}={value:g}"
        return fingerprint


class ModelMessage(BaseModel):
    """One provider-neutral chat message, including native tool turns."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = Field(default=None, exclude_if=lambda value: value is None)
    tool_calls: tuple[ModelToolCall, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    tool_call_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    name: str | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _valid_shape(self) -> "ModelMessage":
        if self.role in {"system", "user"}:
            if self.content is None or self.tool_calls or self.tool_call_id or self.name:
                raise ValueError(f"{self.role} messages carry content only")
        elif self.role == "assistant":
            if self.tool_call_id or self.name:
                raise ValueError("assistant messages cannot identify a tool result")
            if self.tool_calls and self.content not in (None, ""):
                raise ValueError("assistant content and tool_calls are mutually exclusive")
            if not self.tool_calls and self.content is None:
                raise ValueError("assistant messages need content or tool_calls")
        else:
            if self.content is None or not self.tool_call_id or not self.name or self.tool_calls:
                raise ValueError("tool messages require content, name and tool_call_id")
        return self

    def wire_dict(self) -> dict:
        """Render the OpenAI-compatible message shape without provider logic."""
        payload: dict[str, object] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.raw_arguments
                        if call.raw_arguments is not None
                        else json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in self.tool_calls
            ]
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            payload["name"] = self.name
        return payload


class TokenUsage(BaseModel):
    """Token accounting, when the provider reports it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = Field(default=None, exclude_if=lambda v: v is None)
    cached_tokens: int | None = Field(default=None, exclude_if=lambda v: v is None)


class ModelRequest(BaseModel):
    """One model invocation: what to send, and what to record about sending it.

    Built by an agent node from its restricted view. The provenance fields
    (``role``, ``prompt_version``, ``invocation``, ``run_id``) are for the
    raw-call log; they are not sent to the provider.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolDefinition, ...] = Field(default=(), exclude_if=lambda value: not value)
    tool_choice: Literal["auto", "none", "required"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    parallel_tool_calls: bool | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    native_output_format: Literal["closed-final-or-tool-v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    final_output_schema: dict | None = Field(default=None, exclude_if=lambda value: value is None)
    model_call_id: str = Field(default_factory=lambda: uuid4().hex)
    params: ModelParams | PreparationModelParams
    role: str
    """Workflow role this call belongs to: ``manager`` or ``compliance``."""
    prompt_version: str
    """Version identifier of the prompt that produced ``messages``."""
    invocation: int = Field(default=0, ge=0)
    """Fresh-context index for this call. Part of the audit trail, not the prompt."""
    run_id: str | None = None
    """Run this call belongs to, when the node can observe it."""

    @model_validator(mode="after")
    def _preparation_params_only_for_preparation(self) -> "ModelRequest":
        if (self.native_output_format is None) != (self.final_output_schema is None):
            raise ValueError("closed native output requires both version and final output schema")
        if isinstance(self.params, PreparationModelParams) and self.role not in {"material_generator", "material_reviewer"}:
            raise ValueError("preparation model parameters are only allowed for material preparation roles")
        if self.tool_choice is not None and not self.tools:
            raise ValueError("tool_choice requires at least one tool definition")
        if self.tools and len({tool.name for tool in self.tools}) != len(self.tools):
            raise ValueError("tool names must be unique within one request")
        return self

    def rendered_input(self) -> str:
        """Every message sent, in order, exactly as sent.

        This is the "rendered permitted input" the raw-call log records: the
        content is derived from a restricted view and nothing else.
        """
        rendered = "\n\n".join(
            f"### {message.role}\n"
            + (
                message.content
                if message.content is not None and not message.tool_calls
                else json.dumps(message.wire_dict(), ensure_ascii=False, sort_keys=True)
            )
            for message in self.messages
        )
        if self.tools:
            rendered += "\n\n### tools\n" + json.dumps(
                [tool.model_dump(mode="json") for tool in self.tools],
                ensure_ascii=False,
                sort_keys=True,
            )
        return rendered


class ModelResponse(BaseModel):
    """One model reply, plus the provenance needed to audit it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    """The raw response text, unmodified. Parsing happens after logging."""
    provider: str
    model_id: str
    requested_at: datetime
    responded_at: datetime
    request_id: str | None = None
    usage: TokenUsage | None = None
    finish_reason: str | None = None
    """Why the provider stopped generating, when it says.

    ``"stop"`` means the model finished its answer. ``"length"`` means it was cut
    off at a provider-enforced length limit, which can be smaller than the
    requested ``max_output_tokens`` -- and a truncated response is not a model that
    failed to follow the schema, so the two must not be confused. Gate 4 found
    them confused: this provider is a reasoning model that spends output tokens
    before emitting any content, so a budget too small for its reasoning yields
    an *empty* string with ``finish_reason="length"``, which downstream is
    indistinguishable from a model that answered with nothing.

    ``None`` when the provider does not report it. Recorded, never interpreted
    here: what to do about a truncation is the caller's decision, not the
    adapter's.
    """
    reasoning_content: str | None = Field(default=None, exclude_if=lambda v: v is None)
    reasoning_observation: dict | None = Field(default=None, exclude_if=lambda v: v is None)
    provider_response: dict | None = Field(default=None, exclude_if=lambda v: v is None)
    tool_calls: tuple[ModelToolCall, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    """Provider-normalized structured tool calls, if any."""

    @property
    def truncated(self) -> bool:
        """Whether the provider says it hit the output limit."""
        return self.finish_reason == "length"

    @property
    def latency_seconds(self) -> float:
        return (self.responded_at - self.requested_at).total_seconds()


@runtime_checkable
class ModelClient(Protocol):
    """The entire provider boundary.

    Implementations must be deterministic when driven deterministically: a
    client that talks to a live provider is opt-in at construction time, and no
    code path in this repository constructs one implicitly.
    """

    def generate(self, request: ModelRequest) -> ModelResponse:
        """Run one completion.

        Raises :class:`ModelClientError` on any transport or provider failure.
        """
        ...


def prepare_model_request(client: ModelClient, request: ModelRequest) -> ModelRequest:
    """Apply an optional transport-owned request budget before sending/logging.

    Scripted and historical clients have no preparation hook and therefore
    retain their exact requests. Live local clients may deterministically lower
    an output ceiling so the declared input plus completion fits the endpoint.
    """
    prepare = getattr(client, "prepare_request", None)
    if prepare is None:
        return request
    prepared = prepare(request)
    if not isinstance(prepared, ModelRequest):
        raise ModelClientError("model client prepare_request must return ModelRequest")
    return prepared


def estimate_model_request_tokens(
    client: ModelClient, request: ModelRequest
) -> int | None:
    """Return a local provider-input estimate when the adapter supports one."""
    estimate = getattr(client, "estimate_request_tokens", None)
    if estimate is None:
        return None
    value = estimate(request)
    if type(value) is not int or value < 0:
        raise ModelClientError("model client returned an invalid token estimate")
    return value
