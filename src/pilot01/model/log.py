"""The raw model-call log: a private, append-only record of what was sent.

This is **not** the experimental export. It is the private audit trail of the
model calls themselves, and it is deliberately a different type from
:class:`~pilot01.workflow.export.ExecutionRecord`:

* ``ExecutionRecord`` is public and export-safe. It carries no hidden data, and
  a validator walks its own serialization to prove it.
* ``ModelCallRecord`` is private. It carries raw provider responses -- which may
  contain anything the model wrote -- and it is never merged into, or reused as,
  the execution record. Reusing ``ExecutionRecord`` for this would have meant
  relaxing the guarantees that make it safe to publish, so the two stay apart.

What is recorded per call: run id, role, prompt version, provider, exact model
id, the full parameter set, the rendered permitted input, request and response
timestamps, latency, provider request id, token usage, the raw response text,
the parsed output or the parsing failure, and the repair attempt if one
happened. One record per *call*: a call that needed a format repair produces two
records, the first carrying ``parse_error`` and the second carrying
``is_repair=True`` and the reason. Both raw responses are therefore preserved.

Two things are structurally impossible here:

* **Credentials.** There is no field for a key, a header, or an authorization
  value, and no adapter copies one into a request or response. As a second line
  of defence :func:`assert_no_secrets` scans each record's serialized form for
  key-shaped and bearer-shaped strings before it is accepted.
* **Hidden gold.** There is no gold field either. The record is built from a
  :class:`~pilot01.model.client.ModelRequest` that was itself rendered from a
  restricted view, and the node audits that request for hidden-shaped keys
  before sending it.

Persistence is JSONL, one record per line, in a file the caller names. No
database, no server, no batch infrastructure.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .client import ModelParams, TokenUsage

__all__ = [
    "SecretLeakError",
    "SECRET_PATTERNS",
    "ModelCallRecord",
    "ModelCallLog",
    "assert_no_secrets",
]


class SecretLeakError(RuntimeError):
    """A model-call record contains something shaped like a credential."""


SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)\b(api[_-]?key|authorization|x-api-key)\b\s*[:=]\s*\S{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
)
"""Shapes that must never appear in a call record, whatever their provenance."""


def assert_no_secrets(record: "ModelCallRecord") -> None:
    """Raise :class:`SecretLeakError` if a record carries credential-shaped text.

    Scans the serialized record rather than named fields, so a secret that
    arrived inside a response body or a rendered prompt is caught too.
    """
    serialized = record.model_dump_json()
    for pattern in SECRET_PATTERNS:
        match = pattern.search(serialized)
        if match:
            raise SecretLeakError(
                "refusing to log a model call: the record contains a "
                f"credential-shaped string matching {pattern.pattern!r}. "
                "Credentials belong to the transport object and must never enter "
                "a request, a response, or the raw-call log."
            )


class ModelCallRecord(BaseModel):
    """One model call, in full. Private; never exported as an experimental record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_index: int = Field(ge=0, description="Dense, monotonic, per-log sequence.")
    run_id: str | None = None
    role: str
    invocation: int = Field(ge=0)
    prompt_version: str

    params: ModelParams
    """Provider, exact model id, temperature, top_p, max output tokens, timeout."""

    rendered_input: str
    """Every message sent, in order, exactly as sent."""

    attempt: int = Field(ge=0, description="0 for the original call, 1 for the repair.")
    is_repair: bool = False
    repair_reason: str | None = None
    """Why the repair was requested. Set only on a repair record."""

    requested_at: datetime | None = None
    responded_at: datetime | None = None
    latency_seconds: float | None = None
    request_id: str | None = None
    usage: TokenUsage | None = None
    finish_reason: str | None = None
    """Why the provider stopped, when it reports it.

    ``"length"`` records that this call was cut off at ``max_output_tokens``.
    Without it an empty or half-written response reads as a model that ignored
    the schema, and the two need different fixes: one is a bigger budget, the
    other is a better prompt. Gate 4 hit exactly that ambiguity.
    """

    raw_response: str | None = None
    """The provider's response text, unmodified."""
    parsed_output: dict[str, Any] | None = None
    """The validated domain object, or ``None`` when parsing failed."""
    parse_error: str | None = None
    """Why parsing failed, when it did."""

    is_tool_turn: bool = False
    """Whether this turn asked for a source tool instead of answering.

    A tool turn parses into no domain object by design, so it carries neither
    ``parsed_output`` nor ``parse_error``: the request it made is recorded here,
    and what the tool returned is recorded in the tool-call log. The two logs
    together are the whole turn, and neither is complete without the other.
    """
    tool_request: dict[str, Any] | None = None
    """The tool request this turn made, exactly as the model wrote it."""

    error: str | None = None
    """Transport-level failure, when the call never produced a response."""

    def to_jsonl(self) -> str:
        return self.model_dump_json() + "\n"


class ModelCallLog:
    """An append-only sink for :class:`ModelCallRecord`.

    In-memory by default; pass ``path`` to also append JSONL to disk. The file
    is created fresh, because a raw-call log belongs to one run: appending
    across runs would make ``call_index`` ambiguous.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._records: list[ModelCallRecord] = []
        self._path = Path(path) if path is not None else None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text("", encoding="utf-8")

    @property
    def path(self) -> Path | None:
        return self._path

    def append(self, record: ModelCallRecord) -> ModelCallRecord:
        """Validate and append one record. The only mutating operation."""
        assert_no_secrets(record)
        self._records.append(record)
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(record.to_jsonl())
        return record

    @property
    def records(self) -> tuple[ModelCallRecord, ...]:
        """Immutable snapshot, in append order."""
        return tuple(self._records)

    def of_role(self, role: str) -> tuple[ModelCallRecord, ...]:
        return tuple(record for record in self._records if record.role == role)

    def repairs(self) -> tuple[ModelCallRecord, ...]:
        return tuple(record for record in self._records if record.is_repair)

    def tool_turns(self) -> tuple[ModelCallRecord, ...]:
        """Turns that requested a source tool rather than answering."""
        return tuple(record for record in self._records if record.is_tool_turn)

    def __len__(self) -> int:
        return len(self._records)

    def to_jsonl(self) -> str:
        return "".join(record.to_jsonl() for record in self._records)
