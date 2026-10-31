from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import sample_case
from clean_v2_fixture import TARGETS, materials
from pilot01.model import (
    ModelCallLog,
    ModelClientError,
    ModelMessage,
    ModelParams,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ScriptedModelClient,
)
from pilot01.model.openai_compat import OpenAICompatibleClient
from pilot01.schemas import ManagerOutput
from pilot01.source import SourceToolError, SourceTools
from pilot01.source.pagination import NEXT_SEARCH_PAGE, PagedSourceTools
from pilot01.workflow.nodes.agent_protocol import (
    ToolCallDecision,
    ToolProtocolViolation,
    decide_agent_response,
)
from pilot01.workflow.nodes.tool_loop import ToolLoopError, run_tool_loop


PARAMS = ModelParams(
    provider="scripted",
    model_id="scripted-native",
    temperature=0.0,
    max_output_tokens=512,
)

SAMPLING_PARAMS = ModelParams(
    provider="openai-compatible",
    model_id="scripted-native",
    temperature=1.0,
    max_output_tokens=32768,
    top_p=0.95,
    top_k=20,
    min_p=0.0,
    presence_penalty=0.0,
    repetition_penalty=1.0,
    seed=202,
    timeout_seconds=360.0,
    reasoning_effort="xhigh",
)


def native_call(name="search_contract", arguments=None, call_id="call-search-1"):
    return ModelToolCall(
        call_id=call_id,
        name=name,
        arguments=arguments or {"query": "change of control"},
    )


def response(*, text="", calls=()):
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    return ModelResponse(
        text=text,
        tool_calls=calls,
        provider="fixture",
        model_id="fixture",
        requested_at=now,
        responded_at=now,
    )


def test_native_wire_carries_closed_tool_schemas_without_json_response_mode():
    tools = SourceTools(node="manager", library=sample_case.build_source_library())
    request = ModelRequest(
        messages=(ModelMessage(role="user", content="Assess."),),
        tools=tools.tool_definitions,
        tool_choice="auto",
        params=PARAMS,
        role="manager",
        prompt_version="manager-native-v1",
    )
    client = OpenAICompatibleClient(
        base_url="https://offline.invalid/v1",
        api_key="offline-placeholder",
        tool_transport="native",
        transport=lambda *_: b"",
    )
    payload = json.loads(client.build_request(request).data)
    assert payload["tool_choice"] == "auto"
    assert payload["parallel_tool_calls"] is True
    assert "response_format" not in payload and "ebnf" not in payload
    assert [tool["function"]["name"] for tool in payload["tools"]] == [
        "search_contract",
        "open_source_span",
    ]
    assert all(tool["function"]["strict"] is True for tool in payload["tools"])
    assert all(
        tool["function"]["parameters"]["additionalProperties"] is False
        for tool in payload["tools"]
    )


def test_full_sampling_policy_is_sent_and_fingerprinted():
    request = ModelRequest(
        messages=(ModelMessage(role="user", content="Assess."),),
        params=SAMPLING_PARAMS,
        role="manager",
        prompt_version="sampling-native-v1",
    )
    client = OpenAICompatibleClient(
        base_url="https://offline.invalid/v1",
        api_key="offline-placeholder",
        tool_transport="native",
        transport=lambda *_: b"",
    )
    payload = json.loads(client.build_request(request).data)
    assert {
        key: payload[key]
        for key in (
            "temperature",
            "top_p",
            "top_k",
            "min_p",
            "presence_penalty",
            "repetition_penalty",
            "seed",
            "max_tokens",
            "reasoning_effort",
        )
    } == {
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repetition_penalty": 1.0,
        "seed": 202,
        "max_tokens": 32768,
        "reasoning_effort": "xhigh",
    }
    fingerprint = SAMPLING_PARAMS.fingerprint()
    for fragment in (
        "top_k=20",
        "min_p=0",
        "presence_penalty=0",
        "repetition_penalty=1",
        "seed=202",
    ):
        assert fragment in fingerprint


