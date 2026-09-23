"""Gate 2.5, group 5: where each agent's evidence came from.

The pilot's dependent variable is *evidence adoption*: does an agent lean on
material it was handed, or does it go and check? That question is only
answerable if the record distinguishes the two, so this file is about the
distinction rather than about any one run.

Four claims, and they are separate claims:

1. **Access is node-specific.** The Manager's tool calls are the Manager's.
   Compliance does not inherit them, and cannot be credited with them.
2. **Upstream-cited is not self-verified.** An id that arrived in the memo or
   the handoff is legitimate provenance and is *not* verification. Collapsing
   the two would let a node discharge a verification obligation it never
   discharged.
3. **The ledger is reconstructible.** Everything the run concluded about a
   node's access can be recomputed from the tool log, which is what makes the
   conclusion auditable rather than merely asserted.
4. **A cited id must have been observed.** An id in a final output that the node
   could not have obtained from anywhere it was allowed to look was fabricated,
   whatever the node said about it.
"""

from __future__ import annotations

import pytest

import sample_case
from pilot01.config import load_conditions_v1, load_models_v1, load_workflow_v1
from pilot01.model import ModelCallLog, ScriptedModelClient
from pilot01.schemas import ClauseStatus, ErrorCondition, VerificationStatus
from pilot01.source import (
    EvidenceClass,
    SourceLibrary,
    ToolCallLog,
    VerificationLog,
    evidence_access,
    replay_ledger,
)
from pilot01.source.tools import SourceTools
from pilot01.workflow.nodes import FrozenMemoRepository, build_llm_registry
from pilot01.workflow.nodes.compliance import compliance_upstream_ids
from pilot01.workflow.nodes.manager import manager_upstream_ids
from pilot01.workflow.runner import WorkflowRunner
from pilot01.workflow.state import ExperimentState
from pilot01.workflow.transitions import ProtocolError
from pilot01.workflow.views import build_compliance_view, build_manager_view

TARGET_PARAGRAPH = sample_case.paragraph_id_containing(sample_case.CHANGE_OF_CONTROL_PHRASE)
LIABILITY_PARAGRAPH = sample_case.paragraph_id_containing(sample_case.LIABILITY_CAP_PHRASE)
GOVERNING_LAW_PARAGRAPH = sample_case.paragraph_id_containing(
    sample_case.GOVERNING_LAW_PHRASE
)

MEMO_SOURCE_ID = sample_case.TARGET_CLAIM_SOURCE_IDS[0]
"""A source id the memo cites: legitimate to cite, never evidence of searching."""


def search(query: str = "change of control") -> str:
    return sample_case.tool_request_text("search_contract", query=query)


def open_span(paragraph_id: str) -> str:
    return sample_case.tool_request_text("open_source_span", paragraph_id=paragraph_id)


def repository() -> FrozenMemoRepository:
    return FrozenMemoRepository.from_e0_memo(
        sample_case.build_e0_memo(),
        target_claim_id=sample_case.TARGET_CLAIM_ID,
        expected_category=sample_case.TARGET_CATEGORY,
    )[0]


def omission_record():
    return FrozenMemoRepository.from_e0_memo(
        sample_case.build_e0_memo(),
        target_claim_id=sample_case.TARGET_CLAIM_ID,
        expected_category=sample_case.TARGET_CATEGORY,
    )[1]


