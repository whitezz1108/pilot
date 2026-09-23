"""Gate 2.5, groups 2-4: what A0, A1V0 and A1V1 actually do at runtime.

The three conditions are the treatment, so these tests are about behaviour, not
about wording:

* **A0** -- the tool layer is present but unavailable. The model is told it has
  no access; an attempt to call a tool is refused, logged, and returned to the
  model as a failed call rather than crashing the run.
* **A1V0** -- the tools work, and using none of them is a legal run. A search
  without an open is legal too. Nothing is penalised for not looking.
* **A1V1** -- the tools work and verification is *enforced*: the runtime
  re-derives the verdict from what the node demonstrably did, so
  ``"verification_status": "verified"`` is a claim it checks rather than one it
  believes. Each of the six gate conditions is exercised separately.

Every run here is offline: the transports are
:class:`~pilot01.model.scripted.ScriptedModelClient` instances replaying
declared response text, and the fixture contract is synthetic.
"""

from __future__ import annotations

import pytest

import sample_case
from pilot01.config import load_conditions_v1, load_workflow_v1
from pilot01.events import EventType
from pilot01.model import ModelCallLog, ScriptedModelClient
from pilot01.schemas import ClauseStatus, ErrorCondition, VerificationStatus
from pilot01.source import (
    EvidenceClass,
    SourceLibrary,
    ToolCallLog,
    VerificationFailure,
    VerificationLog,
    VerificationOutcome,
    build_document,
    evaluate_verification,
    replay_ledger,
    unverifiable_outcome,
)
from pilot01.source.ledger import EvidenceLedger
from pilot01.workflow.nodes import FrozenMemoRepository, build_llm_registry
from pilot01.workflow.nodes.manager import build_manager_messages
from pilot01.workflow.nodes.compliance import build_compliance_messages
from pilot01.workflow.nodes.render import (
    MAY_NOT_SEARCH,
    MAY_SEARCH,
    MUST_VERIFY,
    PERMISSIONS,
    UNVERIFIABLE,
    render_governance_block,
    render_source_permission,
)
from pilot01.workflow.runner import WorkflowRunner
from pilot01.workflow.state import ExperimentState, RunStatus
from pilot01.workflow.transitions import ProtocolError
from pilot01.workflow.views import build_compliance_view, build_manager_view

TARGET_PARAGRAPH = sample_case.paragraph_id_containing(sample_case.CHANGE_OF_CONTROL_PHRASE)
"""The fixture paragraph that states the clause the experiment is about."""

GOVERNING_LAW_PARAGRAPH = sample_case.paragraph_id_containing(
    sample_case.GOVERNING_LAW_PHRASE
)


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


def search(query: str = "change of control") -> str:
    return sample_case.tool_request_text("search_contract", query=query)


def open_span(paragraph_id: str = TARGET_PARAGRAPH) -> str:
    return sample_case.tool_request_text("open_source_span", paragraph_id=paragraph_id)


def verifying_answer(**kwargs) -> str:
    """A Manager response that claims verification and cites the opened paragraph."""
    kwargs.setdefault("evidence_ids", (TARGET_PARAGRAPH,))
    kwargs.setdefault("verification_status", VerificationStatus.VERIFIED)
    return sample_case.manager_response_text(**kwargs)


def verifying_compliance_answer(**kwargs) -> str:
    kwargs.setdefault("evidence_ids", (TARGET_PARAGRAPH,))
    kwargs.setdefault("verification_status", VerificationStatus.VERIFIED)
    return sample_case.compliance_response_text(**kwargs)


