"""Gate 2: structured parsing and the single format-only repair.

The parsing path is where a real model's output stops being text. Two properties
matter more than any other here:

* **Nothing is invented.** A response that does not validate raises. No field is
  filled in, no enum is coerced, no default is applied on the model's behalf.
* **The repair is format only, and there is at most one.** It is handed the
  model's own previous response and the validation error, and nothing else --
  no policy, no memo, no handoff, no hint of the answer. Both raw responses
  survive: the original is recorded with its ``parse_error``, the repair as a
  separate flagged record.
"""

from __future__ import annotations

import json

import pytest

import sample_case
from pilot01.model import (
    MAX_FORMAT_REPAIRS,
    ModelClientError,
    ModelOutputError,
    ScriptedModelClient,
    call_structured,
)
from pilot01.model.parsing import extract_json_object, parse_structured
from pilot01.prompts import load_repair_prompt
from pilot01.schemas import ClauseStatus, Decision, ManagerOutput
from pilot01.workflow.nodes.llm_agent import build_repair_spec, render_output_fields

VALID = sample_case.manager_response_text()
REPAIR = build_repair_spec(load_repair_prompt(), ManagerOutput)


def params():
    from pilot01.model import ModelParams

    return ModelParams(
        provider="scripted", model_id="test-model-1", temperature=0.0, max_output_tokens=512
    )


def request():
    from pilot01.model import ModelMessage, ModelRequest

    return ModelRequest(
        messages=(ModelMessage(role="user", content="PROMPT"),),
        params=params(),
        role="manager",
        prompt_version="manager_v1",
        run_id="run-0001",
    )


# --------------------------------------------------------------------------
# Extracting a JSON object from whatever the model wrote
# --------------------------------------------------------------------------


def test_extract_accepts_a_bare_object():
    assert extract_json_object(VALID) == VALID.strip()


def test_extract_accepts_a_fenced_object():
    assert json.loads(extract_json_object(sample_case.fenced(VALID)))["decision"] == "ESCALATE"


def test_extract_accepts_an_unlabelled_fence():
    assert json.loads(extract_json_object(f"```\n{VALID}\n```"))["decision"] == "ESCALATE"


def test_extract_accepts_surrounding_prose():
    wrapped = f"Sure! Here is the JSON:\n{VALID}\nLet me know if you need anything else."
    assert json.loads(extract_json_object(wrapped))["decision"] == "ESCALATE"


def test_extract_handles_nested_objects():
    text = '{"a": {"b": {"c": 1}}, "d": 2}'
    assert json.loads(extract_json_object(text)) == {"a": {"b": {"c": 1}}, "d": 2}


def test_extract_is_not_confused_by_braces_inside_strings():
    text = '{"reason_summary": "a } brace and a { brace", "confidence": 0.5}'
    assert json.loads(extract_json_object(text))["confidence"] == 0.5


def test_extract_is_not_confused_by_escaped_quotes():
    text = r'{"reason_summary": "he said \"}\" loudly", "confidence": 0.5}'
    assert json.loads(extract_json_object(text))["confidence"] == 0.5


def test_extract_reports_a_response_with_no_object():
    with pytest.raises(ValueError, match="no JSON object"):
        extract_json_object("I cannot help with that request.")


def test_extract_reports_a_truncated_object():
    with pytest.raises(ValueError, match="no JSON object"):
        extract_json_object('{"decision": "ESCALATE"')


# --------------------------------------------------------------------------
# Validating against the existing schema
# --------------------------------------------------------------------------


def test_a_valid_response_becomes_a_domain_object():
    output = parse_structured(VALID, ManagerOutput, expected_role="manager")
    assert isinstance(output, ManagerOutput)
    assert output.decision is Decision.ESCALATE
    assert (
        output.target_clause_status[sample_case.TARGET_CATEGORY]
        is ClauseStatus.PRESENT
    )


def test_an_unknown_enum_value_is_rejected_not_coerced():
    broken = json.loads(VALID)
    broken["decision"] = "MAYBE"
    with pytest.raises(ModelOutputError, match="does not match ManagerOutput"):
        parse_structured(json.dumps(broken), ManagerOutput)


def test_a_missing_field_is_rejected_not_defaulted():
    broken = json.loads(VALID)
    del broken["confidence"]
    with pytest.raises(ModelOutputError, match="does not match"):
        parse_structured(json.dumps(broken), ManagerOutput)


def test_an_extra_field_is_rejected_not_dropped():
    broken = json.loads(VALID)
    broken["gold_action"] = "ACCEPT"
    with pytest.raises(ModelOutputError, match="does not match"):
        parse_structured(json.dumps(broken), ManagerOutput)


def test_an_out_of_range_confidence_is_rejected():
    broken = json.loads(VALID)
    broken["confidence"] = 4.2
    with pytest.raises(ModelOutputError, match="does not match"):
        parse_structured(json.dumps(broken), ManagerOutput)


def test_a_model_may_not_relabel_its_own_role():
    broken = json.loads(VALID)
    broken["role"] = "compliance"
    with pytest.raises(ModelOutputError, match="may not relabel itself"):
        parse_structured(json.dumps(broken), ManagerOutput, expected_role="manager")


def test_the_role_check_is_skipped_when_no_role_is_expected():
    broken = json.loads(VALID)
    broken["role"] = "compliance"
    assert parse_structured(json.dumps(broken), ManagerOutput).role == "compliance"


def test_prose_with_no_json_is_a_parse_failure():
    with pytest.raises(ModelOutputError, match="not a JSON object"):
        parse_structured("I would rather not answer.", ManagerOutput)