class Run:
    """One scripted run whose tool log, ledgers and state a test can inspect."""

    def __init__(
        self,
        *,
        condition_id: str = "A1V0",
        error_condition: ErrorCondition = ErrorCondition.E0,
        manager_responses: tuple[str, ...],
        compliance_responses: tuple[str, ...],
        source: SourceLibrary | None = None,
    ) -> None:
        self.condition_id = condition_id
        self.error: ProtocolError | None = None
        self.tool_log = ToolCallLog()
        self.verification_log = VerificationLog()
        self.call_log = ModelCallLog()
        self.repository = repository()
        self.conditions = load_conditions_v1()
        self.workflow = load_workflow_v1()
        self.registry = build_llm_registry(
            repository=self.repository,
            models=load_models_v1(),
            manager_client=ScriptedModelClient(manager_responses),
            compliance_client=ScriptedModelClient(compliance_responses),
            call_log=self.call_log,
            source=source if source is not None else sample_case.build_source_library(),
            tool_log=self.tool_log,
            verification_log=self.verification_log,
        )
        self.state = ExperimentState.create(
            run_id="run-0001",
            case_id=sample_case.CASE_ID,
            repetition_id=0,
            condition_id=condition_id,
            conditions=self.conditions,
            error_condition=error_condition,
            contract_id=sample_case.CONTRACT_ID,
            contract_text_hash=sample_case.CONTRACT_TEXT_HASH,
            target_category=sample_case.TARGET_CATEGORY,
            memo=self.repository.load(sample_case.CASE_ID, error_condition),
            gold_target_clause_status={sample_case.TARGET_CATEGORY: ClauseStatus.PRESENT, sample_case.OTHER_CLAUSE_CATEGORY: ClauseStatus.ABSENT},
            policy=sample_case.build_policy(),
            gold_evidence_offsets=(987654321,),
            omission=None if error_condition is ErrorCondition.E0 else omission_record(),
        )

    def go(self) -> ExperimentState:
        runner = WorkflowRunner(
            workflow=self.workflow,
            conditions=self.conditions,
            registry=self.registry,
        )
        try:
            runner.run(self.state)
        except ProtocolError as exc:
            self.error = exc
        return self.state

    def access(self, node: str):
        upstream = ()
        if node == "manager":
            upstream = manager_upstream_ids(build_manager_view(self.state))
        elif node == "compliance" and self.state.manager_output is not None:
            upstream = compliance_upstream_ids(build_compliance_view(self.state))
        return evidence_access(self.tool_log.records, node=node, upstream=upstream)


# --------------------------------------------------------------------------
# The two upstream sets are read from the artifacts, not invented
# --------------------------------------------------------------------------


def test_the_manager_upstream_ids_are_the_memo_source_ids(make_state):
    view = build_manager_view(make_state(condition_id="A1V0"))
    expected = tuple(
        dict.fromkeys(
            source_id for claim in view.analyst_memo.claims for source_id in claim.source_ids
        )
    )
    assert manager_upstream_ids(view) == expected
    assert expected, "the fixture memo cites no source ids; this test would be vacuous"


def test_the_compliance_upstream_ids_are_the_handoff_evidence_ids(make_state):
    """Compliance's upstream set is the Manager's *output*, not the Manager's tools."""
    state = make_state(condition_id="A1V0")
    state.manager_output = sample_case.manager_output(evidence_ids=("X-1", "X-2"))
    view = build_compliance_view(state)
    assert compliance_upstream_ids(view) == ("X-1", "X-2")


def test_the_two_upstream_sets_are_different_objects():
    """Guard the guard: the Manager's route is the memo, Compliance's is the handoff."""
    assert manager_upstream_ids is not compliance_upstream_ids


# --------------------------------------------------------------------------
# 1. Access is node-specific
# --------------------------------------------------------------------------


def test_the_manager_and_compliance_do_not_share_a_ledger():
    run = Run(
        manager_responses=(search(), open_span(TARGET_PARAGRAPH), sample_case.manager_response_text()),
        compliance_responses=(
            search("governing law"),
            open_span(GOVERNING_LAW_PARAGRAPH),
            sample_case.compliance_response_text(),
        ),
    )
    run.go()
    manager = run.access("manager")
    compliance = run.access("compliance")
    assert manager.searches == ("change of control",)
    assert compliance.searches == ("governing law",)
    assert manager.opened_ids == (TARGET_PARAGRAPH,)
    assert compliance.opened_ids == (GOVERNING_LAW_PARAGRAPH,)
    # Neither node is credited with the other's *own* work. The two nodes do
    # share a contract, so a broad enough search can surface the same paragraph
    # for both -- which is why the claim is made about what each node did
    # (searched, opened), not about the union of what either could see.
    assert set(manager.opened_ids) & set(compliance.opened_ids) == set()
    assert TARGET_PARAGRAPH not in compliance.opened_ids
    assert GOVERNING_LAW_PARAGRAPH not in manager.opened_ids
    assert manager.upstream_ids != compliance.upstream_ids