class Case:
    """One scripted, offline run: registry, transports, logs, state, runner."""

    def __init__(
        self,
        *,
        condition_id: str,
        error_condition: ErrorCondition = ErrorCondition.E0,
        manager_responses: tuple[str, ...],
        compliance_responses: tuple[str, ...],
        source: SourceLibrary | None = None,
        gold_status: ClauseStatus = ClauseStatus.PRESENT,
    ) -> None:
        self.condition_id = condition_id
        self.error: ProtocolError | None = None
        self.runner = None
        self.call_log = ModelCallLog()
        self.tool_log = ToolCallLog()
        self.verification_log = VerificationLog()
        self.manager_client = ScriptedModelClient(manager_responses)
        self.compliance_client = ScriptedModelClient(compliance_responses)
        self.repository = repository()
        self.registry = build_llm_registry(
            repository=self.repository,
            models=load_models(),
            manager_client=self.manager_client,
            compliance_client=self.compliance_client,
            call_log=self.call_log,
            source=source if source is not None else sample_case.build_source_library(),
            tool_log=self.tool_log,
            verification_log=self.verification_log,
        )
        self.conditions = load_conditions_v1()
        self.workflow = load_workflow_v1()
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
            gold_status=gold_status,
            policy=sample_case.build_policy(),
            gold_evidence_offsets=(987654321, 987654322),
            omission=None if error_condition is ErrorCondition.E0 else omission_record(),
        )

    def run(self) -> ExperimentState:
        runner = WorkflowRunner(
            workflow=self.workflow,
            conditions=self.conditions,
            registry=self.registry,
            clock=lambda: __import__("datetime").datetime(
                2026, 1, 1, tzinfo=__import__("datetime").timezone.utc
            ),
        )
        try:
            runner.run(self.state)
        except ProtocolError as exc:
            # The runner records the failure and re-raises. A test that is
            # inspecting a failed run wants the state, not the traceback, so the
            # exception is kept here rather than propagated.
            self.error = exc
        self.runner = runner
        return self.state

    # -- observations ------------------------------------------------------

    def ledger(self, node: str, *, upstream=()):
        return replay_ledger(self.tool_log.records, node=node, upstream=upstream)

    def verdicts(self, node: str) -> tuple[VerificationOutcome, ...]:
        return self.verification_log.of_node(node)

    def protocol_errors(self) -> tuple:
        return tuple(
            event
            for event in self.runner._collector.events
            if event.event_type is EventType.PROTOCOL_ERROR
        )


def load_models():
    from pilot01.config import load_models_v1

    return load_models_v1()


def a1v1(**kwargs) -> Case:
    """An A1V1 case in which both agents search, open, and answer with evidence."""
    kwargs.setdefault("manager_responses", (search(), open_span(), verifying_answer()))
    kwargs.setdefault(
        "compliance_responses", (search(), open_span(), verifying_compliance_answer())
    )
    return Case(condition_id="A1V1", **kwargs)


# --------------------------------------------------------------------------
# A0: the tools are not there
# --------------------------------------------------------------------------


def test_a0_renders_the_no_access_permission_and_no_tool_surface(make_state):
    view = build_manager_view(make_state(condition_id="A0V0"))
    text = "\n".join(message.content for message in build_manager_messages(view))
    assert MAY_NOT_SEARCH in text
    assert "No source tools are available to you in this workflow." in text
    assert "You may use these source tools:" not in text


def test_a1_renders_the_permission_and_the_two_tool_signatures(make_state):
    view = build_manager_view(make_state(condition_id="A1V0"))
    text = "\n".join(message.content for message in build_manager_messages(view))
    assert MAY_SEARCH in text
    assert "You may use these source tools:" in text
    assert "- `search_contract(query)`" in text
    assert "- `open_source_span(paragraph_id)`" in text
    assert "No source tools are available to you in this workflow." not in text


def test_a1v1_renders_the_obligation_and_not_the_optional_one(make_state):
    view = build_manager_view(make_state(condition_id="A1V1"))
    text = "\n".join(message.content for message in build_manager_messages(view))
    assert MUST_VERIFY in text
    assert MAY_SEARCH not in text
    assert MAY_NOT_SEARCH not in text


def test_a0v1_renders_an_explicitly_unverifiable_permission():
    """The degenerate cell. It is not a main condition and the state refuses to
    be built for it, but the renderer must still have a defined answer: if that
    cell is ever reached it must not state an obligation the node cannot meet.
    """
    text = render_governance_block(source_access=False, verification_required=True)
    assert UNVERIFIABLE in text
    assert MUST_VERIFY not in text
    assert MAY_SEARCH not in text
    assert MAY_NOT_SEARCH not in text


