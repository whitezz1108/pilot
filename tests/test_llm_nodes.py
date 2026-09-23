"""Gate 2: the real, model-backed Manager and Compliance nodes.

This file is the isolation audit in executable form. It runs the *real* nodes --
not the fixture doubles -- against a scripted transport, and then reads the
messages those nodes actually built and the calls they actually made.

The properties checked, in the order the specification lists them:

1. the Manager is handed its restricted view and nothing else;
2. the Manager's request carries no gold-shaped data;
3. Compliance is handed its restricted view and nothing else;
4. Compliance cannot see the analyst memo;
5. Compliance cannot see hidden gold;
6. the two agents are separate, fresh model invocations;
7. responses go through the existing structured schema;
8. an invalid response gets exactly one format-only repair;
9. a failed repair surfaces as a protocol failure, not a guess;
10. no Python code derives a status or a decision from gold;
17. the prompt version is recorded on every call;
18. changing the treatment cannot change the decision before the model speaks;
19. the Gate 1.5 isolation invariants still hold.

Two assertions deserve a note, because they look weaker than they are.
``test_compliance_cannot_see_the_analyst_memo`` cannot simply scan the
compliance request for the word "memo": the compliance prompt file tells the
agent in so many words that it does not receive one, and that sentence is a
feature. So the check is on the memo's *content* -- its identifier, its claim
count, its claim ids and its claim prose -- and it pins the one thing that does
legitimately travel, which is the target claim id the Manager chose to cite in
its own handoff. ``test_no_agent_node_code_names_a_hidden_value`` cannot scan
node source for the string "gold" either, because the module docstrings explain
at length why the agents never see it; it parses each module and inspects the
code, with docstrings neutralised.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

import pilot01
import sample_case
from pilot01.config import load_models_v1
from pilot01.events import EventType
from pilot01.model import ModelCallLog, ModelClientError, ScriptedModelClient
from pilot01.prompts import (
    load_compliance_prompt,
    load_manager_prompt,
    load_repair_prompt,
)
from pilot01.schemas import (
    ClauseStatus,
    ComplianceOutput,
    Decision,
    ErrorCondition,
    ManagerOutput,
)
from pilot01.source import ToolCallLog, VerificationLog
from pilot01.workflow.nodes import (
    FakeAgentScript,
    FrozenMemoRepository,
    build_llm_registry,
    build_registry,
)
from pilot01.workflow.nodes.compliance import build_compliance_messages
from pilot01.workflow.nodes.manager import build_manager_messages
from pilot01.workflow.runner import WorkflowRunner
from pilot01.workflow.state import RunStatus
from pilot01.workflow.transitions import ProtocolError
from pilot01.workflow.views import (
    ComplianceInput,
    ManagerInput,
    ViewLeakError,
    assert_view_clean,
    audit_keys,
    build_compliance_view,
    build_manager_view,
)

NODES_DIR = Path(pilot01.__file__).resolve().parent / "workflow" / "nodes"

GOLD_SENTINELS = (987654321, 987654322)
"""The distinctive hidden-gold offsets the fixture installs."""

FORBIDDEN_REQUEST_TEXT = (
    "gold",
    "hidden",
    "omission",
    "omitted",
    "error_condition",
    "E0",
    "E1",
)
"""No rendered agent request may contain any of these, in either role.

"memo" is deliberately *not* here: the manager's memo block and the compliance
prompt's disclaimer both legitimately contain it, and the memo's content is
checked directly instead.
"""

CONDITION_NAME_RE = re.compile(r"\b(?:A0V0|A1V0|A1V1|A0|A1|V0|V1|E0|E1)\b")
"""The condition names, matched as whole words rather than as substrings.

