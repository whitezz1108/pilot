"""Experimental-validity isolation tests.

The claim under test is strong: **no** hidden experimental data can reach
Manager or Compliance. It is checked at three levels, because a naming
convention alone proves nothing:

1. field names declared on the view models;
2. every key path in the *serialized* view (catches nested leakage);
3. the actual serialized text, scanned for treatment labels, gold sentinel
   values, and memo content.

Plus a behavioural check that the runner hands agent nodes a view rather than
the state, and refuses to run at all if a node is wired to the state.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from conftest import GOLD_OFFSET_SENTINELS
from pilot01.schemas import ErrorCondition
from pilot01.workflow.runner import WorkflowRunner
from pilot01.workflow.state import ExperimentState
from pilot01.workflow.transitions import Node, NodeRegistry, NodeResult, ProtocolError
from pilot01.workflow.views import (
    ComplianceInput,
    ManagerInput,
    RestrictedView,
    ViewLeakError,
    _key_violations,
    assert_view_clean,
    audit_view,
    build_compliance_view,
    build_manager_view,
)

FORBIDDEN_SUBSTRINGS = (
    "gold",
    "hidden",
    "omission",
    "error_condition",
    "condition_id",
    "treatment",
    "e0",
    "e1",
)


def string_values(value):
    """Every string value anywhere in a serialized structure."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from string_values(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from string_values(nested)


def serialized(view: RestrictedView) -> dict:
    return view.model_dump(mode="json")


def text_of(view: RestrictedView) -> str:
    return json.dumps(serialized(view), sort_keys=True)


# -- key-level: nothing hidden is even declared ----------------------------


def test_gold_never_visible_to_manager(make_state):
    state = make_state("A1V1", ErrorCondition.E1)
    view = build_manager_view(state)

    assert audit_view(view) == ()
    assert not any("gold" in name for name in ManagerInput.model_fields)
    for name in ManagerInput.model_fields:
        assert not any(pattern in name.lower() for pattern in FORBIDDEN_SUBSTRINGS)


def test_gold_never_visible_to_compliance(make_state, runner):
    state = make_state("A1V1", ErrorCondition.E1)
    runner.run(state)
    view = build_compliance_view(state)

    assert audit_view(view, extra_forbidden=("memo", "analyst")) == ()
    assert not any("gold" in name for name in ComplianceInput.model_fields)


def test_gold_values_never_appear_in_a_serialized_view(make_state, runner):
    """Value-level scan: the gold offsets are distinctive, so they are findable."""
    state = make_state("A1V1", ErrorCondition.E0)
    manager_text = text_of(build_manager_view(state))
    runner.run(state)
    compliance_text = text_of(build_compliance_view(state))

    for sentinel in GOLD_OFFSET_SENTINELS:
        assert str(sentinel) not in manager_text
        assert str(sentinel) not in compliance_text
    assert state.hidden.gold.gold_evidence_offsets == GOLD_OFFSET_SENTINELS


def test_error_condition_not_visible_to_agents(make_state, runner):
    """Neither E0 nor E1 may be recoverable from an agent-facing input."""
    e0_state = make_state("A1V1", ErrorCondition.E0)
    e1_state = make_state("A1V1", ErrorCondition.E1)

    for state in (e0_state, e1_state):
        manager_values = set(string_values(serialized(build_manager_view(state))))
        assert not (manager_values & {"E0", "E1"})

    runner.run(e0_state)
    runner.run(e1_state)
    for state in (e0_state, e1_state):
        compliance_values = set(string_values(serialized(build_compliance_view(state))))
        assert not (compliance_values & {"E0", "E1"})


def test_condition_id_not_visible_to_agents(make_state):
    state = make_state("A1V1", ErrorCondition.E0)
    values = set(string_values(serialized(build_manager_view(state))))
    assert "A1V1" not in values
    assert not any(value.startswith("A0V") or value.startswith("A1V") for value in values)


def test_a_treatment_encoding_run_id_cannot_reach_a_view(make_state, runner):
    """A run id like ``case01_E1_A1V1_rep3`` must not be able to leak either.

    Gate 2 harnesses routinely name runs after their cell. Identifiers are
    therefore kept out of agent-facing inputs entirely, rather than by a naming
    convention that a future caller could break.
    """
    encoded = "case01_E1_A1V1_rep3"
    state = make_state("A1V1", ErrorCondition.E1, run_id=encoded)
    manager_text = text_of(build_manager_view(state))
    runner.run(state)
    compliance_text = text_of(build_compliance_view(state))

    assert encoded not in manager_text
    assert encoded not in compliance_text
    assert "A1V1" not in manager_text and "A1V1" not in compliance_text


