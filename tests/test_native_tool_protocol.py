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


def test_native_v2_wire_explicitly_disables_parallel_tool_calls():
    tools = SourceTools(node="manager", library=sample_case.build_source_library())
    request = ModelRequest(
        messages=(ModelMessage(role="user", content="Assess."),),
        tools=tools.tool_definitions,
        tool_choice="auto",
        parallel_tool_calls=False,
        params=PARAMS,
        role="manager",
        prompt_version="manager-native-v2",
    )
    client = OpenAICompatibleClient(
        base_url="https://offline.invalid/v1", api_key="offline-placeholder",
        tool_transport="native", transport=lambda *_: b"",
    )
    assert json.loads(client.build_request(request).data)["parallel_tool_calls"] is False


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


def test_native_loop_switches_to_final_only_after_budget_is_exhausted():
    tools = SourceTools(node="manager", library=sample_case.build_source_library())
    tools.begin_invocation(contract_id=sample_case.CONTRACT_ID, available=True)
    call = native_call(call_id="call-last", arguments={"query": "control"})
    client = ScriptedModelClient(
        (call, sample_case.manager_response_text()),
        tool_transport="native",
    )

    def build_request(messages):
        return ModelRequest(
            messages=messages,
            tools=tools.tool_definitions,
            tool_choice="auto",
            params=SAMPLING_PARAMS,
            role="manager",
            prompt_version="manager-native-v1",
        )

    result = run_tool_loop(
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
        tool_turn_max_output_tokens=8192,
        final_max_output_tokens=16384,
    )
    assert result.rounds == 1
    assert client.requests[0].params.max_output_tokens == 8192
    final_request = client.requests[1]
    assert final_request.params.max_output_tokens == 16384
    assert final_request.tools == ()
    assert final_request.tool_choice is None
    assert "tool budget is exhausted" in final_request.messages[-1].content.lower()
    assert "unknown" in final_request.messages[-1].content.lower()


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


def test_native_v2_pagination_exposes_only_live_single_use_cursor():
    _, registry, _ = materials()
    tools = PagedSourceTools(
        node="manager", library=registry.library, target_categories=TARGETS,
        top_k=2, protocol_version="native-v2",
    )
    tools.begin_invocation(contract_id="CONTRACT-001", available=True)
    assert [tool.name for tool in tools.tool_definitions] == ["search_contract"]
    page = tools.search_contract("administrative", target_category=TARGETS[0])
    first_cursor = page.payload["next_cursor"]
    assert first_cursor
    continuation = next(tool for tool in tools.tool_definitions if tool.name == NEXT_SEARCH_PAGE)
    assert continuation.input_schema["properties"]["cursor"]["enum"] == [first_cursor]
    opener = next(tool for tool in tools.tool_definitions if tool.name == "open_source_span")
    assert opener.input_schema["properties"]["paragraph_id"]["enum"] == sorted(page.result_ids)
    hidden_id = next(
        paragraph_id for paragraph_id in registry.library.get("CONTRACT-001").paragraph_ids()
        if paragraph_id not in page.result_ids
    )
    with pytest.raises(SourceToolError, match="was not returned by search"):
        tools.open_source_span(hidden_id)

    # A cursor is a state transition token, not a reusable string.  Consume
    # every offered token, then pagination must disappear entirely.
    while page.payload["next_cursor"]:
        page = tools.next_search_page(page.payload["next_cursor"])
    assert NEXT_SEARCH_PAGE not in [tool.name for tool in tools.tool_definitions]
    with pytest.raises(SourceToolError, match="unknown or expired"):
        tools.next_search_page(first_cursor)


