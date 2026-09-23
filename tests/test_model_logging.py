"""Gate 2: the raw model-call log.

The log is the private audit trail of the model calls themselves, and it is a
different thing from the experimental export. These tests pin both halves of
that claim:

* the record captures everything needed to reproduce and audit a call --
  provider, exact model id, parameters, rendered permitted input, timestamps,
  latency, request id, token usage, the raw response, the parsed output or the
  parse failure, and the repair attempt;
* it can carry neither a credential nor hidden gold, and it is not an
  :class:`~pilot01.workflow.export.ExecutionRecord` (whose whole purpose is to
  be safe to publish).
"""

from __future__ import annotations

import json

import pytest

import sample_case
from pilot01.model import (
    ModelCallLog,
    ModelCallRecord,
    ModelClientError,
    ModelMessage,
    ModelParams,
    ModelRequest,
    ScriptedModelClient,
    SecretLeakError,
    TokenUsage,
    call_structured,
)
from pilot01.model.log import SECRET_PATTERNS, assert_no_secrets
from pilot01.model.openai_compat import OpenAICompatibleClient
from pilot01.prompts import load_repair_prompt
from pilot01.schemas import ManagerOutput
from pilot01.workflow.export import ExecutionRecord, ScoringRecord
from pilot01.workflow.nodes.llm_agent import build_repair_spec

VALID = sample_case.manager_response_text()
DUMMY_KEY = "sk-unit-test-abcdefghijklmnopqrstuvwxyz"
REPAIR = build_repair_spec(load_repair_prompt(), ManagerOutput)


def params(**overrides) -> ModelParams:
    base = dict(
        provider="scripted",
        model_id="test-model-1",
        temperature=0.0,
        max_output_tokens=512,
    )
    base.update(overrides)
    return ModelParams(**base)


def request(**overrides) -> ModelRequest:
    base = dict(
        messages=(
            ModelMessage(role="system", content="SYSTEM PROMPT"),
            ModelMessage(role="user", content="USER PROMPT"),
        ),
        params=params(),
        role="manager",
        prompt_version="manager_v1",
        invocation=0,
        run_id="run-0001",
    )
    base.update(overrides)
    return ModelRequest(**base)


def a_record(**overrides) -> ModelCallRecord:
    base = dict(
        call_index=0,
        run_id="run-0001",
        role="manager",
        invocation=0,
        prompt_version="manager_v1",
        params=params(),
        rendered_input="### user\nPROMPT",
        attempt=0,
        raw_response=VALID,
    )
    base.update(overrides)
    return ModelCallRecord(**base)


# --------------------------------------------------------------------------
# What a record captures
# --------------------------------------------------------------------------


def test_a_clean_call_produces_one_complete_record():
    log = ModelCallLog()
    client = ScriptedModelClient(
        (VALID,), usage=TokenUsage(prompt_tokens=5, completion_tokens=6, total_tokens=11)
    )
    call_structured(client, request(), ManagerOutput, repair=REPAIR, call_log=log)

    assert len(log) == 1
    record = log.records[0]
    assert record.call_index == 0
    assert record.run_id == "run-0001"
    assert record.role == "manager"
    assert record.invocation == 0
    assert record.prompt_version == "manager_v1"
    assert record.params == params()
    assert record.rendered_input.startswith("### system")
    assert "USER PROMPT" in record.rendered_input
    assert record.requested_at is not None
    assert record.responded_at is not None
    assert record.latency_seconds is not None
    assert record.request_id == "scripted-0"
    assert record.usage == TokenUsage(prompt_tokens=5, completion_tokens=6, total_tokens=11)
    assert record.raw_response == VALID
    assert record.parsed_output["decision"] == "ESCALATE"
    assert record.parse_error is None
    assert record.is_repair is False


def test_a_repaired_call_produces_two_records_in_order():
    log = ModelCallLog()
    client = ScriptedModelClient(("not json", VALID))
    call_structured(client, request(), ManagerOutput, repair=REPAIR, call_log=log)

    assert [record.call_index for record in log.records] == [0, 1]
    assert [record.attempt for record in log.records] == [0, 1]
    assert log.records[0].parse_error is not None
    assert log.records[0].raw_response == "not json"
    assert log.records[1].is_repair is True
    assert log.records[1].raw_response == VALID
    assert log.repairs() == (log.records[1],)


def test_the_log_can_be_filtered_by_role():
    log = ModelCallLog()
    client = ScriptedModelClient((VALID, VALID))
    call_structured(client, request(), ManagerOutput, repair=REPAIR, call_log=log)
    call_structured(
        client, request(role="compliance"), ManagerOutput, repair=REPAIR, call_log=log
    )
    assert len(log.of_role("manager")) == 1
    assert len(log.of_role("compliance")) == 1


def test_a_transport_failure_is_recorded_with_its_error():
    log = ModelCallLog()

    class Failing:
        def generate(self, req):
            raise ModelClientError("connection refused")

    with pytest.raises(ModelClientError):
        call_structured(Failing(), request(), ManagerOutput, repair=REPAIR, call_log=log)
    assert len(log) == 1
    assert log.records[0].error == "ModelClientError: connection refused"
    assert log.records[0].raw_response is None


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def test_the_log_appends_jsonl_to_disk(tmp_path):
    path = tmp_path / "calls.jsonl"
    log = ModelCallLog(path)
    client = ScriptedModelClient(("not json", VALID))
    call_structured(client, request(), ManagerOutput, repair=REPAIR, call_log=log)

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    payloads = [json.loads(line) for line in lines]
    assert [payload["attempt"] for payload in payloads] == [0, 1]
    assert payloads[0]["raw_response"] == "not json"


