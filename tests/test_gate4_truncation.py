"""Gate 4 §12: a truncated response is not a format failure.

Found by the Gate-4 smoke, and pinned here so it stays found.

The provider is a reasoning model: it spends output tokens thinking before it
emits any content, and ``max_output_tokens`` caps both together. With a budget
smaller than its reasoning needs, it returns ``finish_reason="length"`` and an
*empty* content string. Measured against the live endpoint at the time of
writing: ``budget=32`` gave ``finish_reason='length', content=''`` on 4 of 10
calls, every empty one having ``completion_tokens == 32`` exactly; ``budget=512``
and ``budget=1024`` gave ``finish_reason='stop'`` and valid JSON.

The bug was not the truncation. It was that the adapter dropped
``finish_reason``, so a call cut off at the budget reached the workflow as an
empty string -- which parses as a format failure, triggers the one permitted
format repair, and spends a second paid call to truncate again. That is the
"format-repair storm" §12 tells the operator to watch for, and it would have
been recorded as a model that cannot follow a schema.

So: the reason is recorded, and a truncation raises as a truncation without
burning the repair. What this file does *not* do is decide what the experiment
should do about one -- that is the operator's call, and §16's.
"""

from __future__ import annotations

import json

import pytest

from pilot01.model import (
    ModelClientError,
    ModelMessage,
    ModelParams,
    ModelOutputError,
    ModelRequest,
)
from pilot01.model.log import ModelCallLog
from pilot01.model.openai_compat import OpenAICompatibleClient
from pilot01.model.parsing import call_structured
from pilot01.schemas import ManagerOutput

DUMMY_KEY = "unit-test-key-not-a-real-credential"
BASE_URL = "https://example.invalid/v1"


def _params(max_output_tokens: int = 32) -> ModelParams:
    return ModelParams(
        provider="openai-compatible",
        model_id="test-reasoning-model",
        temperature=0.0,
        max_output_tokens=max_output_tokens,
    )


def _envelope(*, content: str, finish_reason: str | None) -> bytes:
    choice: dict[str, object] = {"message": {"role": "assistant", "content": content}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return json.dumps(
        {
            "id": "req-1",
            "model": "test-reasoning-model",
            "choices": [choice],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 32,
                "total_tokens": 132,
            },
        }
    ).encode("utf-8")


def _client(body: bytes) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        base_url=BASE_URL, api_key=DUMMY_KEY, transport=lambda request, timeout: body
    )


def _request() -> ModelRequest:
    return ModelRequest(
        messages=(
            ModelMessage(role="system", content="SYSTEM"),
            ModelMessage(role="user", content="USER"),
        ),
        params=_params(),
        role="manager",
        prompt_version="manager_v1",
    )


# ---------------------------------------------------------------------------
# the adapter records what the provider said
# ---------------------------------------------------------------------------


def test_the_adapter_reports_the_finish_reason():
    response = _client(_envelope(content="{}", finish_reason="stop")).generate(_request())
    assert response.finish_reason == "stop"
    assert response.truncated is False


def test_the_adapter_reports_a_length_stop_as_a_truncation():
    response = _client(_envelope(content="", finish_reason="length")).generate(_request())
    assert response.finish_reason == "length"
    assert response.truncated is True


def test_a_provider_that_reports_no_reason_is_not_read_as_truncated():
    """``None`` is "unknown", and unknown must not become a claim either way."""
    response = _client(_envelope(content="{}", finish_reason=None)).generate(_request())
    assert response.finish_reason is None
    assert response.truncated is False


def test_the_finish_reason_reaches_the_raw_call_log():
    """The record is where an operator looks to tell the two failures apart."""
    log = ModelCallLog()
    with pytest.raises(ModelOutputError):
        call_structured(
            _client(_envelope(content="", finish_reason="length")),
            _request(),
            ManagerOutput,
            call_log=log,
        )
    assert log.records[0].finish_reason == "length"
    assert log.records[0].raw_response == ""


# ---------------------------------------------------------------------------
# and does not spend the repair on it
# ---------------------------------------------------------------------------


def test_a_truncated_response_does_not_burn_the_one_permitted_repair():
    """The repair cannot help, and a second identical failure is not evidence.

    One call, not two: the repair turn would re-send the same request with the
    same budget, truncate in the same place, and the log would show a model that
    failed the schema twice rather than a budget that was too small once.
    """
    calls: list[ModelRequest] = []

    def transport(request, timeout):
        calls.append(request)
        return _envelope(content="", finish_reason="length")

    client = OpenAICompatibleClient(
        base_url=BASE_URL, api_key=DUMMY_KEY, transport=transport
    )
    log = ModelCallLog()
    with pytest.raises(ModelOutputError, match="output limit"):
        call_structured(
            client,
            _request(),
            ManagerOutput,
            repair=_repair_spec(),
            call_log=log,
        )
    assert len(calls) == 1
    assert log.repairs() == ()


def test_a_truncation_says_so_instead_of_reporting_a_schema_failure():
    """The message an operator reads has to name the right cause."""
    with pytest.raises(ModelOutputError) as excinfo:
        call_structured(
            _client(_envelope(content="", finish_reason="length")),
            _request(),
            ManagerOutput,
            repair=_repair_spec(),
        )
    message = str(excinfo.value)
    assert "output limit" in message
    assert "finish_reason='length'" in message
    assert "32" in message  # the budget that was too small
    assert "max_output_tokens" in message


def test_a_genuinely_malformed_response_still_gets_its_repair():
    """The new branch must not swallow the case the repair exists for."""
    calls: list[ModelRequest] = []

    def transport(request, timeout):
        calls.append(request)
        return _envelope(content="not json at all", finish_reason="stop")

    client = OpenAICompatibleClient(
        base_url=BASE_URL, api_key=DUMMY_KEY, transport=transport
    )
    with pytest.raises(ModelOutputError, match="format-repair"):
        call_structured(
            client,
            _request(),
            ManagerOutput,
            repair=_repair_spec(),
        )
    assert len(calls) == 2  # the call, then its repair


def test_a_transport_failure_is_still_a_transport_failure():
    def transport(request, timeout):
        raise ModelClientError("endpoint refused")

    client = OpenAICompatibleClient(
        base_url=BASE_URL, api_key=DUMMY_KEY, transport=transport
    )
    with pytest.raises(ModelClientError, match="endpoint refused"):
        call_structured(client, _request(), ManagerOutput, repair=_repair_spec())


def _repair_spec():
    """The real repair spec, built the way the nodes build it."""
    from pilot01.prompts import load_repair_prompt
    from pilot01.workflow.nodes.llm_agent import build_repair_spec

    return build_repair_spec(load_repair_prompt(), ManagerOutput)