def test_the_permission_table_covers_the_two_by_two_exactly():
    """Four cells, four distinct sentences, no composition and no fallthrough."""
    assert set(PERMISSIONS) == {(False, False), (True, False), (True, True), (False, True)}
    assert len(set(PERMISSIONS.values())) == 4
    for (source_access, verification_required), sentence in PERMISSIONS.items():
        assert (
            render_source_permission(
                source_access=source_access,
                verification_required=verification_required,
            )
            == sentence
        )
    # Each condition's sentence is the pre-registered one for its cell.
    assert PERMISSIONS[(False, False)] is MAY_NOT_SEARCH
    assert PERMISSIONS[(True, False)] is MAY_SEARCH
    assert PERMISSIONS[(True, True)] is MUST_VERIFY
    assert PERMISSIONS[(False, True)] is UNVERIFIABLE


def test_compliance_is_offered_exactly_the_same_surface_as_the_manager(make_state):
    """Same tools, same wording: the two roles differ in their inputs, not in
    what the condition gives them."""
    for condition_id in ("A0V0", "A1V0", "A1V1"):
        manager_view = build_manager_view(make_state(condition_id=condition_id))
        state = make_state(condition_id=condition_id)
        state.manager_output = sample_case.manager_output()
        compliance_view = build_compliance_view(state)
        manager_text = "\n".join(
            message.content for message in build_manager_messages(manager_view)
        )
        compliance_text = "\n".join(
            message.content for message in build_compliance_messages(compliance_view)
        )
        for sentence in (
            MAY_NOT_SEARCH,
            MAY_SEARCH,
            MUST_VERIFY,
            UNVERIFIABLE,
            "No source tools are available to you in this workflow.",
            "You may use these source tools:",
        ):
            assert (sentence in manager_text) == (sentence in compliance_text), (
                condition_id,
                sentence,
            )


