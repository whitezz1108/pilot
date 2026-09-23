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

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ModelClientError",
    "ModelOutputError",
    "ModelParams",
    "ModelMessage",
    "TokenUsage",
    "ModelRequest",
    "ModelResponse",
    "ModelClient",
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
    timeout_seconds: float = Field(default=60.0, gt=0.0)

    def fingerprint(self) -> str:
        """A stable one-line identity for this parameter set."""
        top_p = "default" if self.top_p is None else f"{self.top_p:g}"
        return (
            f"{self.provider}:{self.model_id}"
            f"/T={self.temperature:g}/top_p={top_p}/max={self.max_output_tokens}"
        )


class ModelMessage(BaseModel):
    """One chat message. ``system`` and ``user`` only -- see the note below.

    ``assistant`` exists because the repair turn quotes the model's own previous
    output back to it. No conversation history is ever carried between nodes:
    each invocation builds its messages from scratch, and no object in this
    codebase stores a message list across calls.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str


class TokenUsage(BaseModel):
    """Token accounting, when the provider reports it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class ModelRequest(BaseModel):
    """One model invocation: what to send, and what to record about sending it.

    Built by an agent node from its restricted view. The provenance fields
    (``role``, ``prompt_version``, ``invocation``, ``run_id``) are for the
    raw-call log; they are not sent to the provider.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: tuple[ModelMessage, ...]
    params: ModelParams
    role: str
    """Workflow role this call belongs to: ``manager`` or ``compliance``."""
    prompt_version: str
    """Version identifier of the prompt that produced ``messages``."""
    invocation: int = Field(default=0, ge=0)
    """Fresh-context index for this call. Part of the audit trail, not the prompt."""
    run_id: str | None = None
    """Run this call belongs to, when the node can observe it."""

    def rendered_input(self) -> str:
        """Every message sent, in order, exactly as sent.

        This is the "rendered permitted input" the raw-call log records: the
        content is derived from a restricted view and nothing else.
        """
        return "\n\n".join(
            f"### {message.role}\n{message.content}" for message in self.messages
        )


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
    off at ``max_output_tokens`` -- and a truncated response is not a model that
    failed to follow the schema, so the two must not be confused. Gate 4 found
    them confused: this provider is a reasoning model that spends output tokens
    before emitting any content, so a budget too small for its reasoning yields
    an *empty* string with ``finish_reason="length"``, which downstream is
    indistinguishable from a model that answered with nothing.

    ``None`` when the provider does not report it. Recorded, never interpreted
    here: what to do about a truncation is the caller's decision, not the
    adapter's.
    """

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
