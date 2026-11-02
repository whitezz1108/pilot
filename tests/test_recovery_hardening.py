"""Regression evidence for the failures of the original 96-run batch."""
import io
import json
from types import SimpleNamespace

import pytest
import sample_case
from test_native_tool_protocol import PARAMS, response
from test_model_client import DUMMY_KEY, request_for
from test_reasoning_observation import chunk, client, stream_bytes

from pilot01.model import ModelCallLog, ModelMessage, ModelRequest, ScriptedModelClient
from pilot01.model.client import ModelClientError, ModelOutputError, TokenUsage
from pilot01.model.execution import EndpointSettings
from pilot01.model.native_constraints import schema_without_metadata
from pilot01.model.openai_compat import _urlopen_transport
from pilot01.model.serving_capacity import validate_sglang_capacity
from pilot01.schemas import ManagerOutput, ManagerOutputV2
from pilot01.source import SourceTools
from pilot01.workflow.nodes.tool_loop import run_tool_loop


@pytest.mark.parametrize("constraint", [None, "closed-final-or-tool-v1"])
def test_native_v2_never_reintroduces_unavailable_source_tools(constraint):
    tools = SourceTools(node="manager", library=sample_case.build_source_library())
    tools.begin_invocation(contract_id=sample_case.CONTRACT_ID, available=False)
    model = ScriptedModelClient((sample_case.manager_response_text(),), tool_transport="native")
    log = ModelCallLog()
    def build(messages):
        # Simulate the previously unsafe unconditional definition rebuild.
        return ModelRequest(messages=messages, tools=tools.tool_definitions, tool_choice="auto",
                            params=PARAMS, role="manager", prompt_version="regression")
    result = run_tool_loop(client=model, build_request=build,
        initial_messages=(ModelMessage(role="user", content="No source access."),),
        output_model=ManagerOutput, repair=None, call_log=log, expected_role="manager", tools=tools,
        tool_transport="native", strict_native_state_machine=True, native_output_format=constraint)
    assert result.rounds == 0 and not tools.log.records
    sent = model.requests[0]
    assert sent.tools == () and sent.tool_choice is None and sent.parallel_tool_calls is None
    assert sent.native_output_format == log.records[0].native_output_format == constraint


def test_provider_sse_error_is_visible_redacted_and_keeps_partial_evidence(monkeypatch):
    events = [chunk({"reasoning_content": "partial reasoning"}),
              {"error": {"type": "INTERNAL_SERVER_ERROR", "code": 500,
                         "message": "KV pool exhausted " + DUMMY_KEY}}]
    sent = []
    def open_stream(*args, **kwargs):
        sent.append(1)
        return io.BytesIO(stream_bytes(events, done=False))
    monkeypatch.setattr("urllib.request.urlopen", open_stream)
    with pytest.raises(ModelClientError, match="KV pool exhausted") as exc:
        client(_urlopen_transport, reasoning_stream=True).generate(request_for())
    assert len(sent) == 1 and DUMMY_KEY not in str(exc.value) and "500" in str(exc.value)
    partial = exc.value.partial_response
    assert partial.reasoning_content == "partial reasoning" and partial.usage is None
    assert partial.provider_response["stream_events"][-1]["data"]["error"]["code"] == 500
    assert DUMMY_KEY not in json.dumps(partial.provider_response)


def test_negative_native_index_is_rejected_without_losing_the_original_frame(monkeypatch):
    bad = {"index": -1, "function": {"arguments": "{"}}
    body = stream_bytes([chunk({"reasoning_content": "partial"}), chunk({"tool_calls": [bad]})], done=False)
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: io.BytesIO(body))
    with pytest.raises(ModelClientError, match="invalid native tool-call index: -1") as exc:
        client(_urlopen_transport, reasoning_stream=True, tool_transport="native").generate(request_for())
    partial = exc.value.partial_response
    assert partial.tool_calls == ()
    assert partial.provider_response["stream_events"][-1]["data"]["choices"][0]["delta"]["tool_calls"] == [bad]


