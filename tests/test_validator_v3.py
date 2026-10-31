import json

import pytest

from clean_v2_fixture import materials, profile, answer, TARGETS, request
from pilot01.source.pagination import PagedSourceTools
from pilot01.source.tools import ToolRequest, replay_ledger
from pilot01.source.verification_v3 import evaluate_verification_v3
from pilot01.schemas import ManagerOutputV2 as ManagerOutput


def setup_tools():
    _, registry, _ = materials()
    tools = PagedSourceTools(node="manager", library=registry.library, target_categories=TARGETS, top_k=2)
    tools.begin_invocation(contract_id="CONTRACT-001", available=True)
    return registry, tools


@pytest.mark.parametrize("protocol", ["1", "2"])
def test_advertised_tool_request_example_is_valid_json(protocol):
    from pilot01.workflow.nodes.tool_loop import render_tool_surface, extract_tool_request
    surface = render_tool_surface(names=("search_contract", "open_source_span"), available=True,
                                  protocol_version=protocol)
    example = next(line for line in surface.splitlines() if line.startswith("{"))
    payload = json.loads(example)
    assert extract_tool_request(example) == payload["tool_request"]


def test_paging_repeat_cursor_isolation_and_replay():
    registry, tools = setup_tools()
    first = tools.search_contract(" administrative ", target_category=TARGETS[0])
    second = tools.search_contract("administrative", target_category=TARGETS[0], cursor=first.payload["next_cursor"])
    assert set(first.result_ids).isdisjoint(second.result_ids)
    assert [r["rank"] for r in second.payload["results"]] == [3, 4]
    repeat = tools.search_contract("administrative", target_category=TARGETS[0])
    assert repeat.result_ids == () and repeat.payload["repeat_page"]
    assert repeat.payload["next_cursor"] == first.payload["next_cursor"]
    replay = replay_ledger(tools.log.records, node="manager", invocation=0)
    assert replay.snapshot() == tools.ledger.snapshot()
    from pilot01.source.tools import SourceToolError
    with pytest.raises(SourceToolError, match="mismatch"):
        tools.search_contract("changed", target_category=TARGETS[0], cursor=first.payload["next_cursor"])
    other = PagedSourceTools(node="compliance", library=registry.library, target_categories=TARGETS)
    other.begin_invocation(contract_id="CONTRACT-001", available=True)
    with pytest.raises(SourceToolError, match="expired"):
        other.search_contract("administrative", target_category=TARGETS[0], cursor=first.payload["next_cursor"])
    cursor = first.payload["next_cursor"]
    tools.begin_invocation(contract_id="CONTRACT-001", available=True)
    with pytest.raises(SourceToolError, match="expired"):
        tools.search_contract("administrative", target_category=TARGETS[0], cursor=cursor)


def test_v3_separate_target_records_and_candidate_narrative():
    registry, tools = setup_tools()
    case = registry.get("CASE-0001")
    doc = registry.document(case.case_id)
    pid, candidate = doc.paragraphs[0].paragraph_id, doc.paragraphs[1].paragraph_id
    tools.search_contract("Change Of Control Termination For Convenience", target_category=TARGETS[0])
    tools.open_source_span(pid)
    tools.ledger.record_upstream([candidate])
    out = ManagerOutput.model_validate_json(answer(pid))
    out.reason_summary = "Candidate " + candidate + " was not opened and not adopted."
    out.evidence_provenance = out.evidence_provenance.model_copy(update={"candidate_source_ids": (candidate,), "inherited_source_ids": (candidate,)})
    def check():
        return evaluate_verification_v3(output=out, policy=case.policy, ledger=tools.ledger, document=doc)
    assert not check().validation_pass
    tools.search_contract("no exact category spelling required", target_category=TARGETS[1])
    assert check().validation_pass  # prose mentions an unopened candidate, not adoption
    out.evidence_provenance = out.evidence_provenance.model_copy(update={"relied_paragraph_ids": (candidate,), "candidate_source_ids": ()})
    assert not check().validation_pass