def test_manager_view_carries_only_the_permitted_fields(make_state):
    """The whitelist is exact -- additions must be deliberate, not incidental."""
    assert set(ManagerInput.model_fields) == {
        "case_id",
        "policy",
        "analyst_memo",
        "source_access",
        "verification_required",
        "invocation",
    }


def test_compliance_view_carries_only_the_permitted_fields():
    assert set(ComplianceInput.model_fields) == {
        "case_id",
        "policy",
        "manager_output",
        "source_access",
        "verification_required",
        "invocation",
    }


# -- compliance role isolation --------------------------------------------


def test_compliance_cannot_see_analyst_memo(make_state, runner):
    state = make_state("A1V1", ErrorCondition.E0)
    runner.run(state)
    view = build_compliance_view(state)

    assert not hasattr(view, "analyst_memo")
    assert "analyst_memo" not in serialized(view)
    assert "analyst_memo" not in text_of(view)


def test_compliance_view_does_not_contain_memo_content(make_state, runner):
    """Not just the field: none of the memo's claim text may reach Compliance.

    Only the memo's substantive text is checked. Two things are excluded on
    purpose:

    * the memo *id*, which is identical in both arms (asserted in
      ``test_conditions``) and so carries no treatment signal;
    * claim ids and source ids, which the Manager forwards as
      ``adopted_upstream_claim_ids`` / ``evidence_ids`` -- that is the designed
      handoff, and any treatment dependence there is a downstream consequence
      of the Manager's own output, not an input leak.
    """
    state = make_state("A1V1", ErrorCondition.E0)
    memo_texts = [claim.summary for claim in state.analyst_memo.claims]
    runner.run(state)
    view_text = text_of(build_compliance_view(state))

    for text in memo_texts:
        assert text not in view_text


def test_compliance_view_cannot_be_built_without_a_manager_handoff(make_state):
    state = make_state("A1V1", ErrorCondition.E0)
    assert state.manager_output is None
    with pytest.raises(ProtocolError, match="no manager output"):
        build_compliance_view(state)


def test_compliance_view_carries_no_conversation_history(make_state, runner):
    state = make_state("A1V1", ErrorCondition.E0)
    runner.run(state)
    view = build_compliance_view(state)
    for name in ComplianceInput.model_fields:
        assert not any(
            token in name.lower() for token in ("history", "message", "transcript", "turn")
        )
    assert audit_view(view, extra_forbidden=("history", "message", "transcript")) == ()


# -- the treatment signal is confined to the memo -------------------------


def test_manager_views_differ_between_error_conditions_only_in_the_memo(make_state):
    """E0 and E1 must not be distinguishable by any channel other than the memo."""
    e0 = serialized(build_manager_view(make_state("A1V0", ErrorCondition.E0)))
    e1 = serialized(build_manager_view(make_state("A1V0", ErrorCondition.E1)))

    assert e0 != e1, "fixture assumption: the two arms must differ somewhere"
    assert e0.pop("analyst_memo") != e1.pop("analyst_memo")
    assert e0 == e1


# -- the whole design matrix, audited at once ------------------------------


@pytest.mark.parametrize("condition_id", ["A0V0", "A1V0", "A1V1"])
@pytest.mark.parametrize("error_condition", [ErrorCondition.E0, ErrorCondition.E1])
def test_no_hidden_data_in_any_cell_view(
    make_state, runner, condition_id, error_condition
):
    """Full audit over the 3 x 2 design matrix.

    For every cell: neither agent view may contain hidden keys, gold sentinel
    values, treatment labels, or (for Compliance) the analyst memo.
    """
    state = make_state(condition_id, error_condition)
    manager_view = build_manager_view(state)
    runner.run(state)
    compliance_view = build_compliance_view(state)

    assert audit_view(manager_view) == ()
    assert audit_view(compliance_view, extra_forbidden=("memo", "analyst")) == ()

    for view in (manager_view, compliance_view):
        payload = serialized(view)
        text = json.dumps(payload)
        assert "gold" not in text
        assert not (set(string_values(payload)) & {"E0", "E1", condition_id})
        for sentinel in GOLD_OFFSET_SENTINELS:
            assert str(sentinel) not in text

    assert "analyst_memo" in serialized(manager_view)  # permitted for the Manager
    assert "analyst_memo" not in serialized(compliance_view)  # never for Compliance


# -- the guard rails themselves work --------------------------------------


def test_restricted_view_rejects_a_hidden_field_at_class_definition():
    with pytest.raises(ViewLeakError, match="gold"):

        class LeakyManagerInput(RestrictedView):
            gold_action: str