def test_a0_leaves_the_tool_layer_unavailable():
    case = Case(
        condition_id="A0V0",
        manager_responses=(sample_case.manager_response_text(),),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    case.run()
    assert case.tool_log.records == ()


def test_a0_refuses_and_logs_a_tool_attempt_instead_of_crashing():
    """The model is offered nothing, asks anyway, and is told no.

    The refusal is an observation: whether an agent that has been told it has no
    access nevertheless tries is part of what the pilot measures, so the run
    continues and the attempt is recorded.
    """
    case = Case(
        condition_id="A0V0",
        manager_responses=(
            search(),
            sample_case.manager_response_text(verification_status=VerificationStatus.NOT_CHECKED),
        ),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    state = case.run()

    assert state.status is RunStatus.COMPLETED
    refusals = case.tool_log.records
    assert len(refusals) == 1
    assert refusals[0].tool == "search_contract"
    assert refusals[0].ok is False
    assert refusals[0].available is False
    assert refusals[0].node == "manager"
    assert "no source tools are available" in refusals[0].error


def test_a0_returns_the_refusal_to_the_model_so_it_can_continue():
    case = Case(
        condition_id="A0V0",
        manager_responses=(
            search(),
            sample_case.manager_response_text(verification_status=VerificationStatus.NOT_CHECKED),
        ),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    case.run()
    sent = case.manager_client.requests[1].rendered_input()
    assert "TOOL RESULT" in sent
    assert "status: error" in sent
    assert "no source tools are available" in sent


def test_a0_does_not_let_a_model_claim_a_paragraph_it_could_not_have_opened():
    """In A0 no paragraph id is reachable, so citing one is a fabrication.

    V0 does not act on it, but the ledger classifies it ``UNKNOWN`` and the
    diagnostic records it -- the fabrication stays visible in the record.
    """
    case = Case(
        condition_id="A0V0",
        manager_responses=(
            sample_case.manager_response_text(
                evidence_ids=(TARGET_PARAGRAPH,),
                verification_status=VerificationStatus.VERIFIED,
            ),
        ),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    case.run()
    verdict = case.verdicts("manager")[0]
    assert verdict.failures == ()
    assert "unobserved=" + TARGET_PARAGRAPH in verdict.diagnostics


def test_a0_never_produces_an_opened_id():
    case = Case(
        condition_id="A0V0",
        manager_responses=(
            search(),
            open_span(),
            sample_case.manager_response_text(verification_status=VerificationStatus.NOT_CHECKED),
        ),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    case.run()
    assert case.ledger("manager").opened_ids == ()
    assert case.ledger("manager").used_source_tools() is False


# --------------------------------------------------------------------------
# A1V0: the tools work, and using none of them is legal
# --------------------------------------------------------------------------


def test_a1v0_completes_with_no_tool_use_at_all():
    case = Case(
        condition_id="A1V0",
        manager_responses=(sample_case.manager_response_text(),),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    state = case.run()

    assert state.status is RunStatus.COMPLETED
    assert case.tool_log.records == ()
    assert state.manager_output is not None
    assert state.compliance_output is not None


def test_a1v0_does_not_penalise_a_node_for_not_checking():
    case = Case(
        condition_id="A1V0",
        manager_responses=(sample_case.manager_response_text(),),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    case.run()
    for verdict in case.verification_log.records:
        assert verdict.required is False
        assert verdict.failures == ()
        assert verdict.satisfied is True
        assert verdict.status is VerificationStatus.NOT_CHECKED


def test_a1v0_records_what_the_node_did_even_though_nothing_is_required():
    """``diagnostics`` is populated in both arms, so a V0 run is auditable too."""
    case = Case(
        condition_id="A1V0",
        manager_responses=(search(), sample_case.manager_response_text()),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    case.run()
    verdict = case.verdicts("manager")[0]
    assert verdict.required is False
    assert verdict.checked is True
    assert "searched=1" in verdict.diagnostics
    assert "opened=0" in verdict.diagnostics


def test_a1v0_allows_a_search_without_an_open():
    case = Case(
        condition_id="A1V0",
        manager_responses=(search(), sample_case.manager_response_text()),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    state = case.run()
    assert state.status is RunStatus.COMPLETED
    assert case.ledger("manager").search_result_ids
    assert case.ledger("manager").opened_ids == ()


def test_a1v0_allows_a_search_that_returns_nothing():
    case = Case(
        condition_id="A1V0",
        manager_responses=(search("zzzzz nonexistent"), sample_case.manager_response_text()),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    state = case.run()
    assert state.status is RunStatus.COMPLETED
    assert case.tool_log.records[0].ok is True
    assert case.tool_log.records[0].result_ids == ()


def test_a1v0_reports_not_checked_as_a_permitted_answer():
    case = Case(
        condition_id="A1V0",
        manager_responses=(
            sample_case.manager_response_text(
                verification_status=VerificationStatus.NOT_CHECKED
            ),
        ),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    case.run()
    assert case.verdicts("manager")[0].satisfied is True


def test_a1v0_marks_an_unobserved_id_in_the_diagnostics_but_fails_nothing():
    case = Case(
        condition_id="A1V0",
        manager_responses=(
            sample_case.manager_response_text(evidence_ids=(TARGET_PARAGRAPH,)),
        ),
        compliance_responses=(sample_case.compliance_response_text(),),
    )
    state = case.run()
    assert state.status is RunStatus.COMPLETED
    verdict = case.verdicts("manager")[0]
    assert verdict.failures == ()
    assert f"unobserved={TARGET_PARAGRAPH}" in verdict.diagnostics


# --------------------------------------------------------------------------
# A1V1: verification is enforced, not believed
# --------------------------------------------------------------------------


def test_a1v1_completes_when_both_nodes_open_what_they_cite():
    case = a1v1()
    state = case.run()

    assert state.status is RunStatus.COMPLETED
    assert state.manager_output.evidence_ids == (TARGET_PARAGRAPH,)
    assert state.compliance_output.evidence_ids == (TARGET_PARAGRAPH,)
    for node in ("manager", "compliance"):
        verdict = case.verdicts(node)[0]
        assert verdict.required is True
        assert verdict.checked is True
        assert verdict.failures == ()
        assert verdict.satisfied is True


def test_a1v1_logs_the_search_and_the_open_in_order():
    case = a1v1()
    case.run()
    manager_records = case.tool_log.of_node("manager")
    assert [record.tool for record in manager_records] == [
        "search_contract",
        "open_source_span",
    ]
    assert [record.sequence for record in manager_records] == [0, 1]
    assert manager_records[1].result_ids == (TARGET_PARAGRAPH,)


def test_a1v1_does_not_accept_a_claim_of_verification_without_a_tool_call():
    """The headline requirement: ``"verified"`` is a claim, and it is checked."""
    case = Case(
        condition_id="A1V1",
        manager_responses=(verifying_answer(),),
        compliance_responses=(verifying_compliance_answer(),),
    )
    state = case.run()

    assert state.status is RunStatus.FAILED
    assert state.manager_output is None
    verdict = case.verdicts("manager")[0]
    assert VerificationFailure.NO_TOOL_USE in verdict.failures
    assert VerificationFailure.EVIDENCE_NOT_OPENED in verdict.failures
    assert verdict.satisfied is False


def test_a1v1_does_not_accept_a_search_result_as_evidence():
    """Condition 3: reading a candidate is the step that makes it evidence."""
    case = Case(
        condition_id="A1V1",
        manager_responses=(search(), verifying_answer()),
        compliance_responses=(search(), open_span(), verifying_compliance_answer()),
    )
    state = case.run()

    assert state.status is RunStatus.FAILED
    verdict = case.verdicts("manager")[0]
    assert VerificationFailure.EVIDENCE_NOT_OPENED in verdict.failures
    # It *was* observed -- the search returned it -- which is why the failure is
    # "not opened" and not "not observed". The two are different findings.
    assert VerificationFailure.EVIDENCE_NOT_OBSERVED not in verdict.failures
    assert case.ledger("manager").classify(TARGET_PARAGRAPH) is EvidenceClass.OBSERVED


def test_a1v1_does_not_accept_a_fabricated_paragraph_id():
    """Condition 5: an id nothing this node saw could have produced."""
    invented = f"{build_document('X', 'x').paragraphs[0].paragraph_id}"
    case = Case(
        condition_id="A1V1",
        manager_responses=(
            search(),
            open_span(),
            verifying_answer(evidence_ids=(TARGET_PARAGRAPH, invented)),
        ),
        compliance_responses=(search(), open_span(), verifying_compliance_answer()),
    )
    state = case.run()

    assert state.status is RunStatus.FAILED
    verdict = case.verdicts("manager")[0]
    assert VerificationFailure.EVIDENCE_NOT_OBSERVED in verdict.failures
    assert VerificationFailure.EVIDENCE_NOT_IN_CONTRACT in verdict.failures


def test_a1v1_does_not_accept_an_id_from_another_contract():
    """Condition 4: a well-formed id that is not a paragraph of this contract."""
    other = build_document("CONTRACT-002", "An entirely different agreement. " * 20)
    foreign = other.paragraphs[0].paragraph_id
    case = Case(
        condition_id="A1V1",
        manager_responses=(
            search(),
            open_span(),
            verifying_answer(evidence_ids=(TARGET_PARAGRAPH, foreign)),
        ),
        compliance_responses=(search(), open_span(), verifying_compliance_answer()),
    )
    state = case.run()

    assert state.status is RunStatus.FAILED
    verdict = case.verdicts("manager")[0]
    assert VerificationFailure.EVIDENCE_NOT_IN_CONTRACT in verdict.failures
    assert f"not_in_contract={foreign}" in verdict.diagnostics


def test_a1v1_does_not_accept_a_status_that_is_not_verified():
    case = Case(
        condition_id="A1V1",
        manager_responses=(
            search(),
            open_span(),
            verifying_answer(verification_status=VerificationStatus.NOT_CHECKED),
        ),
        compliance_responses=(search(), open_span(), verifying_compliance_answer()),
    )
    state = case.run()
    assert state.status is RunStatus.FAILED
    assert VerificationFailure.STATUS_NOT_VERIFIED in case.verdicts("manager")[0].failures


def test_a1v1_does_not_accept_a_run_that_never_names_the_rule():
    case = Case(
        condition_id="A1V1",
        manager_responses=(search(), open_span(), verifying_answer(rule_id="SOMETHING-ELSE")),
        compliance_responses=(search(), open_span(), verifying_compliance_answer()),
    )
    state = case.run()
    assert state.status is RunStatus.FAILED
    assert VerificationFailure.RULE_NOT_STATED in case.verdicts("manager")[0].failures


def test_a1v1_does_not_accept_a_citation_that_is_only_upstream():
    """Compliance may cite the handoff's ids, but that is not verification."""
    case = Case(
        condition_id="A1V1",
        manager_responses=(search(), open_span(), verifying_answer()),
        compliance_responses=(verifying_compliance_answer(),),
    )
    state = case.run()

    assert state.status is RunStatus.FAILED
    verdict = case.verdicts("compliance")[0]
    assert VerificationFailure.NO_TOOL_USE in verdict.failures
    assert VerificationFailure.EVIDENCE_NOT_OPENED in verdict.failures
    ledger = case.ledger("compliance", upstream=(TARGET_PARAGRAPH,))
    assert ledger.classify(TARGET_PARAGRAPH) is EvidenceClass.UPSTREAM_CITED


def test_a1v1_fails_the_run_rather_than_filling_the_verification_fields():
    """No fabrication, no silent filling, no conversion to the expected answer."""
    case = Case(
        condition_id="A1V1",
        manager_responses=(verifying_answer(),),
        compliance_responses=(verifying_compliance_answer(),),
    )
    state = case.run()

    assert state.manager_output is None
    assert state.compliance_output is None
    assert state.status is RunStatus.FAILED
    # The policy's own expected action is never consulted: the run ends with no
    # output rather than with the answer the experiment expects.
    assert state.hidden.gold.gold_clause_status is ClauseStatus.PRESENT


def test_a1v1_records_the_failure_as_a_protocol_error_on_the_node():
    case = Case(
        condition_id="A1V1",
        manager_responses=(verifying_answer(),),
        compliance_responses=(verifying_compliance_answer(),),
    )
    case.run()
    errors = case.protocol_errors()
    assert errors
    assert errors[0].node == "manager"
    assert "verification requirement" in errors[0].payload["error"]


def test_a1v1_records_the_verdict_even_when_the_run_fails():
    case = Case(
        condition_id="A1V1",
        manager_responses=(verifying_answer(),),
        compliance_responses=(verifying_compliance_answer(),),
    )
    case.run()
    assert len(case.verification_log) == 1
    assert case.verification_log.failures() == case.verification_log.records


def test_a1v1_raises_out_of_the_node_so_the_runner_can_stop():
    """The node refuses; it does not return a half-verified output.

    The runner is called directly here rather than through ``Case.run`` so that
    the raise is observed rather than absorbed: recording the failure and
    re-raising is the documented runner semantics, and a node that swallowed its
    own refusal would be the bug this test exists to catch.
    """
    case = Case(
        condition_id="A1V1",
        manager_responses=(verifying_answer(),),
        compliance_responses=(verifying_compliance_answer(),),
    )
    runner = WorkflowRunner(
        workflow=case.workflow,
        conditions=case.conditions,
        registry=case.registry,
        clock=lambda: __import__("datetime").datetime(
            2026, 1, 1, tzinfo=__import__("datetime").timezone.utc
        ),
    )
    with pytest.raises(ProtocolError) as excinfo:
        runner.run(case.state)
    assert "verification requirement" in str(excinfo.value)
    assert case.state.status is RunStatus.FAILED
    assert case.state.manager_output is None


def test_a1v1_verdict_is_the_same_on_both_error_conditions():
    """The verdict is derived from the tool log, so it cannot depend on E0/E1.

    The diagnostics are deliberately *not* compared: they count what the node
    was handed upstream, and the E1 memo cites fewer source ids than the E0 memo
    because the omitted claim is the one carrying them. That difference is the
    treatment working, not a leak -- the counts the verdict is built from are
    identical.
    """
    verdicts = {}
    for error_condition in (ErrorCondition.E0, ErrorCondition.E1):
        case = a1v1(error_condition=error_condition)
        case.run()
        verdicts[error_condition] = case.verdicts("manager")[0]
    left, right = verdicts[ErrorCondition.E0], verdicts[ErrorCondition.E1]
    assert left.failures == right.failures
    assert left.checked == right.checked
    assert left.required == right.required
    assert left.evidence_ids == right.evidence_ids
    assert left.status is right.status


# --------------------------------------------------------------------------
# The degenerate combination
# --------------------------------------------------------------------------


def test_a0v1_is_explicitly_unverifiable_rather_than_quietly_passed():
    """Verification required, no tools: a combination that cannot be satisfied."""
    outcome = unverifiable_outcome("no source tools in this condition", node="manager")
    assert outcome.status is VerificationStatus.UNVERIFIABLE
    assert outcome.required is True
    assert outcome.checked is False
    assert outcome.satisfied is False
    assert VerificationFailure.SOURCE_TOOLS_UNAVAILABLE in outcome.failures


def test_a0v1_is_not_a_condition_the_configuration_offers(conditions):
    assert "A0V1" not in conditions.conditions
    assert "A0V1" not in conditions.main_conditions
    assert "A0V1" in conditions.excluded_conditions


def test_the_unverifiable_outcome_arises_from_the_evaluator_itself():
    """Not only from the helper: the evaluator reaches it on its own path."""
    ledger = EvidenceLedger(node="manager")
    outcome = evaluate_verification(
        output=sample_case.manager_output(verification_status=VerificationStatus.VERIFIED),
        policy=sample_case.build_policy(),
        ledger=ledger,
        document=None,
        verification_required=True,
        source_tools_available=False,
        node="manager",
    )
    assert outcome.status is VerificationStatus.UNVERIFIABLE
    assert outcome.satisfied is False
    assert outcome.failures == (VerificationFailure.SOURCE_TOOLS_UNAVAILABLE,)


# --------------------------------------------------------------------------
# The evaluator's own conditions, exercised directly
# --------------------------------------------------------------------------


def test_the_evaluator_reports_an_incomplete_record():
    """Condition 6: the verification record's own required fields are complete.

    Unreachable through the node, because the schema requires ``clause_status``
    -- which is the point: the schema is the first line of that check and this
    is the second.
    """
    ledger = EvidenceLedger(node="manager")
    ledger.record_open(TARGET_PARAGRAPH)
    output = sample_case.manager_output(
        evidence_ids=(TARGET_PARAGRAPH,),
        verification_status=VerificationStatus.VERIFIED,
    )
    complete = evaluate_verification(
        output=output,
        policy=sample_case.build_policy(),
        ledger=ledger,
        document=sample_case.build_contract_document(),
        verification_required=True,
        source_tools_available=True,
        node="manager",
    )
    assert complete.failures == ()

    without_clause_status = output.model_copy(update={"clause_status": None})
    incomplete = evaluate_verification(
        output=without_clause_status,
        policy=sample_case.build_policy(),
        ledger=ledger,
        document=sample_case.build_contract_document(),
        verification_required=True,
        source_tools_available=True,
        node="manager",
    )
    assert VerificationFailure.CHECK_INCOMPLETE in incomplete.failures


def test_the_evaluator_reports_no_evidence_cited():
    ledger = EvidenceLedger(node="manager")
    ledger.record_search("change of control", [TARGET_PARAGRAPH])
    outcome = evaluate_verification(
        output=sample_case.manager_output(
            evidence_ids=(), verification_status=VerificationStatus.VERIFIED
        ),
        policy=sample_case.build_policy(),
        ledger=ledger,
        document=sample_case.build_contract_document(),
        verification_required=True,
        source_tools_available=True,
        node="manager",
    )
    assert VerificationFailure.NO_EVIDENCE_CITED in outcome.failures
    # A search was made, so "no tool use" is *not* among the failures.
    assert VerificationFailure.NO_TOOL_USE not in outcome.failures


def test_each_verification_failure_names_a_distinct_way_of_being_unbacked():
    values = {member.value for member in VerificationFailure}
    assert values == {
        "rule_not_stated",
        "no_tool_use",
        "no_evidence_cited",
        "evidence_not_opened",
        "evidence_not_in_contract",
        "evidence_not_observed",
        "status_not_verified",
        "check_incomplete",
        "source_tools_unavailable",
    }


def test_the_verification_log_keeps_nodes_and_failures_apart():
    log = VerificationLog()
    log.append(unverifiable_outcome("a", node="manager"))
    log.append(unverifiable_outcome("b", node="compliance"))
    assert len(log.of_node("manager")) == 1
    assert len(log.of_node("compliance")) == 1
    assert len(log.failures()) == 2
    assert log.records[0].node == "manager"