def test_a_run_scoped_log_file_is_created_fresh(tmp_path):
    path = tmp_path / "calls.jsonl"
    ModelCallLog(path).append(a_record())
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 1
    ModelCallLog(path)
    assert path.read_text(encoding="utf-8") == ""


def test_the_log_creates_parent_directories(tmp_path):
    path = tmp_path / "runs" / "run-0001" / "model_calls.jsonl"
    ModelCallLog(path).append(a_record())
    assert path.is_file()


def test_the_log_serializes_to_jsonl_without_disk(tmp_path):
    log = ModelCallLog()
    log.append(a_record())
    assert json.loads(log.to_jsonl().strip())["role"] == "manager"


# --------------------------------------------------------------------------
# No credentials, ever
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "sk-abcdefghijklmnopqrstuvwxyz012345",
        "sk-ant-abcdefghijklmnopqrstuvwxyz012345",
        "Bearer abcdefghijklmnopqrstuvwxyz",
        "api_key: abcdefghijklmnop",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    ],
)
def test_a_record_containing_a_credential_is_refused(secret):
    with pytest.raises(SecretLeakError):
        ModelCallLog().append(a_record(raw_response=f"the key is {secret}"))


def test_a_credential_in_the_rendered_input_is_refused_too():
    with pytest.raises(SecretLeakError):
        ModelCallLog().append(a_record(rendered_input=f"Authorization: Bearer {DUMMY_KEY}"))


def test_the_refusal_explains_itself():
    with pytest.raises(SecretLeakError, match="never enter"):
        assert_no_secrets(a_record(raw_response=DUMMY_KEY))


def test_a_clean_record_passes_the_secret_scan():
    assert_no_secrets(a_record())


def test_there_is_a_pattern_for_each_common_credential_shape():
    assert len(SECRET_PATTERNS) >= 5


def test_a_live_client_with_a_key_logs_no_key():
    """The end-to-end version: a keyed client's record carries no key."""
    log = ModelCallLog()
    client = OpenAICompatibleClient(
        base_url="https://example.invalid/v1",
        api_key=DUMMY_KEY,
        transport=lambda *_: json.dumps(
            {
                "model": "m",
                "choices": [{"message": {"content": VALID}}],
            }
        ).encode(),
    )
    call_structured(client, request(), ManagerOutput, repair=REPAIR, call_log=log)

    assert len(log) == 1
    assert DUMMY_KEY not in log.to_jsonl()
    assert "Authorization" not in log.to_jsonl()
    assert "authorization" not in log.to_jsonl().lower()


def test_a_record_has_no_field_a_credential_could_live_in():
    fields = set(ModelCallRecord.model_fields)
    for name in ("api_key", "authorization", "headers", "token", "secret"):
        assert name not in fields


# --------------------------------------------------------------------------
# No hidden gold, and a different thing from the experimental export
# --------------------------------------------------------------------------


def test_a_record_has_no_gold_field():
    for name in ("gold", "gold_action", "gold_clause_status", "hidden", "omission"):
        assert name not in ModelCallRecord.model_fields


def test_a_record_rejects_an_unknown_field():
    with pytest.raises(Exception):
        ModelCallRecord(
            call_index=0,
            role="manager",
            invocation=0,
            prompt_version="manager_v1",
            params=params(),
            rendered_input="x",
            gold_action="ACCEPT",
        )


def test_the_raw_call_record_is_not_an_execution_record():
    assert not issubclass(ModelCallRecord, ExecutionRecord)
    assert not issubclass(ExecutionRecord, ModelCallRecord)

    # The two share exactly one field, the run identifier. Everything that makes
    # each one what it is is absent from the other, so neither can be passed off
    # as the other by accident or by a convenient refactor.
    shared = set(ModelCallRecord.model_fields) & set(ExecutionRecord.model_fields)
    assert shared == {"run_id"}

    for name in ("raw_response", "rendered_input", "prompt_version", "params", "usage"):
        assert name not in ExecutionRecord.model_fields
    for name in ("case_id", "condition_id", "manager_output", "compliance_output", "events"):
        assert name not in ModelCallRecord.model_fields


def test_the_execution_record_still_refuses_gold_and_the_scoring_record_still_carries_it(
    make_state, llm_case, workflow, conditions
):
    """Gate 1.5's separation survives the arrival of the real agents."""
    outcome = llm_case.runner(workflow, conditions).run(make_state())

    execution = outcome.to_execution_record()
    assert "gold" not in json.dumps(execution.model_dump(mode="json")).lower()
    assert "987654321" not in execution.model_dump_json()

    scoring = ScoringRecord(
        run_id=outcome.run_id,
        case_id=outcome.state.case_id,
        repetition_id=outcome.state.repetition_id,
        condition_id=outcome.state.condition_id,
        error_condition=outcome.state.error_condition,
        gold=outcome.state.hidden.gold,
    )
    assert scoring.gold.gold_action.value == "ESCALATE"
    assert scoring.gold.gold_evidence_offsets == (987654321, 987654322)
    assert not isinstance(scoring, ExecutionRecord)


def test_the_raw_call_log_is_not_written_into_the_execution_record(
    make_state, llm_case, workflow, conditions
):
    """Model calls are audited separately; the public record stays public."""
    outcome = llm_case.runner(workflow, conditions).run(make_state())
    execution = outcome.to_execution_record()
    for name in ("raw_response", "rendered_input", "prompt_version", "params"):
        assert name not in ExecutionRecord.model_fields
    assert llm_case.log.records  # the calls were recorded, just not there
    assert "raw_response" not in execution.model_dump_json()