def test_bounded_retrieval_stops_after_two_pages_even_when_source_has_more():
    _, registry, _ = materials()
    tools = PagedSourceTools(
        node="manager", library=registry.library, target_categories=TARGETS,
        top_k=2, protocol_version="native-v2", max_search_pages_per_query=2,
    )
    tools.begin_invocation(contract_id="CONTRACT-001", available=True)
    first = tools.search_contract("administrative", target_category=TARGETS[0])
    assert first.payload["next_cursor"]
    second = tools.next_search_page(first.payload["next_cursor"])
    assert second.payload["page_number"] == 2
    assert second.payload["source_has_more"] is True
    assert second.payload["page_limit_reached"] is True
    assert second.payload["has_more"] is False
    assert second.payload["next_cursor"] is None
    assert NEXT_SEARCH_PAGE not in [tool.name for tool in tools.tool_definitions]


def test_bounded_retrieval_rejects_duplicate_open_and_logs_it():
    _, registry, _ = materials()
    tools = PagedSourceTools(
        node="manager", library=registry.library, target_categories=TARGETS,
        top_k=2, protocol_version="native-v2", reject_duplicate_opens=True,
    )
    tools.begin_invocation(contract_id="CONTRACT-001", available=True)
    page = tools.search_contract("administrative", target_category=TARGETS[0])
    paragraph_id = page.result_ids[0]
    tools.open_source_span(paragraph_id)
    with pytest.raises(SourceToolError, match="already opened"):
        tools.open_source_span(paragraph_id)
    assert [record.ok for record in tools.log.records] == [True, True, False]
    assert tools.log.records[-1].tool == "open_source_span"


def test_target_category_is_an_audit_label_not_a_ranking_feature():
    _, registry, _ = materials()
    tools = PagedSourceTools(
        node="manager", library=registry.library, target_categories=TARGETS,
        top_k=5, protocol_version="native-v2",
        search_ranker_version="human-lexical-v2",
    )
    tools.begin_invocation(contract_id="CONTRACT-001", available=True)
    first = tools.search_contract("administrative", target_category=TARGETS[0])
    second = tools.search_contract("administrative", target_category=TARGETS[1])
    assert first.result_ids == second.result_ids
    assert [result["score"] for result in first.payload["results"]] == [
        result["score"] for result in second.payload["results"]
    ]


def test_context_compaction_keeps_logs_but_replaces_duplicate_search_history():
    _, registry, _ = materials()
    tools = PagedSourceTools(
        node="manager", library=registry.library, target_categories=TARGETS,
        top_k=2, protocol_version="native-v2",
        reject_duplicate_searches=True,
    )
    tools.begin_invocation(contract_id="CONTRACT-001", available=True)
    search_args = {"query": "administrative", "target_category": TARGETS[0]}
    client = ScriptedModelClient((
        native_call(arguments=search_args, call_id="search-1"),
        native_call(arguments=search_args, call_id="search-2"),
        native_call(arguments=search_args, call_id="search-3"),
        sample_case.manager_response_text(),
    ), tool_transport="native")
    client.estimate_request_tokens = lambda request: sum(
        len(message.content or "") for message in request.messages
    )

    def build_request(messages):
        return ModelRequest(messages=messages, tools=tools.tool_definitions,
                            tool_choice="auto", params=SAMPLING_PARAMS,
                            role="manager", prompt_version="manager-native-v2")

    result = run_tool_loop(
        client=client, build_request=build_request,
        initial_messages=(ModelMessage(role="user", content="Assess."),),
        output_model=ManagerOutput, repair=None, call_log=None,
        expected_role="manager", tools=tools, tool_transport="native",
        strict_native_state_machine=True,
        context_compaction_version="retrieval-ledger-v1",
        context_compaction_trigger_tokens=100000,
        context_compaction_target_tokens=50000,
        search_disable_after_no_new=2,
    )

    assert result.compacted_tool_turns == 2
    assert result.search_disabled is True
    assert len(tools.log.records) == 3
    assert [record.ok for record in tools.log.records] == [True, False, False]
    final_request = client.requests[-1]
    rendered = "\n".join(message.content or "" for message in final_request.messages)
    assert "RUNTIME RETRIEVAL LEDGER" in rendered
    assert "#2 search_contract" in rendered and "#3 search_contract" in rendered
    assert "RUNTIME SEARCH STOP" in rendered
    assert "search_contract" not in [tool.name for tool in final_request.tools]
    assert "next_search_page" not in [tool.name for tool in final_request.tools]
    assert "open_source_span" in [tool.name for tool in final_request.tools]


