"""Gate 2.5, group 6: the bounded tool-use loop.

One node invocation may now take several model turns. That is a small piece of
control flow with a large blast radius, so it is tested as its own unit rather
than only through a run:

* **The six shapes.** No tool call; one search; search then open; several
  bounded calls; an invalid request; a successful final output. Every one is a
  scripted response sequence replayed through the real parsing path, so the loop
  is exercised, not simulated.
* **The loop is bounded.** ``MAX_TOOL_ROUNDS`` caps tool *requests*. A model that
  never stops asking is a failed run, not an experiment that spends forever.
* **A refusal is data.** An unknown tool, a malformed envelope, an id that does
  not exist -- each comes back to the model as a failed tool result it can read
  and act on. Whether an agent recovers from a refusal is part of what the pilot
  measures, so killing the run would erase the observation.
* **Every turn is on the record.** The raw-call log keeps each turn with its
  call index, the tool request it carried, and its position in the sequence; the
  tool log keeps each call with the node, invocation, run id and arguments.
"""

from __future__ import annotations

import pytest

import sample_case
from pilot01.model import ModelCallLog, ScriptedModelClient
from pilot01.model.client import ModelParams, ModelRequest
from pilot01.model.parsing import ModelOutputError
from pilot01.schemas import ErrorCondition, ManagerOutput, VerificationStatus
from pilot01.source import ToolCallLog
from pilot01.source.tools import OPEN_SOURCE_SPAN, SEARCH_CONTRACT, SourceTools
from pilot01.workflow.nodes.tool_loop import (
    MAX_TOOL_ROUNDS,
    ToolLoopError,
    extract_tool_request,
    render_tool_result,
    render_tool_surface,
    run_tool_loop,
)
from pilot01.workflow.transitions import ProtocolError

TARGET_PARAGRAPH = sample_case.paragraph_id_containing(sample_case.CHANGE_OF_CONTROL_PHRASE)
LIABILITY_PARAGRAPH = sample_case.paragraph_id_containing(sample_case.LIABILITY_CAP_PHRASE)


def search(query: str = "change of control") -> str:
    return sample_case.tool_request_text("search_contract", query=query)


def open_span(paragraph_id: str) -> str:
    return sample_case.tool_request_text("open_source_span", paragraph_id=paragraph_id)


def answer(**kwargs) -> str:
    return sample_case.manager_response_text(**kwargs)


PARAMS = ModelParams(
    provider="scripted", model_id="scripted-1", temperature=0.0, max_output_tokens=512
)


def build_request(messages) -> ModelRequest:
    """A stand-in for the node's own request builder.

    The node supplies this so the loop never needs to know about provenance; the
    stub keeps the same shape and adds nothing.
    """
    return ModelRequest(
        messages=tuple(messages),
        params=PARAMS,
        role="manager",
        prompt_version="manager_v1",
        invocation=0,
        run_id="run-0001",
    )


def drive(
    responses: tuple[str, ...],
    *,
    output_model=ManagerOutput,
    tools: SourceTools | None = None,
    call_log: ModelCallLog | None = None,
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
    audit=None,
):
    """Run the loop over a scripted sequence and return (result, client, tools)."""
    if tools is None:
        tools = SourceTools(
            node="manager",
            library=sample_case.build_source_library(),
            log=ToolCallLog(),
        )
        tools.begin_invocation(
            contract_id=sample_case.CONTRACT_ID, available=True, run_id="run-0001"
        )
    client = ScriptedModelClient(responses)
    result = run_tool_loop(
        client=client,
        build_request=build_request,
        initial_messages=(),
        output_model=output_model,
        repair=None,
        call_log=call_log,
        expected_role="manager",
        tools=tools,
        max_tool_rounds=max_tool_rounds,
        audit=audit,
    )
    return result, client, tools


# --------------------------------------------------------------------------
# The six shapes
# --------------------------------------------------------------------------


def test_no_tool_call_is_a_complete_run():
    """A1V0's zero-lookup path: one turn, no tools, a valid answer."""
    result, client, tools = drive((answer(),))
    assert client.call_count == 1
    assert result.rounds == 0
    assert result.tool_outcomes == ()
    assert isinstance(result.output, ManagerOutput)
    assert tools.ledger.used_source_tools() is False
    assert tools.log.records == ()