Word boundaries matter: a bare substring search for ``V1`` would flag any text
that happened to contain it, and a test that fires on the wrong thing is worse
than no test.
"""


def rendered(client: ScriptedModelClient, index: int = 0) -> str:
    return client.requests[index].rendered_input()


# --------------------------------------------------------------------------
# 1-2. The Manager's restricted view, and what its request contains
# --------------------------------------------------------------------------


def test_the_manager_node_requires_a_restricted_view():
    node = _llm_registry().get("manager")
    assert node.requires_restricted_view is True
    assert node.output_model is ManagerOutput


def test_the_manager_node_refuses_anything_but_a_manager_view(llm_case, make_state):
    node = llm_case.registry.get("manager")
    with pytest.raises(ProtocolError, match="expected ManagerInput"):
        node.run(make_state())


def test_the_manager_is_passed_a_restricted_view_by_the_runner(llm_runner, make_state):
    outcome = llm_runner.run(make_state())
    created = [
        event
        for event in outcome.events
        if event.event_type is EventType.AGENT_INPUT_CREATED and event.node == "manager"
    ]
    assert created
    assert created[0].payload["restricted"] is True


def test_the_manager_request_is_rendered_from_the_view_alone(llm_runner, llm_case, make_state):
    llm_runner.run(make_state())
    text = rendered(llm_case.manager_client)
    assert sample_case.CASE_ID in text
    assert sample_case.TARGET_CLAIM_ID in text
    assert "POLICY-01" in text
    assert "source_access" in text


def test_the_manager_request_contains_no_gold_shaped_data(llm_runner, llm_case, make_state):
    llm_runner.run(make_state())
    text = rendered(llm_case.manager_client)
    for sentinel in GOLD_SENTINELS:
        assert str(sentinel) not in text
    for pattern in FORBIDDEN_REQUEST_TEXT:
        assert pattern not in text, f"manager request mentions {pattern!r}"


@pytest.mark.parametrize("condition_id", ["A0V0", "A1V0", "A1V1"])
def test_the_manager_request_never_names_the_governance_condition(make_state, condition_id):
    """The condition reaches the agent as flags and a permission sentence.

    Rendered straight from the view rather than from a completed run, so all
    three conditions can be checked -- including the one whose scripted
    responses make no tool call and therefore cannot complete under enforcement.
    """
    view = build_manager_view(make_state(condition_id=condition_id))
    text = "\n".join(message.content for message in build_manager_messages(view))
    assert CONDITION_NAME_RE.search(text) is None, text
    assert "source_access" in text


def test_the_manager_request_carries_no_hidden_shaped_keys(llm_runner, llm_case, make_state):
    llm_runner.run(make_state())
    for sent in llm_case.manager_client.requests:
        assert audit_keys(sent.model_dump(mode="json")) == ()


def test_the_manager_request_does_not_read_a_later_stage(
    make_llm_case, make_state, workflow, conditions
):
    """A state that already carries a handoff must not change the Manager's input.

    The Manager runs before Compliance, so this guards the reverse direction:
    nothing produced by a downstream stage can leak backwards into an upstream
    one, even when the state object happens to hold it.
    """
    manager_text = sample_case.manager_response_text()
    compliance_text = sample_case.compliance_response_text()
    case = make_llm_case(
        manager_responses=(manager_text, manager_text),
        compliance_responses=(compliance_text, compliance_text),
    )
    runner = case.runner(workflow, conditions)

    runner.run(make_state(run_id="clean"))
    baseline = rendered(case.manager_client, 0)

    seeded = make_state(run_id="seeded")
    seeded.manager_output = sample_case.manager_output(reason_summary="SENTINEL-HANDOFF")
    runner.run(seeded)
    later = rendered(case.manager_client, 1)

    assert "SENTINEL-HANDOFF" not in later
    assert later == baseline


# --------------------------------------------------------------------------
# 3-5. Compliance's restricted view, and what its request contains
# --------------------------------------------------------------------------


def test_the_compliance_node_requires_a_restricted_view():
    node = _llm_registry().get("compliance")
    assert node.requires_restricted_view is True
    assert node.output_model is ComplianceOutput


def test_the_compliance_node_refuses_anything_but_a_compliance_view(llm_case, make_state):
    node = llm_case.registry.get("compliance")
    with pytest.raises(ProtocolError, match="expected ComplianceInput"):
        node.run(make_state())


def test_the_compliance_is_passed_a_restricted_view_by_the_runner(llm_runner, make_state):
    outcome = llm_runner.run(make_state())
    created = [
        event
        for event in outcome.events
        if event.event_type is EventType.AGENT_INPUT_CREATED and event.node == "compliance"
    ]
    assert created
    assert created[0].payload["restricted"] is True


def test_the_compliance_request_contains_the_manager_handoff(llm_runner, llm_case, make_state):
    llm_runner.run(make_state())
    text = rendered(llm_case.compliance_client)
    assert "MANAGER HANDOFF" in text
    assert "clause_status" in text
    assert "POLICY-01" in text


def test_compliance_cannot_see_the_analyst_memo(llm_runner, llm_case, make_state):
    """Not merely absent from the rendering: unreachable from its input type."""
    llm_runner.run(make_state())
    text = rendered(llm_case.compliance_client)
    memo = sample_case.build_e0_memo()

    assert "ANALYST MEMO" not in text
    assert memo.memo_id not in text
    assert f"claims: {len(memo.claims)}" not in text
    for claim in memo.claims:
        assert claim.summary not in text
        assert f"CLAIM {claim.claim_id}" not in text
        if claim.claim_id != sample_case.TARGET_CLAIM_ID:
            assert claim.claim_id not in text

    # The target claim id *does* appear -- and that is correct. It is there
    # because the Manager cited it in the handoff it chose to send. Compliance
    # sees the Manager's citation, never the memo it came from.
    assert sample_case.TARGET_CLAIM_ID in text


def test_the_compliance_view_type_has_no_memo_field():
    assert "analyst_memo" not in ComplianceInput.model_fields
    assert "memo" not in ComplianceInput.model_fields
    assert "analyst_memo" in ManagerInput.model_fields


def test_building_a_compliance_view_never_reads_the_memo(make_state):
    """The builder's source is the guarantee; this is its serialized half."""
    state = make_state()
    state.manager_output = sample_case.manager_output()
    dumped = json.dumps(build_compliance_view(state).model_dump(mode="json"))

    assert sample_case.build_e0_memo().memo_id not in dumped
    assert "ANALYST MEMO" not in dumped


