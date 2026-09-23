"""Raw model text to validated domain object, with one format-only repair.

The flow is fixed and has no branch in which Python decides anything
substantive::

    raw response text
      -> extract a JSON object (tolerating fences and surrounding prose)
      -> validate against the *existing* pydantic output schema
      -> domain object

Schemas are not duplicated here. ``ManagerOutput`` and ``ComplianceOutput`` are
the same classes the runner validates against, so a parsed object is a domain
object by construction.

**Repair.** If the first response cannot be turned into a valid object, at most
one further call is made, and that call is *format only*. It is handed the
model's own previous response and the validation error, and nothing else: no
task restatement, no policy, no memo, no handoff, no new evidence, no
indication of what the answer should be. The instruction is to re-emit the same
judgements in the required shape. Both raw responses are preserved -- the
original is logged with its ``parse_error``, the repair as a separate record
flagged ``is_repair`` -- so a repaired call is never mistaken for a clean one.

If the repair also fails, :class:`~pilot01.model.client.ModelOutputError` is
raised. Nothing is invented to fill a gap: a call that produced no usable
observation produces no observation, and the run fails loudly.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from .client import (
    ModelClient,
    ModelClientError,
    ModelMessage,
    ModelOutputError,
    ModelRequest,
)
from .log import ModelCallLog, ModelCallRecord

__all__ = [
    "MAX_FORMAT_REPAIRS",
    "RepairSpec",
    "StructuredCallResult",
    "extract_json_object",
    "parse_structured",
    "record_call",
    "call_structured",
]

MAX_FORMAT_REPAIRS = 1
"""Format-repair attempts permitted per call. One, and only one."""

ModelT = TypeVar("ModelT", bound=BaseModel)

_FENCE = re.compile(r"^\s*```[A-Za-z0-9_+-]*[ \t]*\r?\n(?P<body>.*?)\r?\n?[ \t]*```\s*$", re.DOTALL)


def _balanced_object(text: str, start: int) -> str | None:
    """Return the balanced ``{...}`` block beginning at ``start``, if any.

    String-aware, so a brace inside a JSON string does not close the object.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def extract_json_object(text: str) -> str:
    """Return the first JSON object found in a model response.

    Tolerates a markdown fence and leading or trailing prose, because both are
    common and neither changes what the model said. Raises ``ValueError`` if the
    response contains no JSON object at all.
    """
    candidates: list[str] = []
    fenced = _FENCE.match(text)
    if fenced:
        candidates.append(fenced.group("body").strip())
    candidates.append(text.strip())

    for candidate in candidates:
        try:
            if isinstance(json.loads(candidate), dict):
                return candidate
        except json.JSONDecodeError:
            pass
        start = candidate.find("{")
        while start != -1:
            block = _balanced_object(candidate, start)
            if block is not None:
                try:
                    if isinstance(json.loads(block), dict):
                        return block
                except json.JSONDecodeError:
                    pass
            start = candidate.find("{", start + 1)

    raise ValueError("no JSON object found in the response")


def parse_structured(text: str, output_model: type[ModelT], *, expected_role: str | None = None) -> ModelT:
    """Validate a raw response against an existing output schema.

    Raises :class:`~pilot01.model.client.ModelOutputError` if the response is
    not a JSON object, if it does not validate, or if it relabels the agent's
    own role. No field is ever filled in here.
    """
    try:
        payload = json.loads(extract_json_object(text))
    except ValueError as exc:
        raise ModelOutputError(f"response is not a JSON object: {exc}") from exc
    if not isinstance(payload, dict):  # pragma: no cover - extract guarantees a dict
        raise ModelOutputError(f"response parsed as {type(payload).__name__}, expected an object")

    try:
        output = output_model.model_validate(payload)
    except ValidationError as exc:
        raise ModelOutputError(
            f"response does not match {output_model.__name__}: "
            f"{exc.error_count()} validation error(s); {exc}"
        ) from exc

    if expected_role is not None:
        declared = getattr(output, "role", None)
        if declared != expected_role:
            raise ModelOutputError(
                f"response declares role {declared!r}; this call is the "
                f"{expected_role!r} node and an agent may not relabel itself"
            )
    return output


@dataclass(frozen=True)
class RepairSpec:
    """How to build the one permitted format-repair turn.

    ``build_messages`` receives the previous raw response and the validation
    error. It must not be given anything else -- no view, no policy, no memo --
    and its signature makes that structurally awkward to violate.
    """

    prompt_version: str
    build_messages: Callable[[str, str], tuple[ModelMessage, ...]]