def test_token_pressure_compacts_only_complete_old_search_pairs():
    _, registry, _ = materials()
    tools = PagedSourceTools(
        node="manager", library=registry.library, target_categories=TARGETS,
        top_k=2, protocol_version="native-v2",
    )
    tools.begin_invocation(contract_id="CONTRACT-001", available=True)
    client = ScriptedModelClient((
        native_call(arguments={"query": "administrative", "target_category": TARGETS[0]},
                    call_id="pressure-1"),
        native_call(arguments={"query": "indemnification", "target_category": TARGETS[1]},
                    call_id="pressure-2"),
        sample_case.manager_response_text(),
    ), tool_transport="native")
    client.estimate_request_tokens = lambda request: sum(
        len(message.content or "") for message in request.messages
    )

    def build_request(messages):
        return ModelRequest(messages=messages, tools=tools.tool_definitions,
                            tool_choice="auto", params=SAMPLING_PARAMS,
                            role="manager", prompt_version="manager-native-v2")

    result = run_tool_loop(
        client=client, build_request=build_request,
        initial_messages=(ModelMessage(role="user", content="Assess."),),
        output_model=ManagerOutput, repair=None, call_log=None,
        expected_role="manager", tools=tools, tool_transport="native",
        strict_native_state_machine=True,
        context_compaction_version="retrieval-ledger-v1",
        context_compaction_trigger_tokens=800,
        context_compaction_target_tokens=400,
        search_disable_after_no_new=3,
    )
    assert result.compacted_tool_turns >= 1
    assert len(tools.log.records) == 2
    final_request = client.requests[-1]
    rendered = "\n".join(message.content or "" for message in final_request.messages)
    assert "RUNTIME RETRIEVAL LEDGER" in rendered
    # Native history is removed as complete assistant/tool pairs: no orphaned tool result.
    call_ids = {call.call_id for message in final_request.messages for call in message.tool_calls}
    assert all(
        message.role != "tool" or message.tool_call_id in call_ids
        for message in final_request.messages
    )


def test_context_compaction_never_removes_an_opened_full_paragraph():
    _, registry, _ = materials()
    probe = PagedSourceTools(
        node="probe", library=registry.library, target_categories=TARGETS,
        top_k=2, protocol_version="native-v2",
    )
    probe.begin_invocation(contract_id="CONTRACT-001", available=True)
    paragraph_id = probe.search_contract(
        "administrative", target_category=TARGETS[0]
    ).result_ids[0]
    paragraph_text = registry.library.get("CONTRACT-001").paragraph(paragraph_id).text

    tools = PagedSourceTools(
        node="manager", library=registry.library, target_categories=TARGETS,
        top_k=2, protocol_version="native-v2",
    )
    tools.begin_invocation(contract_id="CONTRACT-001", available=True)
    client = ScriptedModelClient((
        native_call(arguments={"query": "administrative", "target_category": TARGETS[0]},
                    call_id="keep-search"),
        native_call(name="open_source_span", arguments={"paragraph_id": paragraph_id},
                    call_id="keep-open"),
        native_call(arguments={"query": "indemnification", "target_category": TARGETS[1]},
                    call_id="keep-search-2"),
        sample_case.manager_response_text(),
    ), tool_transport="native")
    client.estimate_request_tokens = lambda request: sum(
        len(message.content or "") for message in request.messages
    )

    def build_request(messages):
        return ModelRequest(messages=messages, tools=tools.tool_definitions,
                            tool_choice="auto", params=SAMPLING_PARAMS,
                            role="manager", prompt_version="manager-native-v2")

    result = run_tool_loop(
        client=client, build_request=build_request,
        initial_messages=(ModelMessage(role="user", content="Assess."),),
        output_model=ManagerOutput, repair=None, call_log=None,
        expected_role="manager", tools=tools, tool_transport="native",
        strict_native_state_machine=True,
        context_compaction_version="retrieval-ledger-v1",
        context_compaction_trigger_tokens=800,
        context_compaction_target_tokens=400,
        search_disable_after_no_new=3,
    )
    assert result.compacted_tool_turns >= 1
    final_rendered = "\n".join(
        message.content or "" for message in client.requests[-1].messages
    )
    assert paragraph_text in final_rendered
    assert any(
        message.role == "tool" and message.name == "open_source_span"
        for message in client.requests[-1].messages
    )