def test_compliance_cannot_see_hidden_gold(llm_runner, llm_case, make_state):
    llm_runner.run(make_state())
    text = rendered(llm_case.compliance_client)
    for sentinel in GOLD_SENTINELS:
        assert str(sentinel) not in text
    for pattern in FORBIDDEN_REQUEST_TEXT:
        assert pattern not in text, f"compliance request mentions {pattern!r}"


@pytest.mark.parametrize("condition_id", ["A0V0", "A1V0", "A1V1"])
def test_compliance_cannot_see_the_governance_condition(make_state, condition_id):
    """Same rule for the second agent: the flags, never the condition's name."""
    state = make_state(condition_id=condition_id)
    state.manager_output = sample_case.manager_output()
    view = build_compliance_view(state)
    text = "\n".join(message.content for message in build_compliance_messages(view))
    assert CONDITION_NAME_RE.search(text) is None, text
    assert "source_access" in text


def test_the_compliance_request_carries_no_hidden_shaped_keys(llm_runner, llm_case, make_state):
    llm_runner.run(make_state())
    for sent in llm_case.compliance_client.requests:
        assert audit_keys(sent.model_dump(mode="json")) == ()


def test_a_request_is_not_a_state_object(llm_runner, llm_case, make_state):
    """The request is its own type: it cannot smuggle the state alongside."""
    llm_runner.run(make_state())
    expected = {"messages", "params", "role", "prompt_version", "invocation", "run_id"}
    for client in (llm_case.manager_client, llm_case.compliance_client):
        for sent in client.requests:
            assert set(sent.model_dump(mode="json")) == expected


# --------------------------------------------------------------------------
# 6. Separate, fresh invocations
# --------------------------------------------------------------------------


def test_the_two_agents_use_separate_transports(llm_runner, llm_case, make_state):
    llm_runner.run(make_state())
    assert llm_case.manager_client.call_count == 1
    assert llm_case.compliance_client.call_count == 1
    assert llm_case.manager_client.requests[0].role == "manager"
    assert llm_case.compliance_client.requests[0].role == "compliance"