def test_restricted_view_rejects_a_treatment_field_at_class_definition():
    with pytest.raises(ViewLeakError, match="error_condition"):

        class LeakyComplianceInput(RestrictedView):
            error_condition: str


def test_audit_view_detects_a_nested_leak():
    class PayloadView(RestrictedView):
        payload: dict

    view = PayloadView(payload={"nested": {"gold_action": "ESCALATE"}})
    violations = audit_view(view)
    assert violations and "gold" in violations[0]
    with pytest.raises(ViewLeakError):
        assert_view_clean(view)


def test_views_are_frozen_and_closed(make_state):
    view = build_manager_view(make_state("A0V0"))
    with pytest.raises(ValidationError):
        view.case_id = "CASE-999"
    with pytest.raises(ValidationError):
        ManagerInput(
            case_id="CASE-001",
            policy=view.policy,
            analyst_memo=view.analyst_memo,
            source_access=False,
            verification_required=False,
            gold_action="ESCALATE",
        )


# -- behavioural: what nodes actually receive ------------------------------


def test_agent_nodes_receive_restricted_views_not_full_state(
    make_state, workflow, conditions, make_registry
):
    received: list[tuple[str, object]] = []
    real = make_registry()

    def spy(name: str) -> Node:
        node = real.get(name)

        def run(node_input):
            received.append((name, node_input))
            return node.run(node_input)

        return Node(
            name=node.name,
            build_input=node.build_input,
            run=run,
            apply=node.apply,
            requires_restricted_view=node.requires_restricted_view,
            output_model=node.output_model,
        )

    registry = NodeRegistry([real.get("analyst"), spy("manager"), spy("compliance")])
    runner = WorkflowRunner(workflow=workflow, conditions=conditions, registry=registry)
    runner.run(make_state("A1V1"))

    kinds = dict(received)
    assert set(kinds) == {"manager", "compliance"}
    assert isinstance(kinds["manager"], ManagerInput)
    assert isinstance(kinds["compliance"], ComplianceInput)
    for name, node_input in received:
        assert not isinstance(node_input, ExperimentState), name
        assert isinstance(node_input, RestrictedView), name


def test_runner_refuses_to_hand_the_full_state_to_an_agent_node(
    make_state, workflow, conditions, make_registry
):
    real = make_registry()
    rogue = Node(
        name="manager",
        build_input=lambda state, invocation: state,
        run=lambda node_input: NodeResult(),
        apply=lambda state, output: None,
        requires_restricted_view=True,
    )
    registry = NodeRegistry([real.get("analyst"), rogue, real.get("compliance")])
    runner = WorkflowRunner(workflow=workflow, conditions=conditions, registry=registry)

    with pytest.raises(ProtocolError, match="must receive a RestrictedView"):
        runner.run(make_state("A0V0"))


def test_runner_refuses_a_restricted_view_for_the_artifact_loader(
    make_state, workflow, conditions, make_registry
):
    """The analyst loads artifacts; it is not an agent and must not be treated as one."""
    real = make_registry()
    analyst = real.get("analyst")
    rogue = Node(
        name="analyst",
        build_input=lambda state, invocation: build_manager_view(state),
        run=analyst.run,
        apply=analyst.apply,
        requires_restricted_view=False,
        output_model=analyst.output_model,
    )
    registry = NodeRegistry([rogue, real.get("manager"), real.get("compliance")])
    runner = WorkflowRunner(workflow=workflow, conditions=conditions, registry=registry)

    with pytest.raises(ProtocolError, match="artifact-loader"):
        runner.run(make_state("A0V0"))


def test_fresh_context_per_agent_call(make_state, runner):
    """Rule D: each agent call gets a new, frozen view; no shared context object."""
    state = make_state("A1V1")
    first = build_manager_view(state, invocation=0)
    second = build_manager_view(state, invocation=1)
    assert first is not second
    assert first.invocation == 0
    assert second.invocation == 1
    # Nothing mutable is shared between successive calls.
    assert isinstance(first, RestrictedView) and isinstance(second, RestrictedView)


def test_forbidden_key_scanner_is_precise():
    """Guard the guard: it must flag hidden data without flagging legitimate fields."""
    for benign in (
        "case_id",
        "analyst_memo",
        "manager_output",
        "source_access",
        "verification_required",
        "invocation",
        "adopted_upstream_claim_ids",
        "evidence_ids",
    ):
        assert _key_violations(benign) == (), benign
    for hidden in ("gold_action", "gold_evidence_offsets", "error_condition", "E1", "hidden"):
        assert _key_violations(hidden), hidden