def test_native_v2_rejects_a_parallel_batch_before_any_dispatch():
    tools = SourceTools(node="manager", library=sample_case.build_source_library())
    tools.begin_invocation(contract_id=sample_case.CONTRACT_ID, available=True)
    batch = (
        native_call(call_id="call-one"),
        native_call(call_id="call-two", arguments={"query": "termination"}),
    )
    client = ScriptedModelClient((batch,), tool_transport="native")
    log = ModelCallLog()

    def build_request(messages):
        return ModelRequest(messages=messages, tools=tools.tool_definitions, tool_choice="auto",
                            params=PARAMS, role="manager", prompt_version="manager-native-v2")

    with pytest.raises(ToolLoopError, match="exactly one tool call"):
        run_tool_loop(client=client, build_request=build_request,
                      initial_messages=(ModelMessage(role="user", content="Assess."),),
                      output_model=ManagerOutput, repair=None, call_log=log,
                      expected_role="manager", tools=tools, tool_transport="native",
                      strict_native_state_machine=True)
    assert not tools.log.records
    assert client.requests[0].parallel_tool_calls is False
    assert log.records[0].parse_error.startswith("tool_protocol_violation:")


def test_native_v2_last_budget_rejects_batch_before_second_call():
    tools = SourceTools(node="manager", library=sample_case.build_source_library())
    tools.begin_invocation(contract_id=sample_case.CONTRACT_ID, available=True)
    first = native_call(call_id="call-first")
    batch = (
        native_call(call_id="call-last-a", arguments={"query": "termination"}),
        native_call(call_id="call-last-b", arguments={"query": "notice"}),
    )
    client = ScriptedModelClient(((first,), batch), tool_transport="native")

    def build_request(messages):
        return ModelRequest(messages=messages, tools=tools.tool_definitions, tool_choice="auto",
                            params=PARAMS, role="manager", prompt_version="manager-native-v2")

    with pytest.raises(ToolLoopError, match="exactly one tool call"):
        run_tool_loop(client=client, build_request=build_request,
                      initial_messages=(ModelMessage(role="user", content="Assess."),),
                      output_model=ManagerOutput, repair=None, call_log=None,
                      expected_role="manager", tools=tools, tool_transport="native",
                      max_tool_rounds=2, strict_native_state_machine=True)
    assert len(tools.log.records) == 1


def test_native_v2_rejects_fabricated_cursor_before_tool_execution():
    _, registry, _ = materials()
    tools = PagedSourceTools(node="manager", library=registry.library,
                             target_categories=TARGETS, protocol_version="native-v2")
    tools.begin_invocation(contract_id="CONTRACT-001", available=True)
    client = ScriptedModelClient(((native_call(
        name=NEXT_SEARCH_PAGE, arguments={"cursor": "invalid_check_only"}, call_id="bad-cursor"),),),
        tool_transport="native")

    def build_request(messages):
        return ModelRequest(messages=messages, tools=tools.tool_definitions, tool_choice="auto",
                            params=PARAMS, role="manager", prompt_version="manager-native-v2")

    with pytest.raises(ToolLoopError, match="was not offered"):
        run_tool_loop(client=client, build_request=build_request,
                      initial_messages=(ModelMessage(role="user", content="Assess."),),
                      output_model=ManagerOutput, repair=None, call_log=None,
                      expected_role="manager", tools=tools, tool_transport="native",
                      strict_native_state_machine=True)
    assert not tools.log.records