def test_one_search_then_an_answer():
    result, client, tools = drive((search(), answer()))
    assert client.call_count == 2
    assert result.rounds == 1
    assert [outcome.tool for outcome in result.tool_outcomes] == [SEARCH_CONTRACT]
    assert result.tool_outcomes[0].ok
    assert result.tool_outcomes[0].result_ids
    assert tools.ledger.searches == ("change of control",)
    assert tools.ledger.opened_ids == ()
    assert isinstance(result.output, ManagerOutput)


def test_search_then_open_then_an_answer():
    result, client, tools = drive((search(), open_span(TARGET_PARAGRAPH), answer()))
    assert client.call_count == 3
    assert result.rounds == 2
    assert [outcome.tool for outcome in result.tool_outcomes] == [
        SEARCH_CONTRACT,
        OPEN_SOURCE_SPAN,
    ]
    assert tools.ledger.opened_ids == (TARGET_PARAGRAPH,)
    assert isinstance(result.output, ManagerOutput)


def test_several_bounded_calls():
    """Four requests, then the answer. The limit is on requests, not turns."""
    responses = (
        search("change of control"),
        open_span(TARGET_PARAGRAPH),
        search("liability"),
        open_span(LIABILITY_PARAGRAPH),
        answer(),
    )
    result, client, tools = drive(responses)
    assert result.rounds == 4
    assert client.call_count == 5
    assert tools.ledger.opened_ids == (TARGET_PARAGRAPH, LIABILITY_PARAGRAPH)
    assert len(tools.ledger.searches) == 2
    assert isinstance(result.output, ManagerOutput)


def test_an_invalid_tool_request_is_returned_to_the_model_not_raised():
    """An unknown tool. The model reads the refusal and recovers."""
    result, client, tools = drive(
        (
            sample_case.tool_request_text("delete_contract", paragraph_id="x"),
            answer(),
        )
    )
    assert result.rounds == 1
    outcome = result.tool_outcomes[0]
    assert outcome.ok is False
    assert "delete_contract" in (outcome.error or "")
    # The refusal reached the model, as a user turn it can act on.
    assert client.call_count == 2
    follow_up = client.requests[1].messages
    assert follow_up[-1].role == "user"
    assert "TOOL RESULT" in follow_up[-1].content
    assert "status: error" in follow_up[-1].content
    assert isinstance(result.output, ManagerOutput)


def test_a_successful_final_output_is_validated_not_trusted():
    """The last turn goes through the real structured path."""
    result, _, _ = drive((search(), answer(confidence=0.25)))
    assert result.output.confidence == 0.25
    assert result.repaired is False
    assert result.final_record.parsed_output is not None


def test_a_tool_request_with_the_wrong_arguments_is_refused():
    """``search_contract`` with no query. Refused, returned, recoverable."""
    result, _, tools = drive(
        ('{"tool_request": {"tool": "search_contract"}}', answer())
    )
    outcome = result.tool_outcomes[0]
    assert outcome.ok is False
    assert outcome.tool == SEARCH_CONTRACT
    assert "takes exactly the argument" in (outcome.error or "")
    assert tools.log.records[0].ok is False


def test_a_tool_request_that_is_not_an_object_is_refused():
    result, _, tools = drive(('{"tool_request": "search_contract"}', answer()))
    outcome = result.tool_outcomes[0]
    assert outcome.ok is False
    assert "must be an object" in (outcome.error or "")
    assert tools.log.records[0].ok is False


def test_a_tool_request_with_extra_keys_is_refused():
    result, _, _ = drive(
        (
            '{"tool_request": {"tool": "search_contract", "arguments": {"query": "x"},'
            ' "reason": "because"}}',
            answer(),
        )
    )
    assert result.tool_outcomes[0].ok is False
    assert "only 'tool' and 'arguments'" in (result.tool_outcomes[0].error or "")


# --------------------------------------------------------------------------
# The loop is bounded
# --------------------------------------------------------------------------


def test_a_model_that_never_stops_asking_is_cut_off():
    """Every turn is a tool request, so the loop must end by refusing to go on."""
    responses = tuple(search(f"query {index}") for index in range(MAX_TOOL_ROUNDS + 3))
    with pytest.raises(ToolLoopError, match="without producing a final answer"):
        drive(responses)