def test_neither_transport_is_shared_between_the_roles(llm_case):
    assert llm_case.manager_client is not llm_case.compliance_client


def test_the_manager_call_carries_no_assistant_turn(llm_runner, llm_case, make_state):
    llm_runner.run(make_state())
    roles = [message.role for message in llm_case.manager_client.requests[0].messages]
    assert roles == ["system", "user"]


def test_the_compliance_call_carries_no_assistant_turn(llm_runner, llm_case, make_state):
    llm_runner.run(make_state())
    roles = [message.role for message in llm_case.compliance_client.requests[0].messages]
    assert roles == ["system", "user"]


def test_each_invocation_builds_a_fresh_message_list(
    make_llm_case, make_state, workflow, conditions
):
    """Two runs produce byte-identical messages: no context is carried over."""
    manager_text = sample_case.manager_response_text()
    compliance_text = sample_case.compliance_response_text()
    case = make_llm_case(
        manager_responses=(manager_text, manager_text),
        compliance_responses=(compliance_text, compliance_text),
    )
    runner = case.runner(workflow, conditions)
    runner.run(make_state(run_id="run-a"))
    runner.run(make_state(run_id="run-b"))

    first, second = case.manager_client.requests
    assert first.messages == second.messages
    assert first.run_id == "run-a"
    assert second.run_id == "run-b"


def test_the_run_identifier_is_recorded_but_never_rendered(llm_runner, llm_case, make_state):
    llm_runner.run(make_state(run_id="run-xyz"))
    sent = llm_case.manager_client.requests[0]
    assert sent.run_id == "run-xyz"
    assert "run-xyz" not in sent.rendered_input()


# --------------------------------------------------------------------------
# 7-9. Structured output, repair, and failure
# --------------------------------------------------------------------------


def test_model_text_becomes_the_domain_object(make_llm_case, make_state, workflow, conditions):
    """The decision in the run is the decision the model wrote, not a fixture."""
    case = make_llm_case(
        manager_responses=(
            sample_case.manager_response_text(
                decision=Decision.ACCEPT, clause_status=ClauseStatus.ABSENT
            ),
        ),
        compliance_responses=(
            sample_case.compliance_response_text(
                decision=Decision.ACCEPT, clause_status=ClauseStatus.ABSENT
            ),
        ),
    )
    outcome = case.runner(workflow, conditions).run(make_state())

    assert isinstance(outcome.state.manager_output, ManagerOutput)
    assert isinstance(outcome.state.compliance_output, ComplianceOutput)
    assert outcome.state.manager_output.decision is Decision.ACCEPT
    assert outcome.state.compliance_output.decision is Decision.ACCEPT


def test_a_fenced_response_is_accepted_without_a_repair(
    make_llm_case, make_state, workflow, conditions
):
    case = make_llm_case(
        manager_responses=(sample_case.fenced(sample_case.manager_response_text()),),
        compliance_responses=(sample_case.fenced(sample_case.compliance_response_text()),),
    )
    outcome = case.runner(workflow, conditions).run(make_state())

    assert outcome.status is RunStatus.COMPLETED
    assert case.log.repairs() == ()


