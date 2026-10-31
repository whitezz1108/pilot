"""Typed decisions at the boundary between model generation and orchestration."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ...model.client import ModelResponse
from ...model.tool_protocol import ModelToolCall, ToolTransport
from ...model.parsing import extract_json_object
from ..transitions import ProtocolError

__all__ = [
    "AgentDecision",
    "FinalSubmissionDecision",
    "ToolCallDecision",
    "ToolProtocolViolation",
    "decide_agent_response",
]


class ToolProtocolViolation(ProtocolError):
    """The model used an invalid or ambiguous control-channel shape."""


class ToolCallDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["tool_call"] = "tool_call"
    call: ModelToolCall


class FinalSubmissionDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["final_submission"] = "final_submission"
    text: str


AgentDecision = ToolCallDecision | FinalSubmissionDecision


def _reserved_text_envelope(text: str) -> bool:
    """Whether normal content tried to use the retired control envelope."""
    try:
        payload = json.loads(extract_json_object(text))
    except (ValueError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and "tool_request" in payload


def decide_agent_response(
    response: ModelResponse,
    *,
    transport: ToolTransport,
) -> AgentDecision:
    """Resolve one response into exactly one orchestration decision.

    Native mode is intentionally strict: the provider's structured tool-call
    channel and normal assistant content are mutually exclusive, and this
    workflow executes one call at a time.  A legacy ``tool_request`` object in
    normal content is a protocol violation, not a final answer and not a target
    for format repair.
    """
    calls = response.tool_calls
    visible = response.text.strip()
    if transport == "native" and response.finish_reason == "tool_calls" and not calls:
        raise ToolProtocolViolation(
            "provider reported finish_reason='tool_calls' but returned no structured tool call"
        )
    if calls:
        if visible:
            raise ToolProtocolViolation(
                "assistant returned normal content and a tool call in the same turn"
            )
        if len(calls) != 1:
            raise ToolProtocolViolation(
                f"this sequential workflow permits exactly one tool call per turn; got {len(calls)}"
            )
        return ToolCallDecision(call=calls[0])

    if transport == "native" and _reserved_text_envelope(response.text):
        raise ToolProtocolViolation(
            "assistant wrote the retired 'tool_request' text envelope instead of using the native tool-call channel"
        )
    return FinalSubmissionDecision(text=response.text)