def test_compliance_does_not_inherit_the_managers_tool_history():
    """The Manager searched; Compliance did not. Compliance is not credited.

    This is the failure the separate-ledger design exists to prevent: a shared
    history would make "Compliance verified this itself" indistinguishable from
    "the Manager happened to have looked at it".
    """
    run = Run(
        manager_responses=(search(), open_span(TARGET_PARAGRAPH), sample_case.manager_response_text()),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    run.go()
    compliance = run.access("compliance")
    assert compliance.searches == ()
    assert compliance.opened_ids == ()
    assert compliance.used_source_tools is False
    # The Manager's work is still on the record, against the Manager.
    assert run.access("manager").opened_ids == (TARGET_PARAGRAPH,)
    assert len(run.tool_log.of_node("manager")) == 2
    assert run.tool_log.of_node("compliance") == ()


def test_every_tool_record_names_the_node_that_made_it():
    run = Run(
        manager_responses=(search(), open_span(TARGET_PARAGRAPH), sample_case.manager_response_text()),
        compliance_responses=(search("liability"), open_span(LIABILITY_PARAGRAPH),
                              sample_case.compliance_response_text()),
    )
    run.go()
    nodes = {record.node for record in run.tool_log.records}
    assert nodes == {"manager", "compliance"}
    for record in run.tool_log.records:
        assert record.node in ("manager", "compliance")
        assert record.invocation == 0


def test_a_second_invocation_of_a_node_does_not_reuse_the_first_ledger():
    """The ledger is per invocation. Nothing survives into the next one."""
    tools = SourceTools(
        node="manager",
        library=sample_case.build_source_library(),
        log=ToolCallLog(),
    )
    tools.begin_invocation(contract_id=sample_case.CONTRACT_ID, available=True, invocation=0)
    tools.search_contract("change of control")
    tools.open_source_span(TARGET_PARAGRAPH)
    assert tools.ledger.opened_ids == (TARGET_PARAGRAPH,)

    tools.begin_invocation(contract_id=sample_case.CONTRACT_ID, available=True, invocation=1)
    assert tools.ledger.opened_ids == ()
    assert tools.ledger.searches == ()
    assert tools.ledger.classify(TARGET_PARAGRAPH) is EvidenceClass.UNKNOWN


# --------------------------------------------------------------------------
# 2. Upstream-cited is not self-verified
# --------------------------------------------------------------------------


def test_a_memo_source_id_is_upstream_cited_not_self_verified():
    run = Run(
        manager_responses=(sample_case.manager_response_text(),),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    run.go()
    manager = run.access("manager")
    assert manager.classify(MEMO_SOURCE_ID) is EvidenceClass.UPSTREAM_CITED
    assert manager.used_source_tools is False
    assert manager.opened_ids == ()


def test_reading_the_handoff_is_not_searching_the_contract():
    """Compliance's upstream set is populated and its tool use is still false."""
    run = Run(
        manager_responses=(sample_case.manager_response_text(evidence_ids=("S-3.2",)),),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    run.go()
    compliance = run.access("compliance")
    assert compliance.upstream_ids == ("S-3.2",)
    assert compliance.used_source_tools is False
    assert compliance.searches == ()


def test_upstream_ids_are_inside_observed_ids_but_not_inside_opened_ids():
    """The set relation that keeps the two concepts from collapsing."""
    run = Run(
        manager_responses=(sample_case.manager_response_text(),),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    run.go()
    manager = run.access("manager")
    assert set(manager.upstream_ids) <= set(manager.observed_ids)
    assert not set(manager.upstream_ids) & set(manager.opened_ids)


def test_the_four_classes_are_mutually_exclusive_and_ordered():
    """Opening wins over seeing; seeing wins over being handed; else unknown."""
    tools = SourceTools(
        node="manager",
        library=sample_case.build_source_library(),
        log=ToolCallLog(),
    )
    tools.begin_invocation(contract_id=sample_case.CONTRACT_ID, available=True)
    hits = tools.search_contract("change of control")
    seen = hits[0].paragraph_id
    tools.open_source_span(seen)
    tools.ledger.record_upstream(["S-3.2"])
    ledger = tools.ledger
    assert ledger.classify(seen) is EvidenceClass.SELF_OPENED
    other_seen = next(
        (hit.paragraph_id for hit in hits if hit.paragraph_id != seen), None
    )
    if other_seen is not None:
        assert ledger.classify(other_seen) is EvidenceClass.OBSERVED
    assert ledger.classify("S-3.2") is EvidenceClass.UPSTREAM_CITED
    assert ledger.classify("9f9f9f9f9f9f:p0001") is EvidenceClass.UNKNOWN


def test_only_self_opened_ids_count_as_verification():
    run = Run(
        manager_responses=(search(), sample_case.manager_response_text(),),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    run.go()
    manager = run.access("manager")
    assert manager.search_result_ids
    for paragraph_id in manager.search_result_ids:
        assert manager.classify(paragraph_id) is EvidenceClass.OBSERVED
    assert manager.opened_ids == ()


# --------------------------------------------------------------------------
# 3. The ledger is reconstructible from the log
# --------------------------------------------------------------------------


def test_the_replayed_ledger_matches_the_live_one():
    run = Run(
        manager_responses=(search(), open_span(TARGET_PARAGRAPH), sample_case.manager_response_text()),
        compliance_responses=(search("liability"), sample_case.compliance_response_text()),
    )
    run.go()
    live = run.access("manager")
    replayed = evidence_access(
        run.tool_log.records,
        node="manager",
        upstream=manager_upstream_ids(build_manager_view(run.state)),
    )
    assert replayed == live


def test_replay_without_upstream_understates_access_rather_than_inventing_it():
    """Omitting the upstream argument is the safe direction, and it is visible.

    A replay that guessed at upstream ids would be able to manufacture a
    provenance claim the log does not support. Understating produces an
    unexplained verification failure instead -- which is the failure a reader
    can see and chase.
    """
    run = Run(
        manager_responses=(sample_case.manager_response_text(),),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    run.go()
    with_upstream = run.access("manager")
    without = evidence_access(run.tool_log.records, node="manager")
    assert without.upstream_ids == ()
    assert set(without.observed_ids) < set(with_upstream.observed_ids)
    assert with_upstream.upstream_ids, "the fixture run has no upstream ids"


def test_replay_ignores_refused_calls():
    """A refusal is on the record but grants nothing."""
    run = Run(
        condition_id="A0V0",
        manager_responses=(
            search(),
            sample_case.manager_response_text(),
        ),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    run.go()
    assert run.tool_log.failures()
    assert evidence_access(run.tool_log.records, node="manager").opened_ids == ()


def test_replay_is_order_independent_within_a_node():
    """The ledger is a set of facts, so shuffling the records changes nothing."""
    run = Run(
        manager_responses=(search(), open_span(TARGET_PARAGRAPH), sample_case.manager_response_text()),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    run.go()
    records = run.tool_log.of_node("manager")
    forwards = replay_ledger(records, node="manager")
    backwards = replay_ledger(tuple(reversed(records)), node="manager")
    assert forwards.opened_ids == backwards.opened_ids
    assert sorted(forwards.search_result_ids) == sorted(backwards.search_result_ids)


def test_replay_filters_to_one_node_only():
    run = Run(
        manager_responses=(search(), sample_case.manager_response_text()),
        compliance_responses=(search("liability"), open_span(LIABILITY_PARAGRAPH),
                              sample_case.compliance_response_text()),
    )
    run.go()
    every = replay_ledger(run.tool_log.records)
    manager_only = replay_ledger(run.tool_log.records, node="manager")
    assert set(every.opened_ids) == {LIABILITY_PARAGRAPH}
    assert manager_only.opened_ids == ()


# --------------------------------------------------------------------------
# 4. A cited id must have been observed by the node that cites it
# --------------------------------------------------------------------------


def test_a_node_cannot_cite_an_id_it_never_observed():
    """A fabricated paragraph id is UNKNOWN against this node's ledger."""
    run = Run(
        manager_responses=(
            search(),
            sample_case.manager_response_text(evidence_ids=("deadbeef0000:p0000",)),
        ),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    run.go()
    manager = run.access("manager")
    assert manager.classify("deadbeef0000:p0000") is EvidenceClass.UNKNOWN
    assert "deadbeef0000:p0000" not in manager.observed_ids


def test_an_id_returned_by_a_search_is_observed_even_if_never_opened():
    """The model saw it, so citing it is not fabrication -- it is just not
    verification, and the ledger says exactly that."""
    run = Run(
        manager_responses=(search(), sample_case.manager_response_text(),),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    run.go()
    manager = run.access("manager")
    for paragraph_id in manager.search_result_ids:
        assert paragraph_id in manager.observed_ids
        assert manager.classify(paragraph_id) is not EvidenceClass.SELF_OPENED


def test_an_id_from_another_contract_is_not_observed():
    """A real, well-formed paragraph id -- of a contract this run is not about.

    The id scheme is content-derived, so a genuinely different contract needs
    genuinely different text; a copy of the same text would produce the same
    ids and the test would prove nothing. One heading is lengthened, which
    changes the digest and therefore every id in the document.
    """
    foreign_text = sample_case.CONTRACT_TEXT.replace(
        "GOVERNING LAW", "GOVERNING LAW AND JURISDICTION"
    )
    assert foreign_text != sample_case.CONTRACT_TEXT
    other = SourceLibrary.from_texts({"CONTRACT-999": foreign_text})
    foreign_id = other.get("CONTRACT-999").paragraphs[0].paragraph_id
    assert foreign_id != TARGET_PARAGRAPH
    assert foreign_id not in {
        paragraph.paragraph_id
        for paragraph in sample_case.build_contract_document().paragraphs
    }

    run = Run(
        manager_responses=(
            open_span(foreign_id),
            sample_case.manager_response_text(evidence_ids=(foreign_id,)),
        ),
        compliance_responses=(sample_case.compliance_response_text(),),
        source=sample_case.build_source_library(),
    )
    run.go()
    manager = run.access("manager")
    assert foreign_id not in manager.observed_ids
    assert manager.classify(foreign_id) is EvidenceClass.UNKNOWN
    assert run.tool_log.failures(), "opening a foreign id must be refused"


def test_the_same_contract_text_yields_the_same_ids_in_any_library():
    """The id is a property of the text, not of the library that holds it.

    Worth stating outright, because it is the reason the test above has to
    change the text: a "second contract" that is a copy of the first is not a
    second contract as far as the id scheme is concerned.
    """
    left = SourceLibrary.from_texts({"A": sample_case.CONTRACT_TEXT})
    right = SourceLibrary.from_texts({"B": sample_case.CONTRACT_TEXT})
    assert [p.paragraph_id for p in left.get("A").paragraphs] == [
        p.paragraph_id for p in right.get("B").paragraphs
    ]


def test_the_verdict_is_derived_from_the_ledger_not_from_the_claim():
    """Two runs, identical claim, different access: different verdicts."""
    claiming = sample_case.manager_response_text(
        evidence_ids=(TARGET_PARAGRAPH,),
        verification_status=VerificationStatus.VERIFIED,
    )
    no_access = Run(
        condition_id="A1V1",
        manager_responses=(claiming,),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    no_access.go()
    with_access = Run(
        condition_id="A1V1",
        manager_responses=(search(), search("assignment"), open_span(TARGET_PARAGRAPH), claiming),
        compliance_responses=(
            search(),
            search("assignment"),
            open_span(TARGET_PARAGRAPH),
            sample_case.compliance_response_text(
                evidence_ids=(TARGET_PARAGRAPH,),
                verification_status=VerificationStatus.VERIFIED,
            ),
        ),
    )
    with_access.go()
    assert no_access.error is not None
    assert with_access.error is None
    assert no_access.verification_log.failures()
    assert with_access.verification_log.failures() == ()