def test_length_error_reports_actual_usage_separately_from_requested_maximum():
    result = response(text="incomplete").model_copy(update={"finish_reason": "length",
        "usage": TokenUsage(prompt_tokens=12492, completion_tokens=8016, total_tokens=20508)})
    model = SimpleNamespace(generate=lambda request: result)
    log = ModelCallLog()
    with pytest.raises(ModelOutputError) as exc:
        run_tool_loop(client=model, build_request=lambda messages: ModelRequest(messages=messages,
            params=PARAMS.model_copy(update={"max_output_tokens": 16384}), role="manager", prompt_version="test"),
            initial_messages=(ModelMessage(role="user", content="Assess."),), output_model=ManagerOutput,
            repair=None, call_log=log, expected_role="manager", tools=None, tool_transport="native")
    assert "requested_max_output_tokens=16384" in str(exc.value)
    assert "actual_completion_tokens=8016" in str(exc.value)
    assert len(log.records) == 1 and log.records[0].parse_error == "output_truncated"


def endpoint():
    return EndpointSettings(slot=1, max_in_flight=4, context_window_tokens=49152,
        context_safety_tokens=1024, reasoning_tokenizer_path="frozen-tokenizer.json", capacity_preflight="sglang-v1")


@pytest.mark.parametrize("pool", [20510, 44184])
def test_declared_49k_context_does_not_pass_with_a_smaller_actual_pool(pool):
    info = {"context_length": 49152, "max_running_requests": 4,
            "internal_states": [{"max_total_num_tokens": pool}]}
    with pytest.raises(ModelClientError, match=f"actual KV tokens={pool}"):
        validate_sglang_capacity(info, endpoint())


def test_capacity_record_uses_the_actual_pool_and_excludes_unrelated_server_fields():
    info = {"context_length": 49152, "max_running_requests": 4, "admin_api_key": "private",
            "internal_states": [{"max_total_num_tokens": 65536, "max_req_input_len": 49146}]}
    snapshot = validate_sglang_capacity(info, endpoint())
    assert snapshot["max_total_num_tokens"] == 65536 and snapshot["max_req_input_len"] == 49146
    assert "admin_api_key" not in snapshot


def test_schema_commentary_is_removed_but_property_names_and_values_are_preserved():
    schema = {"type": "object", "description": "research commentary", "properties": {
        "description": {"type": "string", "description": "comment", "enum": ["keep"]},
        "default": {"type": "string", "default": "optional"}}, "required": ["description"]}
    clean = schema_without_metadata(schema)
    assert "description" not in clean
    assert clean["properties"]["description"] == {"type": "string", "enum": ["keep"]}
    assert "default" in clean["properties"] and "default" not in clean["properties"]["default"]
    assert "description" in schema


def test_a_completed_authorized_format_repair_is_not_an_unresolved_failure():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts/audit_failure_recovery.py"
    spec = importlib.util.spec_from_file_location("recovery_audit", path)
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    output = ManagerOutputV2(role="manager", target_clause_status={"Change Of Control": "unknown",
        "Termination For Convenience": "unknown"}, decision="REVIEW", rule_id="POLICY-01",
        evidence_provenance={"upstream_claim_ids": [], "inherited_source_ids": [],
            "opened_paragraph_ids": [], "relied_paragraph_ids": [], "candidate_source_ids": [],
            "verification_basis": "unavailable"}, confidence=0.5, reason_summary="Fixture", uncertainties=[]).model_dump(mode="json")
    first = {"role": "manager", "invocation": 0, "attempt": 0, "parse_error": "missing output fields"}
    repair = {"role": "manager", "invocation": 0, "attempt": 1, "is_repair": True,
              "repair_reason": first["parse_error"], "parsed_output": output, "params": {"max_output_tokens": 4096}}
    execution, runtime = {"manager_output": output}, {"repair_max_output_tokens": 4096}
    assert audit.authorized_format_repair(first, repair, execution, runtime)
    assert not audit.authorized_format_repair(first, {**repair, "repair_reason": "unrelated"}, execution, runtime)
    assert not audit.authorized_format_repair({**first, "finish_reason": "length"}, repair, execution, runtime)
    assert not audit.authorized_format_repair(first, {**repair, "parsed_output": {}}, execution, runtime)