# --------------------------------------------------------------------------
# The repair turn
# --------------------------------------------------------------------------


def test_a_clean_response_needs_no_repair():
    client = ScriptedModelClient((VALID,))
    result = call_structured(client, request(), ManagerOutput, repair=REPAIR)
    assert result.repaired is False
    assert result.first_attempt_parsed is True
    assert client.call_count == 1
    assert len(result.records) == 1


def test_exactly_one_repair_is_permitted():
    assert MAX_FORMAT_REPAIRS == 1


def test_a_malformed_response_triggers_one_repair_call():
    client = ScriptedModelClient(("not json at all", VALID))
    result = call_structured(client, request(), ManagerOutput, repair=REPAIR)
    assert result.repaired is True
    assert client.call_count == 2
    assert isinstance(result.output, ManagerOutput)
    assert result.output.decision is Decision.ESCALATE


def test_both_raw_responses_are_preserved():
    client = ScriptedModelClient(("not json at all", VALID))
    result = call_structured(client, request(), ManagerOutput, repair=REPAIR)
    assert result.raw_response == "not json at all"
    assert result.raw_repair_response == VALID
    assert result.records[0].parse_error is not None
    assert result.records[0].parsed_output is None
    assert result.records[1].parsed_output is not None


def test_the_repair_record_is_flagged_and_explains_itself():
    client = ScriptedModelClient(("not json at all", VALID))
    result = call_structured(client, request(), ManagerOutput, repair=REPAIR)
    first, second = result.records
    assert first.is_repair is False and first.attempt == 0
    assert second.is_repair is True and second.attempt == 1
    assert second.repair_reason == first.parse_error
    # Derived rather than spelled out: the claim is that the repair call is
    # tagged with the prompt it actually used, not that the prompt is v1.
    assert second.prompt_version == f"{request().prompt_version}+{load_repair_prompt().ref}"


def test_the_repair_reuses_the_original_parameters():
    client = ScriptedModelClient(("nope", VALID))
    result = call_structured(client, request(), ManagerOutput, repair=REPAIR)
    assert result.records[1].params == result.records[0].params


def test_the_repair_turn_is_handed_only_the_previous_response_and_the_error():
    """Format only: no view, no policy, no memo, no hint of the answer."""
    client = ScriptedModelClient(("nope", VALID))
    result = call_structured(client, request(), ManagerOutput, repair=REPAIR)
    repair_text = result.records[1].rendered_input

    assert "nope" in repair_text
    assert result.records[0].parse_error in repair_text
    assert "POLICY-01" not in repair_text
    assert "change_of_control" not in repair_text
    assert sample_case.TARGET_CLAIM_ID not in repair_text
    assert "MEMO-001" not in repair_text
    assert "ESCALATE" not in repair_text.replace(
        render_output_fields(ManagerOutput), ""
    )


def test_the_repair_turn_carries_exactly_one_message():
    client = ScriptedModelClient(("nope", VALID))
    result = call_structured(client, request(), ManagerOutput, repair=REPAIR)
    assert len(client.requests[1].messages) == 1
    assert client.requests[1].messages[0].role == "user"


def test_a_failed_repair_surfaces_as_an_output_error():
    client = ScriptedModelClient(("nope", "still not json"))
    with pytest.raises(ModelOutputError, match="no usable ManagerOutput"):
        call_structured(client, request(), ManagerOutput, repair=REPAIR)
    assert client.call_count == 2


def test_a_failed_repair_still_preserves_both_raw_responses():
    log_records = []
    client = ScriptedModelClient(("nope", "still not json"))
    with pytest.raises(ModelOutputError):
        call_structured(
            client,
            request(),
            ManagerOutput,
            repair=REPAIR,
            call_log=_RecordingLog(log_records),
        )
    assert [record.raw_response for record in log_records] == ["nope", "still not json"]
    assert log_records[1].parse_error is not None


def test_no_third_attempt_is_made():
    client = ScriptedModelClient(("a", "b", VALID))
    with pytest.raises(ModelOutputError):
        call_structured(client, request(), ManagerOutput, repair=REPAIR)
    assert client.call_count == 2


def test_repair_can_be_disabled_entirely():
    client = ScriptedModelClient(("nope", VALID))
    with pytest.raises(ModelOutputError, match="not a JSON object"):
        call_structured(client, request(), ManagerOutput, repair=None)
    assert client.call_count == 1


def test_a_transport_failure_on_the_first_call_is_recorded_and_reraised():
    records = []
    client = ScriptedModelClient(("x",))
    client.generate = _boom  # type: ignore[method-assign]
    with pytest.raises(ModelClientError):
        call_structured(
            client, request(), ManagerOutput, repair=REPAIR, call_log=_RecordingLog(records)
        )
    assert records[0].error is not None
    assert records[0].raw_response is None


def test_a_transport_failure_on_the_repair_is_recorded_and_reraised():
    records = []
    client = ScriptedModelClient(("nope",))
    calls = {"n": 0}
    original = client.generate

    def flaky(req):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ModelClientError("provider went away")
        return original(req)

    client.generate = flaky  # type: ignore[method-assign]
    with pytest.raises(ModelClientError, match="provider went away"):
        call_structured(
            client, request(), ManagerOutput, repair=REPAIR, call_log=_RecordingLog(records)
        )
    assert records[1].is_repair is True
    assert "provider went away" in records[1].error


class _RecordingLog:
    """A minimal append-only sink, so these tests need no ModelCallLog import."""

    def __init__(self, sink: list) -> None:
        self._sink = sink

    def append(self, record):
        self._sink.append(record)
        return record


def _boom(req):
    raise ModelClientError("connection refused")