def test_native_tools_refuse_legacy_grammar_constraint(monkeypatch):
    tools = SourceTools(node="manager", library=sample_case.build_source_library())
    request = ModelRequest(
        messages=(ModelMessage(role="user", content="Assess."),),
        tools=tools.tool_definitions,
        tool_choice="auto",
        params=PARAMS,
        role="manager",
        prompt_version="manager-native-v1",
    )
    client = OpenAICompatibleClient(
        base_url="https://offline.invalid/v1",
        api_key="offline-placeholder",
        tool_transport="native",
        transport=lambda *_: b"",
    )
    monkeypatch.setenv("PILOT01_STRICT_TOOL_PROTOCOL", "1")
    with pytest.raises(ModelClientError, match="cannot be combined"):
        client.build_request(request)


def test_live_adapter_normalizes_openai_tool_calls():
    body = json.dumps(
        {
            "id": "chatcmpl-tool",
            "model": "served",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-123",
                                "type": "function",
                                "function": {
                                    "name": "search_contract",
                                    "arguments": '{"query":"control"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
    ).encode()
    client = OpenAICompatibleClient(
        base_url="https://offline.invalid/v1",
        api_key="offline-placeholder",
        tool_transport="native",
        transport=lambda *_: body,
    )
    result = client.generate(
        ModelRequest(
            messages=(ModelMessage(role="user", content="Assess."),),
            params=PARAMS,
            role="manager",
            prompt_version="native-v1",
        )
    )
    assert result.text == ""
    assert result.tool_calls == (
        ModelToolCall(
            call_id="call-123",
            name="search_contract",
            arguments={"query": "control"},
            raw_arguments='{"query":"control"}',
        ),
    )


def test_live_adapter_generates_and_marks_missing_provider_call_id():
    body = json.dumps(
        {
            "id": "chatcmpl-missing-call-id",
            "model": "served",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "search_contract",
                                    "arguments": '{"query":"control"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
    ).encode()
    client = OpenAICompatibleClient(
        base_url="https://offline.invalid/v1",
        api_key="offline-placeholder",
        tool_transport="native",
        transport=lambda *_: body,
    )
    request = ModelRequest(
        messages=(ModelMessage(role="user", content="Assess."),),
        params=PARAMS,
        role="manager",
        prompt_version="native-v1",
    )
    result = client.generate(request)
    assert result.tool_calls[0].call_id == f"call_adapter_{request.model_call_id}_0"
    assert result.tool_calls[0].call_id_origin == "adapter_generated"
    assert result.provider_response["choices"][0]["message"]["tool_calls"][0].get("id") is None


def test_malformed_native_call_keeps_raw_provider_envelope_on_error():
    body = json.dumps(
        {
            "id": "chatcmpl-malformed-tool",
            "model": "served",
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-no-name",
                                "type": "function",
                                "function": {"arguments": "{}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
    ).encode()
    client = OpenAICompatibleClient(
        base_url="https://offline.invalid/v1",
        api_key="offline-placeholder",
        tool_transport="native",
        transport=lambda *_: body,
    )
    with pytest.raises(ModelClientError, match="has no function name") as exc:
        client.generate(
            ModelRequest(
                messages=(ModelMessage(role="user", content="Assess."),),
                params=PARAMS,
                role="manager",
                prompt_version="native-v1",
            )
        )
    raw = exc.value.partial_response.provider_response
    assert raw["choices"][0]["message"]["tool_calls"][0]["id"] == "call-no-name"


def test_mixed_content_and_tool_call_is_a_protocol_violation():
    with pytest.raises(ToolProtocolViolation, match="normal content and a tool call"):
        decide_agent_response(
            response(text="also answering", calls=(native_call(),)),
            transport="native",
        )


def test_multiple_native_calls_are_one_typed_tool_decision():
    calls = (native_call(call_id="one"), native_call(call_id="two"))
    decision = decide_agent_response(response(calls=calls), transport="native")
    assert isinstance(decision, ToolCallDecision)
    assert decision.calls == calls


def test_finish_reason_cannot_claim_a_missing_tool_call():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    missing = ModelResponse(
        text="",
        provider="fixture",
        model_id="fixture",
        requested_at=now,
        responded_at=now,
        finish_reason="tool_calls",
    )
    with pytest.raises(ToolProtocolViolation, match="returned no structured tool call"):
        decide_agent_response(missing, transport="native")


def test_native_loop_links_assistant_call_tool_result_and_tool_log():
    tools = SourceTools(
        node="manager",
        library=sample_case.build_source_library(),
    )
    tools.begin_invocation(
        contract_id=sample_case.CONTRACT_ID,
        available=True,
        run_id="run-native",
    )
    call = native_call()
    client = ScriptedModelClient(
        (call, sample_case.manager_response_text()),
        tool_transport="native",
    )
    log = ModelCallLog()

    def build_request(messages):
        return ModelRequest(
            messages=messages,
            tools=tools.tool_definitions,
            tool_choice="auto",
            params=PARAMS,
            role="manager",
            prompt_version="manager-native-v1",
            run_id="run-native",
        )

    result = run_tool_loop(
        client=client,
        build_request=build_request,
        initial_messages=(ModelMessage(role="user", content="Assess."),),
        output_model=ManagerOutput,
        repair=None,
        call_log=log,
        expected_role="manager",
        tools=tools,
        tool_transport="native",
    )
    assert result.rounds == 1
    follow_up = client.requests[1].messages
    assert follow_up[-2].role == "assistant"
    assert follow_up[-2].tool_calls == (call,)
    assert follow_up[-1].role == "tool"
    assert follow_up[-1].tool_call_id == call.call_id
    single_payload = json.loads(follow_up[-1].content)
    assert single_payload["status"] == "ok"
    assert single_payload["tool_budget"] == {
        "used": 1,
        "limit": 16,
        "remaining": 15,
    }
    assert log.records[0].tool_call_id == call.call_id
    assert tools.log.records[0].tool_call_id == call.call_id


def test_native_loop_executes_multiple_read_only_calls_in_provider_order():
    tools = SourceTools(node="manager", library=sample_case.build_source_library())
    tools.begin_invocation(
        contract_id=sample_case.CONTRACT_ID,
        available=True,
        run_id="run-native-multi",
    )
    calls = (
        native_call(
            call_id="call-control",
            arguments={"query": "change of control"},
        ),
        native_call(
            call_id="call-termination",
            arguments={"query": "termination"},
        ),
    )
    client = ScriptedModelClient(
        (calls, sample_case.manager_response_text()),
        tool_transport="native",
    )
    log = ModelCallLog()

    def build_request(messages):
        return ModelRequest(
            messages=messages,
            tools=tools.tool_definitions,
            tool_choice="auto",
            params=PARAMS,
            role="manager",
            prompt_version="manager-native-v1",
            run_id="run-native-multi",
        )

    result = run_tool_loop(
        client=client,
        build_request=build_request,
        initial_messages=(ModelMessage(role="user", content="Assess."),),
        output_model=ManagerOutput,
        repair=None,
        call_log=log,
        expected_role="manager",
        tools=tools,
        tool_transport="native",
    )
    assert result.rounds == 2
    follow_up = client.requests[1].messages
    assert follow_up[-3].role == "assistant"
    assert follow_up[-3].tool_calls == calls
    assert [message.tool_call_id for message in follow_up[-2:]] == [
        "call-control",
        "call-termination",
    ]
    assert [json.loads(message.content)["tool_budget"] for message in follow_up[-2:]] == [
        {"used": 1, "limit": 16, "remaining": 15},
        {"used": 2, "limit": 16, "remaining": 14},
    ]
    assert [record.tool_call_id for record in tools.log.records] == [
        "call-control",
        "call-termination",
    ]
    assert [call["call_id"] for call in log.records[0].tool_request["calls"]] == [
        "call-control",
        "call-termination",
    ]


def test_multi_call_turn_is_rejected_atomically_when_budget_is_too_small():
    tools = SourceTools(node="manager", library=sample_case.build_source_library())
    tools.begin_invocation(contract_id=sample_case.CONTRACT_ID, available=True)
    calls = (
        native_call(call_id="call-one", arguments={"query": "control"}),
        native_call(call_id="call-two", arguments={"query": "termination"}),
    )
    client = ScriptedModelClient((calls,), tool_transport="native")

    def build_request(messages):
        return ModelRequest(
            messages=messages,
            tools=tools.tool_definitions,
            tool_choice="auto",
            params=PARAMS,
            role="manager",
            prompt_version="manager-native-v1",
        )

    with pytest.raises(ToolLoopError, match="No tool from this response was executed"):
        run_tool_loop(
            client=client,
            build_request=build_request,
            initial_messages=(ModelMessage(role="user", content="Assess."),),
            output_model=ManagerOutput,
            repair=None,
            call_log=None,
            expected_role="manager",
            tools=tools,
            tool_transport="native",
            max_tool_rounds=1,
        )
    assert not tools.log.records


def test_native_text_tool_request_null_fails_without_repair_or_dispatch():
    tools = SourceTools(node="manager", library=sample_case.build_source_library())
    tools.begin_invocation(contract_id=sample_case.CONTRACT_ID, available=True)
    client = ScriptedModelClient(
        ('{"tool_request": null}',),
        tool_transport="native",
    )
    log = ModelCallLog()

    def build_request(messages):
        return ModelRequest(
            messages=messages,
            tools=tools.tool_definitions,
            tool_choice="auto",
            params=PARAMS,
            role="manager",
            prompt_version="manager-native-v1",
        )

    with pytest.raises(ToolLoopError, match="retired 'tool_request'"):
        run_tool_loop(
            client=client,
            build_request=build_request,
            initial_messages=(ModelMessage(role="user", content="Assess."),),
            output_model=ManagerOutput,
            repair=None,
            call_log=log,
            expected_role="manager",
            tools=tools,
            tool_transport="native",
        )
    assert not tools.log.records
    assert len(log.records) == 1
    assert log.records[0].parse_error.startswith("tool_protocol_violation:")
    assert not log.records[0].is_repair


def test_duplicate_native_call_id_aborts_before_second_dispatch():
    tools = SourceTools(node="manager", library=sample_case.build_source_library())
    tools.begin_invocation(contract_id=sample_case.CONTRACT_ID, available=True)
    repeated = native_call(call_id="call-duplicate")
    client = ScriptedModelClient(
        (repeated, repeated),
        tool_transport="native",
    )
    log = ModelCallLog()

    def build_request(messages):
        return ModelRequest(
            messages=messages,
            tools=tools.tool_definitions,
            tool_choice="auto",
            params=PARAMS,
            role="manager",
            prompt_version="manager-native-v1",
        )

    with pytest.raises(ToolLoopError, match="duplicate native tool call id"):
        run_tool_loop(
            client=client,
            build_request=build_request,
            initial_messages=(ModelMessage(role="user", content="Assess."),),
            output_model=ManagerOutput,
            repair=None,
            call_log=log,
            expected_role="manager",
            tools=tools,
            tool_transport="native",
        )
    assert len(tools.log.records) == 1
    assert len(log.records) == 2
    assert log.records[1].parse_error.startswith("tool_protocol_violation:")


def test_native_pagination_binds_query_and_target_inside_cursor():
    _, registry, _ = materials()
    tools = PagedSourceTools(
        node="manager",
        library=registry.library,
        target_categories=TARGETS,
        top_k=2,
        protocol_version="native-v1",
    )
    tools.begin_invocation(contract_id="CONTRACT-001", available=True)
    assert [tool.name for tool in tools.tool_definitions] == [
        "search_contract",
        "next_search_page",
        "open_source_span",
    ]
    first = tools.search_contract("administrative", target_category=TARGETS[0])
    cursor = first.payload["next_cursor"]
    second = tools.next_search_page(cursor)
    assert second.tool == NEXT_SEARCH_PAGE
    assert second.arguments == {"cursor": cursor}
    assert second.payload["query"] == "administrative"
    assert second.payload["target_category"] == TARGETS[0]
    assert set(first.result_ids).isdisjoint(second.result_ids)
    with pytest.raises(SourceToolError, match="next_search_page"):
        tools.search_contract(
            "changed",
            target_category=TARGETS[1],
            cursor=cursor,
        )