def test_the_limit_is_on_requests_not_on_turns():
    """Exactly at the limit is fine; one past it is not."""
    at_limit = tuple(search(f"query {index}") for index in range(MAX_TOOL_ROUNDS)) + (
        answer(),
    )
    result, _, _ = drive(at_limit)
    assert result.rounds == MAX_TOOL_ROUNDS

    past_limit = tuple(search(f"query {index}") for index in range(MAX_TOOL_ROUNDS + 1))
    with pytest.raises(ToolLoopError):
        drive(past_limit)


def test_the_limit_is_configurable():
    responses = (search("a"), search("b"), answer())
    with pytest.raises(ToolLoopError):
        drive(responses, max_tool_rounds=1)
    result, _, _ = drive(responses, max_tool_rounds=2)
    assert result.rounds == 2


def test_the_runaway_failure_is_a_protocol_error():
    """So the runner records it and stops, rather than salvaging an output."""
    assert issubclass(ToolLoopError, ProtocolError)
    responses = tuple(search(f"q{index}") for index in range(MAX_TOOL_ROUNDS + 1))
    with pytest.raises(ProtocolError):
        drive(responses)


def test_the_runaway_failure_says_how_many_it_asked_for():
    responses = tuple(search(f"q{index}") for index in range(MAX_TOOL_ROUNDS + 1))
    with pytest.raises(ToolLoopError) as excinfo:
        drive(responses)
    assert str(MAX_TOOL_ROUNDS) in str(excinfo.value)


# --------------------------------------------------------------------------
# Every turn is on the record
# --------------------------------------------------------------------------


def test_every_turn_is_recorded_with_a_dense_call_index():
    log = ModelCallLog()
    drive((search(), open_span(TARGET_PARAGRAPH), answer()), call_log=log)
    assert [record.call_index for record in log.records] == [0, 1, 2]
    assert len(log) == 3


def test_tool_turns_are_marked_and_carry_the_request():
    log = ModelCallLog()
    drive((search(), answer()), call_log=log)
    first, second = log.records
    assert first.is_tool_turn is True
    assert first.tool_request["tool"] == SEARCH_CONTRACT
    assert first.tool_request["arguments"] == {"query": "change of control"}
    assert second.is_tool_turn is False
    assert second.tool_request is None


def test_the_tool_log_records_the_model_call_each_tool_call_answers():
    log = ModelCallLog()
    _, _, tools = drive(
        (search(), open_span(TARGET_PARAGRAPH), answer()), call_log=log
    )
    records = tools.log.records
    assert [record.call_index for record in records] == [0, 1]
    assert [record.tool for record in records] == [SEARCH_CONTRACT, OPEN_SOURCE_SPAN]
    assert records[0].arguments == {"query": "change of control"}
    assert records[1].arguments == {"paragraph_id": TARGET_PARAGRAPH}
    assert records[1].result_ids == (TARGET_PARAGRAPH,)


def test_the_tool_log_sequence_is_dense_and_monotonic():
    _, _, tools = drive((search(), search("liability"), answer()))
    assert [record.sequence for record in tools.log.records] == [0, 1]


def test_the_run_and_node_are_stamped_on_every_tool_call():
    _, _, tools = drive((search(), answer()))
    record = tools.log.records[0]
    assert record.node == "manager"
    assert record.run_id == "run-0001"
    assert record.invocation == 0


def test_refusals_are_recorded_alongside_successes():
    _, _, tools = drive((search(), open_span("not-a-paragraph-id"), answer()))
    assert len(tools.log.records) == 2
    assert [record.ok for record in tools.log.records] == [True, False]
    assert tools.log.failures()[0].tool == OPEN_SOURCE_SPAN


def test_the_audit_runs_on_every_turn_not_only_the_first():
    """The node's hidden-key audit extends to the whole invocation."""
    seen: list[int] = []
    drive((search(), open_span(TARGET_PARAGRAPH), answer()), audit=lambda r: seen.append(len(r.messages)))
    assert seen == [0, 2, 4]


def test_the_raw_response_text_is_kept_unmodified():
    raw = answer(confidence=0.5)
    log = ModelCallLog()
    drive((raw,), call_log=log)
    assert log.records[0].raw_response == raw


# --------------------------------------------------------------------------
# The loop hands malformed answers to the structured path, not to itself
# --------------------------------------------------------------------------


def test_a_non_tool_response_goes_through_the_structured_path():
    """Garbage in, the real diagnostic out -- not a loop-level guess."""
    with pytest.raises(ModelOutputError):
        drive(("this is not JSON at all",))