def test_one_malformed_response_is_repaired_and_the_run_completes(
    make_llm_case, make_state, workflow, conditions
):
    case = make_llm_case(
        manager_responses=("I think the clause is present.", sample_case.manager_response_text()),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    outcome = case.runner(workflow, conditions).run(make_state())

    assert outcome.status is RunStatus.COMPLETED
    assert case.manager_client.call_count == 2
    repairs = case.log.repairs()
    assert len(repairs) == 1
    assert repairs[0].role == "manager"
    assert repairs[0].repair_reason is not None
    assert repairs[0].prompt_version == (
        f"{load_manager_prompt().ref}+{load_repair_prompt().ref}"
    )


def test_the_repair_turn_carries_no_experimental_content(
    make_llm_case, make_state, workflow, conditions
):
    case = make_llm_case(
        manager_responses=("nonsense", sample_case.manager_response_text()),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    case.runner(workflow, conditions).run(make_state())

    repair = case.manager_client.requests[1]
    assert [message.role for message in repair.messages] == ["user"]
    text = repair.rendered_input()
    assert "nonsense" in text
    assert "POLICY-01" not in text
    assert sample_case.TARGET_CLAIM_ID not in text
    assert "change_of_control" not in text
    assert "ANALYST MEMO" not in text


def test_a_failed_repair_aborts_the_run_as_a_protocol_error(
    make_llm_case, make_state, workflow, conditions
):
    case = make_llm_case(
        manager_responses=("nonsense", "still nonsense"),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    runner = case.runner(workflow, conditions)
    state = make_state()

    with pytest.raises(ProtocolError, match="produced no usable ManagerOutput"):
        runner.run(state)

    assert state.status is RunStatus.FAILED
    assert state.manager_output is None
    assert case.manager_client.call_count == 2  # one attempt, one repair, no more

    errors = [
        event for event in runner._collector.events if event.event_type is EventType.PROTOCOL_ERROR
    ]
    assert errors and errors[0].node == "manager"


def test_a_transport_failure_aborts_the_run_as_a_protocol_error(make_state, workflow, conditions):
    registry = build_llm_registry(
        repository=_repository(),
        models=load_models_v1(),
        manager_client=ScriptedModelClient((sample_case.manager_response_text(),)),
        compliance_client=_RefusingClient("provider unreachable"),
        call_log=ModelCallLog(),
        **_source_collaborators(),
    )
    runner = WorkflowRunner(
        workflow=workflow, conditions=conditions, registry=registry, clock=_clock
    )

    with pytest.raises(ProtocolError, match="compliance model call failed"):
        runner.run(make_state())


def test_no_field_is_invented_when_the_model_omits_one(
    make_llm_case, make_state, workflow, conditions
):
    """``reason_summary`` is required. The node fails; it does not supply one.

    (Fields that the schema declares with a default -- ``uncertainties``,
    ``evidence_ids``, ``role`` -- are a different matter: their default *is* the
    schema's answer, and the model is entitled to rely on it.)
    """
    incomplete = json.loads(sample_case.manager_response_text())
    del incomplete["reason_summary"]
    case = make_llm_case(
        manager_responses=(json.dumps(incomplete), json.dumps(incomplete)),
        compliance_responses=(sample_case.compliance_response_text(),),
    )

    with pytest.raises(ProtocolError, match="produced no usable ManagerOutput"):
        case.runner(workflow, conditions).run(make_state())


def test_a_schema_default_is_not_an_invented_value(
    make_llm_case, make_state, workflow, conditions
):
    """The complement of the test above: an omitted defaulted field is fine."""
    minimal = json.loads(sample_case.manager_response_text())
    del minimal["uncertainties"]
    case = make_llm_case(
        manager_responses=(json.dumps(minimal),),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    outcome = case.runner(workflow, conditions).run(make_state())

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.state.manager_output.uncertainties == ()
    assert case.log.repairs() == ()


def test_an_extra_field_is_not_silently_dropped(make_llm_case, make_state, workflow, conditions):
    """A model that invents a field is a protocol failure, not a rounding error."""
    inflated = json.loads(sample_case.manager_response_text())
    inflated["gold_action"] = "ACCEPT"
    case = make_llm_case(
        manager_responses=(json.dumps(inflated), json.dumps(inflated)),
        compliance_responses=(sample_case.compliance_response_text(),),
    )

    with pytest.raises(ProtocolError, match="produced no usable ManagerOutput"):
        case.runner(workflow, conditions).run(make_state())


# --------------------------------------------------------------------------
# 10. No Python derivation from gold
# --------------------------------------------------------------------------

NODE_CODE_FILES = ("manager.py", "compliance.py", "llm_agent.py", "render.py")

FORBIDDEN_IDENTIFIERS = (
    "gold",
    "gold_action",
    "gold_clause_status",
    "gold_evidence_offsets",
    "hidden",
    "expected_decision",
    "error_condition",
    "condition_id",
)

FORBIDDEN_STRING_FRAGMENTS = (
    "gold",
    "expected_decision",
    "error_condition",
    "hidden_gold",
)
"""Narrower than the identifier list: a node's error text may say
"hidden-shaped", so only genuinely gold-shaped literals are refused here."""


def _neutralise_docstrings(tree: ast.Module) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body[0].value.value = ""


def _parsed(filename: str) -> ast.Module:
    tree = ast.parse((NODES_DIR / filename).read_text(encoding="utf-8"))
    _neutralise_docstrings(tree)
    return tree


@pytest.mark.parametrize("filename", NODE_CODE_FILES)
def test_no_agent_node_code_names_a_hidden_value(filename):
    """The Gate 1.5 lesson, enforced statically over the real agents."""
    names: set[str] = set()
    for node in ast.walk(_parsed(filename)):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.arg):
            names.add(node.arg)

    for forbidden in FORBIDDEN_IDENTIFIERS:
        assert forbidden not in names, f"{filename} references {forbidden!r}"


@pytest.mark.parametrize("filename", NODE_CODE_FILES)
def test_no_agent_node_code_contains_a_gold_shaped_literal(filename):
    for node in ast.walk(_parsed(filename)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for fragment in FORBIDDEN_STRING_FRAGMENTS:
                assert fragment not in node.value, (
                    f"{filename} contains the literal {node.value!r}, which names {fragment!r}"
                )


def test_the_real_agents_do_not_import_the_gold_path():
    for filename in NODE_CODE_FILES:
        source = (NODES_DIR / filename).read_text(encoding="utf-8")
        for module in ("..state", "..export", "expected_decision"):
            assert module not in source, f"{filename} imports {module!r}"


def test_the_same_model_response_yields_the_same_output_on_both_arms(
    make_llm_case, make_state, workflow, conditions
):
    """Whatever the model says, Python carries it through unchanged.

    The E0 and E1 requests differ -- the memo is different -- but the recorded
    outputs are identical, because nothing between the response and the state
    reads the treatment.
    """
    manager_text = sample_case.manager_response_text(
        decision=Decision.ACCEPT, clause_status=ClauseStatus.ABSENT
    )
    compliance_text = sample_case.compliance_response_text(
        decision=Decision.ACCEPT, clause_status=ClauseStatus.ABSENT
    )
    case = make_llm_case(
        manager_responses=(manager_text, manager_text),
        compliance_responses=(compliance_text, compliance_text),
    )
    runner = case.runner(workflow, conditions)

    e0 = runner.run(make_state(error_condition=ErrorCondition.E0, run_id="e0"))
    e1 = runner.run(make_state(error_condition=ErrorCondition.E1, run_id="e1"))

    assert e0.state.manager_output == e1.state.manager_output
    assert e0.state.compliance_output == e1.state.compliance_output
    assert e0.state.compliance_output.decision is Decision.ACCEPT

    # ... while the requests genuinely differ, so the arms are not one run twice.
    first, second = case.manager_client.requests
    assert first.messages != second.messages


def test_gold_does_not_override_what_the_model_decided(
    make_llm_case, make_state, workflow, conditions
):
    """The fixture's gold says ESCALATE; the model says ACCEPT; ACCEPT stands.

    This is the strongest form of requirement 10: if any Python code were
    reconciling the output against the expected answer, this run would end
    ESCALATE.
    """
    case = make_llm_case(
        manager_responses=(
            sample_case.manager_response_text(
                decision=Decision.ACCEPT, clause_status=ClauseStatus.ABSENT
            ),
        ),
        compliance_responses=(
            sample_case.compliance_response_text(
                decision=Decision.ACCEPT, clause_status=ClauseStatus.ABSENT
            ),
        ),
    )
    state = make_state(gold_status=ClauseStatus.PRESENT)
    assert state.hidden.gold.gold_action is Decision.ESCALATE

    outcome = case.runner(workflow, conditions).run(state)

    assert outcome.state.compliance_output.decision is Decision.ACCEPT
    assert outcome.status is RunStatus.COMPLETED


@pytest.mark.parametrize(
    ("decision", "status"),
    ((Decision.ESCALATE, ClauseStatus.PRESENT), (Decision.ACCEPT, ClauseStatus.ABSENT)),
)
def test_the_decision_follows_the_model_and_not_a_default(
    make_llm_case, make_state, workflow, conditions, decision, status
):
    case = make_llm_case(
        manager_responses=(
            sample_case.manager_response_text(decision=decision, clause_status=status),
        ),
        compliance_responses=(
            sample_case.compliance_response_text(decision=decision, clause_status=status),
        ),
    )
    outcome = case.runner(workflow, conditions).run(make_state())
    assert outcome.state.compliance_output.decision is decision


def test_the_fixture_gold_is_present_and_visible_only_on_the_state(llm_runner, make_state):
    """The gold exists, so the tests above are not vacuously passing."""
    outcome = llm_runner.run(make_state())
    assert outcome.state.hidden.gold.gold_evidence_offsets == GOLD_SENTINELS


# --------------------------------------------------------------------------
# 17. Prompt version recorded per call
# --------------------------------------------------------------------------


def test_every_model_call_records_its_prompt_version(llm_runner, llm_case, make_state):
    llm_runner.run(make_state())
    by_role = {record.role: record.prompt_version for record in llm_case.log.records}
    assert by_role == {
        "manager": load_manager_prompt().ref,
        "compliance": load_compliance_prompt().ref,
    }


def test_the_prompt_version_matches_the_file_that_was_used(llm_runner, llm_case, make_state):
    llm_runner.run(make_state())
    versions = {record.role: record.prompt_version for record in llm_case.log.records}
    assert versions["manager"] == load_manager_prompt().ref
    assert versions["compliance"] == load_compliance_prompt().ref


def test_the_rendered_system_prompt_comes_from_the_versioned_file(
    llm_runner, llm_case, make_state
):
    llm_runner.run(make_state())
    system = llm_case.manager_client.requests[0].messages[0]
    assert system.role == "system"
    assert "Manager agent" in system.content
    assert "{{policy}}" not in system.content
    assert "policy_id: POLICY-01" in system.content


# --------------------------------------------------------------------------
# Renderer signatures: the view is the only thing an agent can see
# --------------------------------------------------------------------------


@pytest.mark.parametrize("renderer", [build_manager_messages, build_compliance_messages])
def test_a_role_renderer_takes_exactly_one_argument(renderer):
    """The signature is the isolation boundary; widening it must be deliberate."""
    parameters = list(inspect.signature(renderer).parameters.values())
    assert len(parameters) == 1
    assert parameters[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_the_manager_renderer_accepts_a_hand_built_view(make_state):
    """No state, no repository, no runner: a view is sufficient."""
    messages = build_manager_messages(build_manager_view(make_state()))
    assert [message.role for message in messages] == ["system", "user"]


def test_the_compliance_renderer_accepts_a_hand_built_view(make_state):
    state = make_state()
    state.manager_output = sample_case.manager_output()
    messages = build_compliance_messages(build_compliance_view(state))
    assert [message.role for message in messages] == ["system", "user"]


# --------------------------------------------------------------------------
# 15. The fakes are untouched
# --------------------------------------------------------------------------


def test_the_fake_registry_still_replays_its_declared_script(runner, make_state, manager_script):
    outcome = runner.run(make_state())
    assert outcome.state.manager_output == manager_script.outputs[0]
    assert outcome.state.compliance_output == sample_case.compliance_output()


def test_the_fake_registry_needs_no_model_client(repository):
    """The scripted wiring is constructible with no transport at all."""
    registry = build_registry(
        repository=repository,
        manager_script=FakeAgentScript(outputs=(sample_case.manager_output(),)),
        compliance_script=FakeAgentScript(outputs=(sample_case.compliance_output(),)),
    )
    assert set(registry.names()) == {"analyst", "manager", "compliance"}
    assert registry.get("manager").run.__self__.__class__.__name__ == "FakeManager"
    assert registry.get("compliance").run.__self__.__class__.__name__ == "FakeCompliance"


def test_the_two_registries_are_distinct_wirings(repository):
    scripted = build_registry(
        repository=repository,
        manager_script=FakeAgentScript(outputs=(sample_case.manager_output(),)),
        compliance_script=FakeAgentScript(outputs=(sample_case.compliance_output(),)),
    )
    model_backed = _llm_registry()

    assert set(scripted.names()) == set(model_backed.names())
    assert scripted.get("manager").run != model_backed.get("manager").run
    assert model_backed.get("manager").output_model is ManagerOutput
    assert model_backed.get("manager").build_input.__qualname__.startswith("make_llm_agent_node")


def test_building_the_llm_registry_requires_every_collaborator():
    with pytest.raises(TypeError):
        build_llm_registry(repository=_repository())  # type: ignore[call-arg]


def test_a_transport_is_never_constructed_implicitly(llm_runner, llm_case, make_state):
    """Nothing in the run creates a second client behind the caller's back."""
    llm_runner.run(make_state())
    assert llm_case.manager_client.call_count == 1
    assert llm_case.compliance_client.call_count == 1


def test_the_runner_is_unchanged_by_the_substitution(llm_runner, runner):
    assert type(llm_runner) is type(runner)
    assert llm_runner.workflow is runner.workflow
    assert llm_runner._conditions is runner._conditions


def test_both_wirings_produce_the_same_outcome_shape(llm_runner, runner, make_state):
    scripted = runner.run(make_state())
    model_backed = llm_runner.run(make_state())

    assert type(scripted) is type(model_backed)
    assert scripted.steps_executed == model_backed.steps_executed
    assert scripted.final_node == model_backed.final_node


# --------------------------------------------------------------------------
# 19. Gate 1.5 invariants
# --------------------------------------------------------------------------


def test_the_restricted_views_are_unchanged():
    assert set(ManagerInput.model_fields) == {
        "case_id",
        "policy",
        "analyst_memo",
        "source_access",
        "verification_required",
        "invocation",
    }
    assert set(ComplianceInput.model_fields) == {
        "case_id",
        "policy",
        "manager_output",
        "source_access",
        "verification_required",
        "invocation",
    }


def test_a_view_class_that_declares_a_hidden_field_is_rejected_at_definition():
    with pytest.raises(ViewLeakError):

        class Leaky(ManagerInput):
            gold_action: str = "ACCEPT"


def test_a_view_class_that_declares_a_memo_field_on_compliance_is_rejected():
    with pytest.raises(ViewLeakError):

        class Leaky(ComplianceInput):
            analyst_memo: str = ""


def test_the_view_audit_still_passes_a_clean_view(make_state):
    assert_view_clean(build_manager_view(make_state()))


def test_a_clean_request_passes_the_same_audit_a_view_does(llm_runner, llm_case, make_state):
    llm_runner.run(make_state())
    for client in (llm_case.manager_client, llm_case.compliance_client):
        for sent in client.requests:
            assert audit_keys(sent.model_dump(mode="json")) == ()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class _RefusingClient:
    """A transport that always fails, to prove the node translates the failure."""

    def __init__(self, message: str) -> None:
        self._message = message

    def generate(self, request):  # noqa: ANN001, ANN201 - protocol shape
        raise ModelClientError(self._message)


def _llm_registry():
    return build_llm_registry(
        repository=_repository(),
        models=load_models_v1(),
        manager_client=ScriptedModelClient((sample_case.manager_response_text(),)),
        compliance_client=ScriptedModelClient((sample_case.compliance_response_text(),)),
        call_log=ModelCallLog(),
        **_source_collaborators(),
    )


def _source_collaborators() -> dict:
    """The source layer a model-backed registry now requires, freshly built.

    Returned as a dict so a test can build a registry inline without repeating
    four lines of wiring, and so the *same* objects are handed to the registry
    that the test then inspects.
    """
    return {
        "source": sample_case.build_source_library(),
        "tool_log": ToolCallLog(),
        "verification_log": VerificationLog(),
    }


def _repository() -> FrozenMemoRepository:
    return FrozenMemoRepository.from_e0_memo(
        sample_case.build_e0_memo(),
        target_claim_id=sample_case.TARGET_CLAIM_ID,
        expected_category=sample_case.TARGET_CATEGORY,
    )[0]


def _clock() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