class StructuredCallResult(BaseModel):
    """A successful structured call, with every record it produced."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    output: Any
    """The validated domain object."""
    records: tuple[ModelCallRecord, ...]
    """One record per call made, in order. Length 2 when a repair happened."""

    @property
    def repaired(self) -> bool:
        return len(self.records) > 1

    @property
    def first_attempt_parsed(self) -> bool:
        return not self.repaired

    @property
    def raw_response(self) -> str | None:
        """The first response, unmodified, whether or not it parsed."""
        return self.records[0].raw_response if self.records else None

    @property
    def raw_repair_response(self) -> str | None:
        """The repair response, when one was made."""
        return self.records[-1].raw_response if self.repaired else None


def record_call(
    *,
    index: int,
    request: ModelRequest,
    prompt_version: str,
    attempt: int,
    is_repair: bool,
    repair_reason: str | None = None,
    response: Any = None,
    parsed_output: dict[str, Any] | None = None,
    parse_error: str | None = None,
    error: str | None = None,
    tool_request: dict[str, Any] | None = None,
) -> ModelCallRecord:
    """Build one raw-call record from a request and whatever came back.

    Public because the tool-use loop makes calls that are not structured domain
    calls -- a turn whose response is a tool request parses into no domain
    object at all -- and those turns must be recorded by the same machinery,
    with the same fields, as every other call. Two record builders would drift.
    """
    return ModelCallRecord(
        call_index=index,
        run_id=request.run_id,
        role=request.role,
        invocation=request.invocation,
        prompt_version=prompt_version,
        params=request.params,
        rendered_input=request.rendered_input(),
        attempt=attempt,
        is_repair=is_repair,
        repair_reason=repair_reason,
        requested_at=getattr(response, "requested_at", None),
        responded_at=getattr(response, "responded_at", None),
        latency_seconds=getattr(response, "latency_seconds", None),
        request_id=getattr(response, "request_id", None),
        usage=getattr(response, "usage", None),
        finish_reason=getattr(response, "finish_reason", None),
        raw_response=getattr(response, "text", None),
        parsed_output=parsed_output,
        parse_error=parse_error,
        error=error,
        is_tool_turn=tool_request is not None,
        tool_request=tool_request,
    )


def call_structured(
    client: ModelClient,
    request: ModelRequest,
    output_model: type[ModelT],
    *,
    repair: RepairSpec | None = None,
    call_log: ModelCallLog | None = None,
    expected_role: str | None = None,
    first_index: int = 0,
    first_response: Any = None,
) -> StructuredCallResult:
    """Make one structured model call, repairing the format at most once.

    ``repair`` is required for any repair to be attempted; passing ``None``
    disables the repair path entirely, which is what the tests that pin the
    no-repair behaviour use.

    ``first_index`` offsets the call indices this call records, so a caller that
    has already made calls can keep the log's indices dense and monotonic.
    ``first_response`` lets a caller that has *already* obtained the response
    -- the tool-use loop, which must inspect a response to decide whether it is
    a final answer or a tool request -- hand it in rather than pay for a second
    call. Passing one skips the transport for attempt 0; everything else,
    including the repair path, is unchanged.
    """
    records: list[ModelCallRecord] = []

    def emit(record: ModelCallRecord) -> ModelCallRecord:
        records.append(record)
        if call_log is not None:
            call_log.append(record)
        return record

    # -- attempt 0: the call itself ---------------------------------------
    if first_response is None:
        try:
            response = client.generate(request)
        except ModelClientError as exc:
            emit(
                record_call(
                    index=first_index,
                    request=request,
                    prompt_version=request.prompt_version,
                    attempt=0,
                    is_repair=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            raise
    else:
        response = first_response

    try:
        output = parse_structured(
            response.text, output_model, expected_role=expected_role
        )
    except ModelOutputError as exc:
        first_error = str(exc)
        emit(
            record_call(
                index=first_index,
                request=request,
                prompt_version=request.prompt_version,
                attempt=0,
                is_repair=False,
                response=response,
                parse_error=first_error,
            )
        )
        truncated = getattr(response, "truncated", False)
        if repair is None or truncated:
            if truncated:
                # A repair turn exists to fix a *format*. A response cut off at
                # the output limit has no format to fix: the model was still
                # writing when the budget ran out, and re-asking with the same
                # budget truncates in the same place. Spending the one permitted
                # repair here would buy a second identical failure and record it
                # as a format problem -- which is the misreading this whole
                # branch exists to prevent.
                raise ModelOutputError(
                    f"model stopped at the output limit "
                    f"({request.params.max_output_tokens} tokens, "
                    f"finish_reason='length') before producing a complete "
                    f"answer; this is a truncation and not a format failure, so "
                    f"no repair was attempted. Raise max_output_tokens. "
                    f"Underlying parse error: {first_error}"
                ) from exc
            raise
    else:
        emit(
            record_call(
                index=first_index,
                request=request,
                prompt_version=request.prompt_version,
                attempt=0,
                is_repair=False,
                response=response,
                parsed_output=output.model_dump(mode="json"),
            )
        )
        return StructuredCallResult(output=output, records=tuple(records))

    # -- attempt 1: format repair, and nothing else ------------------------
    repair_version = f"{request.prompt_version}+{repair.prompt_version}"
    repair_request = ModelRequest(
        messages=repair.build_messages(response.text, first_error),
        params=request.params,
        role=request.role,
        prompt_version=repair_version,
        invocation=request.invocation,
        run_id=request.run_id,
    )
    try:
        repaired = client.generate(repair_request)
    except ModelClientError as exc:
        emit(
            record_call(
                index=first_index + 1,
                request=repair_request,
                prompt_version=repair_version,
                attempt=1,
                is_repair=True,
                repair_reason=first_error,
                error=f"{type(exc).__name__}: {exc}",
            )
        )
        raise

    try:
        output = parse_structured(
            repaired.text, output_model, expected_role=expected_role
        )
    except ModelOutputError as exc:
        emit(
            record_call(
                index=first_index + 1,
                request=repair_request,
                prompt_version=repair_version,
                attempt=1,
                is_repair=True,
                repair_reason=first_error,
                response=repaired,
                parse_error=str(exc),
            )
        )
        raise ModelOutputError(
            f"{request.role} produced no usable {output_model.__name__} after "
            f"{MAX_FORMAT_REPAIRS} format-repair attempt. "
            f"original response failed: {first_error}. "
            f"repair response failed: {exc}"
        ) from exc

    emit(
        record_call(
            index=first_index + 1,
            request=repair_request,
            prompt_version=repair_version,
            attempt=1,
            is_repair=True,
            repair_reason=first_error,
            response=repaired,
            parsed_output=output.model_dump(mode="json"),
        )
    )
    return StructuredCallResult(output=output, records=tuple(records))