def test_the_records_of_a_failed_parse_are_still_kept():
    log = ModelCallLog()
    with pytest.raises(ModelOutputError):
        drive(("not json",), call_log=log)
    assert len(log) >= 1
    assert log.records[-1].parse_error


def test_a_response_with_no_json_object_is_not_treated_as_a_tool_request():
    assert extract_tool_request("no json here") is None
    assert extract_tool_request('{"tool_request": {"tool": "x", "arguments": {}}}') == {
        "tool": "x",
        "arguments": {},
    }
    assert extract_tool_request('{"clause_status": "present"}') is None


def test_a_tool_request_wrapped_in_a_fence_is_still_read():
    """Models fence things unprompted; the extractor tolerates it."""
    fenced = sample_case.fenced(search())
    assert extract_tool_request(fenced) == {
        "tool": SEARCH_CONTRACT,
        "arguments": {"query": "change of control"},
    }


# --------------------------------------------------------------------------
# The surface and the result rendering
# --------------------------------------------------------------------------


def test_the_surface_lists_exactly_the_tools_that_exist():
    from pilot01.source.tools import SOURCE_TOOL_NAMES

    text = render_tool_surface(names=SOURCE_TOOL_NAMES, available=True)
    for name in SOURCE_TOOL_NAMES:
        assert f"- `{name}(" in text
    assert len(SOURCE_TOOL_NAMES) == 2


def test_the_surface_is_empty_when_tools_are_unavailable():
    from pilot01.source.tools import SOURCE_TOOL_NAMES

    text = render_tool_surface(names=SOURCE_TOOL_NAMES, available=False)
    assert "No source tools are available" in text
    assert "search_contract" not in text
    assert "open_source_span" not in text


def test_the_surface_advertises_the_same_limit_the_loop_enforces():
    from pilot01.source.tools import SOURCE_TOOL_NAMES

    text = render_tool_surface(names=SOURCE_TOOL_NAMES, available=True)
    assert str(MAX_TOOL_ROUNDS) in text


def test_a_refusal_is_rendered_without_advice():
    """Told what failed and why; not told what to ask for instead."""
    result, _, _ = drive((search(), open_span("bogus-id"), answer()))
    refusal = result.tool_outcomes[1]
    assert refusal.ok is False
    rendered = render_tool_result(refusal)
    assert "status: error" in rendered
    assert refusal.error in rendered
    assert "try" not in rendered.lower()
    assert "instead" not in rendered.lower()


def test_an_empty_search_result_is_rendered_as_empty_not_as_an_error():
    result, _, tools = drive((search("zzzzzqqqqxxxx"), answer()))
    record = tools.log.records[0]
    assert record.ok is True
    assert record.result_ids == ()
    rendered = render_tool_result(result.tool_outcomes[0])
    assert "results: 0" in rendered
    assert "no paragraph of this contract matched" in rendered


def test_an_opened_span_is_rendered_with_its_offsets_and_text():
    result, _, _ = drive((open_span(TARGET_PARAGRAPH), answer()))
    rendered = render_tool_result(result.tool_outcomes[0])
    assert f"paragraph_id: {TARGET_PARAGRAPH}" in rendered
    assert "offsets: " in rendered
    assert "text:" in rendered
    # The full paragraph text, not the elided excerpt a search would show.
    assert "..." not in rendered


# --------------------------------------------------------------------------
# No tool layer at all
# --------------------------------------------------------------------------


def test_a_tool_request_with_no_tool_layer_is_a_protocol_error():
    """A wiring bug, not a model error: the node was built without tools."""
    client = ScriptedModelClient((search(), answer()))
    with pytest.raises(ToolLoopError, match="no tool layer"):
        run_tool_loop(
            client=client,
            build_request=build_request,
            initial_messages=(),
            output_model=ManagerOutput,
            repair=None,
            call_log=None,
            expected_role="manager",
            tools=None,
        )


def test_a_client_failure_is_recorded_before_it_propagates():
    from pilot01.model import ModelClientError

    log = ModelCallLog()

    class Exploding:
        def generate(self, request):
            raise ModelClientError("transport is down")

    with pytest.raises(ModelClientError):
        run_tool_loop(
            client=Exploding(),
            build_request=build_request,
            initial_messages=(),
            output_model=ManagerOutput,
            repair=None,
            call_log=log,
            expected_role="manager",
            tools=None,
        )
    assert len(log) == 1
    assert "transport is down" in (log.records[0].error or "")
