"""Exercise runtime-v4 provider-visible bytes with an offline HTTP transport.

Responses are prescribed fixtures. The actual adapter, role builders, source
tools, compacting history and repair machinery produce every captured request.
"""
import copy
import json

import pytest

from clean_v2_fixture import ROOT, TARGETS, answer, materials
from pilot01.model import ModelCallLog
from pilot01.model.openai_compat import OpenAICompatibleClient
from pilot01.runtime import ExperimentProfile
from pilot01.schemas import ClauseStatus, ErrorCondition
from pilot01.source.tools import ToolCallLog
from pilot01.source.verification import VerificationLog
from pilot01.workflow.nodes import build_llm_registry
from pilot01.workflow.nodes.disclosure import RECOVERY_OBJECTIVE
from pilot01.workflow.runner import WorkflowRunner
from pilot01.workflow.state import ExperimentState


def wire_response(content=None, *, tool=None, arguments=None, call_id=None):
    message = {"role": "assistant", "content": content}
    if tool:
        message["tool_calls"] = [{"id": call_id, "type": "function", "function": {
            "name": tool, "arguments": json.dumps(arguments)}}]
    return json.dumps({"id": "offline-response", "model": "offline-fixture",
                       "choices": [{"message": message,
                                    "finish_reason": "tool_calls" if tool else "stop"}]}).encode()


def execute_wire(condition_id, error_condition, *, changed_gold=False):
    profile = ExperimentProfile(ROOT / "config/experiment_recovery_v4.yaml")
    _, registry, _ = materials()
    case = registry.get("CASE-0001")
    pid = registry.document(case.case_id).paragraphs[0].paragraph_id
    prescribed = (
        wire_response(tool="search_contract", arguments={"query": "control", "target_category": TARGETS[0]}, call_id="s1"),
        wire_response(tool="open_source_span", arguments={"paragraph_id": pid}, call_id="o1"),
        wire_response(tool="open_source_span", arguments={"paragraph_id": pid}, call_id="o2"),
        wire_response(tool="search_contract", arguments={"query": "control", "target_category": TARGETS[0]}, call_id="s2"),
        wire_response('{"decision":"ESCALATE"}'),
        wire_response(answer(pid)),
    )
    captures = {role: [] for role in ("manager", "compliance")}

    def client_for(role):
        def transport(request, timeout):
            captures[role].append(json.loads(request.data))
            return prescribed[len(captures[role]) - 1]

        client = OpenAICompatibleClient(base_url="https://offline.invalid/v1",
                                        api_key="offline-placeholder", tool_transport="native",
                                        transport=transport)
        # Deterministic test estimator forces exposed search history through the
        # production compactor. No tokenizer/network dependency is introduced.
        client.estimate_request_tokens = lambda request: 30000
        return client

    gold = case.gold_target_clause_status
    offsets = case.gold_evidence_offsets
    if changed_gold:
        gold = {category: ClauseStatus.ABSENT for category in case.policy.target_clause_categories}
        offsets = (5, 6)
    state = ExperimentState.create(
        run_id="PRIVATE-RUN-" + condition_id, case_id=case.case_id, repetition_id=0,
        condition_id=condition_id, conditions=profile.conditions, error_condition=error_condition,
        contract_id=case.contract_id, contract_text_hash=case.contract_text_hash,
        target_category=case.target_category, memo=registry.memo(case.case_id, error_condition),
        gold_target_clause_status=gold, policy=case.policy, gold_evidence_offsets=offsets,
        omission=registry.omission_record(case.case_id) if error_condition == ErrorCondition.E1 else None,
        experiment_version="4", workflow_version=profile.workflow.workflow_version)
    calls, tools, checks = ModelCallLog(), ToolCallLog(), VerificationLog()
    nodes = build_llm_registry(repository=registry.repository, models=profile.models,
                               manager_client=client_for("manager"), compliance_client=client_for("compliance"),
                               call_log=calls, source=registry.library, tool_log=tools,
                               verification_log=checks, runtime=profile.runtime, disclosure_level="D0",
                               target_categories=case.policy.target_clause_categories)
    outcome = WorkflowRunner(workflow=profile.workflow, conditions=profile.conditions, registry=nodes).run(state)
    assert outcome.status.value == "completed"
    return captures, calls.records, tools.records


@pytest.mark.parametrize("error_condition", [ErrorCondition.E0, ErrorCondition.E1])
def test_every_http_turn_is_invariant_to_hidden_gold(error_condition):
    original, calls, tools = execute_wire("A1V2D0", error_condition)
    changed, _, _ = execute_wire("A1V2D0", error_condition, changed_gold=True)
    assert original == changed
    assert len(calls) == 12 and sum(call.is_repair for call in calls) == 2
    for role in ("manager", "compliance"):
        role_tools = [call for call in tools if call.node == role]
        assert [call.ok for call in role_tools] == [True, True, False, False]
        payloads = original[role]
        assert len(payloads) == 6
        combined = json.dumps(payloads)
        assert "RUNTIME RETRIEVAL LEDGER" in combined
        assert "duplicate" in combined
        assert any(message["role"] == "tool" for body in payloads for message in body["messages"])
        for forbidden in ("PRIVATE-RUN", "A1V2D0", "recovery_required", "verification_required",
                          "gold_action", "gold_target_clause_status", "gold_evidence_offsets",
                          "omission_target_claim_id", "PILOT-POS", "PILOT-SENT"):
            assert forbidden not in combined
        # Format-only repair has no upstream memo, policy values or source text.
        repair = json.dumps(payloads[-1])
        assert "SOURCE CHECKING TASK" not in repair
        assert "CASE-0001" not in repair
        assert "POLICY-01" not in repair
        assert all(category not in repair for category in TARGETS)
        assert "Upon a change of control" not in repair
        assert "upstream material" not in repair


@pytest.mark.parametrize("error_condition", [ErrorCondition.E0, ErrorCondition.E1])
def test_v1_v2_http_payloads_differ_only_by_recovery_paragraph(error_condition):
    v1, _, _ = execute_wire("A1V1D0", error_condition)
    v2, _, _ = execute_wire("A1V2D0", error_condition)
    normalized = copy.deepcopy(v2)
    for role, payloads in normalized.items():
        for index, body in enumerate(payloads):
            count = sum(message.get("content", "").count(RECOVERY_OBJECTIVE)
                        for message in body["messages"] if isinstance(message.get("content"), str))
            assert count == (0 if index == len(payloads) - 1 else 1)
            for message in body["messages"]:
                if isinstance(message.get("content"), str):
                    message["content"] = message["content"].replace("\n\n" + RECOVERY_OBJECTIVE, "")
    assert normalized == v1