@pytest.mark.parametrize("extra_request", [False, True])
def test_real_node_sixteen_tools_then_answer_or_seventeenth_refused(extra_request):
    from pilot01.model import ScriptedModelClient, ModelCallLog
    from pilot01.workflow.nodes.manager import make_llm_manager_node
    from pilot01.workflow.state import ExperimentState
    from pilot01.schemas import ErrorCondition
    from pilot01.workflow.transitions import ProtocolError
    registry, tools = setup_tools()
    p = profile()
    case = registry.get("CASE-0001")
    pid = registry.document(case.case_id).paragraphs[0].paragraph_id
    outputs = [request("search_contract", query="control", target_category=TARGETS[0])] * 14
    outputs += [request("search_contract", query="term", target_category=TARGETS[1]), request("open_source_span", paragraph_id=pid)]
    outputs += [request("open_source_span", paragraph_id=pid) if extra_request else answer(pid)]
    client = ScriptedModelClient(outputs)
    log = ModelCallLog()
    node, context = make_llm_manager_node(client=client, params=p.models.for_role("manager"), call_log=log,
        tools=tools, runtime=p.runtime, disclosure_level="D2")
    state = ExperimentState.create(run_id="synthetic", case_id=case.case_id, repetition_id=0, condition_id="D2", conditions=p.conditions,
        error_condition=ErrorCondition.E0, contract_id=case.contract_id, contract_text_hash=case.contract_text_hash,
        target_category=case.target_category, memo=case.memo, gold_target_clause_status=case.gold_target_clause_status,
        policy=case.policy, gold_evidence_offsets=case.gold_evidence_offsets, experiment_version="2")
    view = node.build_input(state, 0)
    if extra_request:
        with pytest.raises(ProtocolError, match="more than 16"):
            node.run(view)
    else:
        assert node.run(view).output.decision.value == "ESCALATE"
    assert len(tools.log.records) == 16
    assert len(log.records) == 17


def test_truncated_tool_shaped_response_is_logged_but_never_dispatched():
    from pilot01.model import ScriptedModelClient, ModelCallLog, ModelRequest, ModelMessage, ModelOutputError
    from pilot01.schemas import ManagerOutputV2
    from pilot01.workflow.nodes.tool_loop import run_tool_loop
    registry, tools = setup_tools()
    class TruncatedClient(ScriptedModelClient):
        def generate(self, req):
            return super().generate(req).model_copy(update={"finish_reason": "length"})
    client = TruncatedClient([request("search_contract", query="control", target_category=TARGETS[0])])
    log = ModelCallLog()
    with pytest.raises(ModelOutputError, match="length"):
        run_tool_loop(client=client, build_request=lambda messages: ModelRequest(messages=messages,
                      params=profile().models.for_role("manager"), role="manager", prompt_version="manager_v4", invocation=0),
                      initial_messages=(ModelMessage(role="user", content="Assess this contract."),),
                      output_model=ManagerOutputV2, repair=None, call_log=log, expected_role="manager", tools=tools)
    assert not tools.log.records
    assert len(log.records) == 1 and log.records[0].finish_reason == "length"


def test_wire_budget_timeout_and_reasoning_usage():
    from pilot01.model import ModelRequest, ModelMessage
    from pilot01.model.openai_compat import OpenAICompatibleClient
    captured = {}
    def transport(req, timeout):
        captured.update(payload=json.loads(req.data), timeout=timeout)
        return json.dumps({"model": "served-fixture-model", "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 15, "total_tokens": 25,
                                     "completion_tokens_details": {"reasoning_tokens": 12}, "prompt_tokens_details": {"cached_tokens": 0}}}).encode()
    client = OpenAICompatibleClient(base_url="https://fixture.invalid/v1", api_key="fixture-credential", transport=transport)
    response = client.generate(ModelRequest(messages=(ModelMessage(role="user", content="Fixture."),),
        params=profile().models.for_role("manager"), role="manager", prompt_version="manager_v4", invocation=0))
    assert captured["payload"]["max_tokens"] == 32768 and captured["timeout"] == 360
    assert response.usage.reasoning_tokens == 12 and response.usage.cached_tokens == 0
    missing = client._extract_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 15, "total_tokens": 25}})
    assert missing.reasoning_tokens is None and missing.cached_tokens is None
