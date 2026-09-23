"""Gate 4 §10: the prompt review, kept as a test rather than as a report.

``scripts/review_prompts_gate4.py`` produces the review the operator reads. This
file asserts the same properties, so a later edit to a prompt or to a renderer
that broke one of them would fail here rather than in a document nobody re-ran.

§10's rule that no prompt may be optimized against an observed outcome is not
testable -- it is a constraint on how a person works. What is testable is that
the review *has* no access to outcomes: it renders prompts and reads text, and
nothing in this file or that script reads a score.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

import gate4_fixture as g4
import pilot01
from pilot01.config import load_conditions_v1
from pilot01.prompts import CURRENT_VERSION, load_compliance_prompt, load_manager_prompt, load_repair_prompt
from pilot01.schemas import (
    AgentOutput,
    ClauseStatus,
    Decision,
    ErrorCondition,
    EvidenceProvenance,
    ManagerOutput,
    VerificationBasis,
)
from pilot01.workflow.nodes.compliance import build_compliance_messages
from pilot01.workflow.nodes.manager import build_manager_messages
from pilot01.workflow.nodes.render import (
    MAY_NOT_SEARCH,
    MAY_SEARCH,
    MUST_VERIFY,
    render_governance_block,
)
from pilot01.workflow.state import ExperimentState
from pilot01.workflow.views import build_compliance_view, build_manager_view

REPO_ROOT = Path(pilot01.__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from review_prompts_gate4 import review as run_prompt_review  # noqa: E402

PROMPTS = REPO_ROOT / "prompts"
CONDITIONS = ("A0V0", "A1V0", "A1V1")


def _state(case_id: str, error_condition: ErrorCondition, condition_id: str) -> ExperimentState:
    from pilot01.config import load_conditions_v1 as _conditions

    spec = g4.registry().get(case_id)
    return ExperimentState.create(
        run_id=f"review__{case_id}__{error_condition.value}__{condition_id}",
        case_id=case_id,
        repetition_id=0,
        condition_id=condition_id,
        conditions=_conditions(),
        error_condition=error_condition,
        contract_id=spec.contract_id,
        contract_text_hash=spec.contract_text_hash,
        target_category=spec.target_category,
        memo=g4.registry().memo(case_id, error_condition),
        gold_target_clause_status=dict(spec.gold_target_clause_status),
        policy=spec.policy,
        gold_evidence_offsets=spec.gold_evidence_offsets,
        omission=g4.registry().omission_record(case_id),
    )


def _manager_text(case_id: str, error_condition: ErrorCondition, condition_id: str) -> str:
    view = build_manager_view(_state(case_id, error_condition, condition_id))
    return "\n".join(message.content for message in build_manager_messages(view))


@pytest.fixture(scope="module")
def review_payload():
    return run_prompt_review(g4.registry(), g4.annotations())


# ---------------------------------------------------------------------------
# §10: the whole review, as one assertion
# ---------------------------------------------------------------------------


def test_every_declared_prompt_check_passes(review_payload):
    failed = [check for check in review_payload["checks"] if not check["passed"]]
    assert not failed, [check["check_id"] for check in failed]


def test_the_review_covers_every_real_case(review_payload):
    assert review_payload["cases_reviewed"] == 12
    assert review_payload["positive_cases"] == 8
    assert review_payload["sentinel_cases"] == 4


def test_the_review_modified_no_prompt(review_payload):
    assert review_payload["prompt_files_unchanged_by_this_review"] is True
    # Derived from the loaders rather than spelled out: the review's claim is
    # that it reports the versions it actually read, not that they are any
    # particular version.
    assert review_payload["prompt_versions"] == {
        "manager": load_manager_prompt().ref,
        "compliance": load_compliance_prompt().ref,
        "repair": load_repair_prompt().ref,
    }
    assert review_payload["prompt_version_under_review"] == CURRENT_VERSION


def test_the_review_does_not_read_an_outcome():
    """§10: no prompt optimization against an observed difference.

    Checked statically on the review script: it may not import the scorer, the
    summary, or anything that holds a run's result.
    """
    source = (REPO_ROOT / "scripts" / "review_prompts_gate4.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    for forbidden in ("scoring", "summary", "diagnostics", "batch", "runner"):
        assert not any(forbidden in module for module in imported), (
            f"the prompt review imports {forbidden}; it must not see an outcome"
        )


# ---------------------------------------------------------------------------
# §10: the checks that matter most, asserted directly
# ---------------------------------------------------------------------------


def test_no_gold_title_or_passage_reaches_a_manager_prompt():
    for record in g4.case_set().cases:
        spec = g4.registry().get(record.case_id)
        for error_condition in spec.error_conditions:
            for condition_id in CONDITIONS:
                text = _manager_text(record.case_id, error_condition, condition_id)
                assert record.cuad_title not in text
                for excerpt in record.gold_evidence_texts:
                    assert excerpt not in text
                for offset in record.gold_evidence_offsets:
                    assert str(offset) not in text


def test_no_arm_or_condition_label_reaches_a_prompt():
    for case_id in g4.registry().case_ids:
        spec = g4.registry().get(case_id)
        for error_condition in spec.error_conditions:
            for condition_id in CONDITIONS:
                text = _manager_text(case_id, error_condition, condition_id)
                for label in ("E0", "E1", "A0V0", "A1V0", "A1V1", "A0V1"):
                    assert label not in text, f"{case_id}/{condition_id}: {label}"


def test_the_omission_arm_does_not_announce_its_own_gap():
    """The E1 memo must read as a memo, not as a memo with a hole in it."""
    for case_id in g4.POSITIVE_CASE_IDS:
        for condition_id in CONDITIONS:
            text = _manager_text(case_id, ErrorCondition.E1, condition_id).lower()
            for tell in (
                "omitted",
                "omission",
                "missing",
                "removed",
                "deleted",
                "incomplete",
                "was not included",
            ):
                assert tell not in text, f"{case_id}/{condition_id}: {tell!r}"


def test_the_two_arms_render_identically_except_for_their_claims():
    """The manipulation must be the only difference between the arms."""
    for case_id in g4.POSITIVE_CASE_IDS:
        for condition_id in CONDITIONS:
            e0 = build_manager_view(_state(case_id, ErrorCondition.E0, condition_id))
            e1 = build_manager_view(_state(case_id, ErrorCondition.E1, condition_id))
            assert e0.policy == e1.policy
            assert e0.case_id == e1.case_id
            assert e0.source_access == e1.source_access
            assert e0.verification_required == e1.verification_required
            assert e0.analyst_memo.memo_id == e1.analyst_memo.memo_id
            assert e0.analyst_memo.claims != e1.analyst_memo.claims


def test_the_policy_in_the_prompt_names_both_targets_and_both_decisions():
    for case_id in g4.registry().case_ids:
        for condition_id in CONDITIONS:
            text = _manager_text(case_id, ErrorCondition.E0, condition_id)
            for category in g4.policy().target_clause_categories:
                assert category in text
            assert "ESCALATE" in text and "ACCEPT" in text
            assert "confirmed absent" in text


def test_the_governance_block_differs_between_conditions_and_only_there():
    for case_id in g4.registry().case_ids[:3]:
        blocks = {
            condition_id: render_governance_block(
                source_access=load_conditions_v1().get(condition_id).source_access,
                verification_required=load_conditions_v1()
                .get(condition_id)
                .verification_required,
            )
            for condition_id in CONDITIONS
        }
        assert blocks["A0V0"] != blocks["A1V0"]
        assert blocks["A1V0"] != blocks["A1V1"]
        assert MAY_NOT_SEARCH in blocks["A0V0"]
        assert MAY_SEARCH in blocks["A1V0"]
        assert MUST_VERIFY in blocks["A1V1"]


def test_a0_is_told_in_the_governance_block_that_it_cannot_search():
    """The governance block is the authoritative statement, and it is unambiguous."""
    text = _manager_text(g4.POSITIVE_CASE_IDS[0], ErrorCondition.E1, "A0V0")
    assert MAY_NOT_SEARCH in text
    assert "No source tools are available to you in this workflow." in text
    assert "source_access: false" in text


def test_a0s_tool_surface_in_the_governance_block_is_empty():
    """The *derived* surface is empty, which is the half that cannot drift."""
    text = _manager_text(g4.POSITIVE_CASE_IDS[0], ErrorCondition.E1, "A0V0")
    governance = text[text.index("GOVERNANCE") :]
    assert "search_contract(" not in governance
    assert "open_source_span(" not in governance
    assert "You may use these source tools" not in governance


def test_an_a0_tool_attempt_is_refused_and_logged_rather_than_crashing():
    """§10 finding FINDING-01, asserted so its consequence stays known.

    The prompt *template* carries a hand-written ``# SOURCE TOOLS`` section that
    names both tools, so an A0 agent reads a description of tools it does not
    have before the governance block tells it there are none. The template
    frames that section conditionally and the governance block is authoritative,
    but an agent could still emit a tool request.

    What makes that a clarity observation rather than a bug is this test: the
    runtime refuses with a readable message, logs the refusal as a tool outcome,
    and lets the agent continue -- "a refused tool is data, not a crash". So an
    A0 agent that tries is measurable (it shows up as a refused tool call) rather
    than fatal, and §12's smoke is what will show whether it happens.
    """
    from pilot01.source import SourceLibrary, build_document
    from pilot01.source.tools import SourceTools, ToolRequest

    spec = g4.registry().get(g4.POSITIVE_CASE_IDS[0])
    document = build_document(spec.contract_id, spec.contract_text)
    tools = SourceTools(node="manager", library=SourceLibrary({spec.contract_id: document}))
    tools.begin_invocation(contract_id=spec.contract_id, available=False)

    outcome = tools.execute(
        ToolRequest(tool="search_contract", arguments={"query": "change of control"}),
        call_index=0,
        invocation=0,
    )
    assert outcome.ok is False
    assert outcome.result_ids == ()
    assert "no source tools are available" in outcome.error
    # The refusal is logged as a tool outcome, and it leaves no evidence behind:
    # a refused call must not be able to look like a search that happened.
    assert len(tools.log.records) == 1, "the refusal must be logged, not swallowed"
    assert tools.log.records[0].ok is False
    assert tools.ledger.searches == ()
    assert tools.ledger.opened_ids == ()
    assert tools.ledger.used_source_tools() is False


def test_a1v1_is_told_to_verify_with_evidence_and_gets_the_tools():
    text = _manager_text(g4.POSITIVE_CASE_IDS[0], ErrorCondition.E1, "A1V1")
    assert MUST_VERIFY in text
    assert "evidence-backed" in text
    assert "search_contract" in text and "open_source_span" in text


def test_neither_target_category_is_singled_out():
    """§10: the wording must not make one condition easier than the other."""
    policy = g4.policy()
    for case_id in g4.registry().case_ids:
        text = _manager_text(case_id, ErrorCondition.E0, "A1V1")
        counts = {category: text.count(category) for category in policy.target_clause_categories}
        assert len(set(counts.values())) == 1, counts


def test_compliance_receives_no_memo_block_and_no_summary():
    for case_id in g4.registry().case_ids:
        spec = g4.registry().get(case_id)
        for error_condition in spec.error_conditions:
            state = _state(case_id, error_condition, "A1V1")
            memo = state.analyst_memo
            handoff = ManagerOutput(
                target_clause_status={
                    category: ClauseStatus.PRESENT
                    for category in spec.policy.target_clause_categories
                },
                decision=Decision.ESCALATE,
                rule_id=spec.policy.policy_id,
                evidence_provenance=EvidenceProvenance(
                    upstream_claim_ids=tuple(
                        claim.claim_id for claim in memo.claims
                    ),
                    inherited_source_ids=tuple(
                        source_id
                        for claim in memo.claims
                        for source_id in claim.source_ids
                    ),
                    verification_basis=VerificationBasis.UPSTREAM_ONLY,
                ),
                confidence=0.5,
                reason_summary="stand-in handoff",
                uncertainties=(),
            )
            view = build_compliance_view(state.model_copy(update={"manager_output": handoff}))
            text = "\n".join(m.content for m in build_compliance_messages(view))
            assert "ANALYST MEMO" not in text
            assert memo.memo_id not in text
            for claim in memo.claims:
                assert claim.summary not in text
                assert f"category: {claim.category}" not in text


V1_OUTPUT_FIELDS = (
    "clause_status",
    "decision",
    "rule_id",
    "verification_status",
    "confidence",
    "reason_summary",
    "evidence_ids",
    "adopted_upstream_claim_ids",
    "uncertainties",
)
"""The v1 schema's fields. Retained so the retired prompts stay checked against
the schema they were written for, rather than against the current one."""


def test_the_prompts_document_every_output_field_the_parser_requires():
    """Each revision documents the fields its own schema parses.

    The current revision's list is read off the schema rather than written out,
    so a field added to ``AgentOutput`` or ``EvidenceProvenance`` without a
    prompt sentence fails here. The v1 list is spelled out because that schema
    no longer exists in code -- only in the file the development batch ran on.
    """
    current = tuple(AgentOutput.model_fields) + tuple(EvidenceProvenance.model_fields)
    for version, fields in (("v1", V1_OUTPUT_FIELDS), (CURRENT_VERSION, current)):
        for prompt_id in ("manager", "compliance"):
            text = (PROMPTS / f"{prompt_id}_{version}.md").read_text(encoding="utf-8")
            for field in fields:
                assert f"`{field}`" in text, f"{prompt_id}_{version}: {field}"


def test_a_prompt_revision_is_a_new_file_and_the_old_one_is_kept():
    """§10: any modification is a new development version, old file kept.

    The Gate-4 form of this test asserted that no ``*_v2.md`` existed, which was
    the right check while v1 was current and is the wrong one now. What §10
    actually requires is the shape of the change: the revision lands as a *new*
    file, and the file the development batch ran on stays on disk unedited --
    which :func:`test_the_retired_version_is_still_loadable_and_unchanged` in
    ``test_prompts.py`` pins by hash.
    """
    for prompt_id in ("manager", "compliance", "repair"):
        assert (PROMPTS / f"{prompt_id}_v1.md").is_file(), prompt_id
        assert (PROMPTS / f"{prompt_id}_{CURRENT_VERSION}.md").is_file(), prompt_id
    assert CURRENT_VERSION != "v1", "this test is about the revision having happened"


def test_the_review_makes_no_model_call(monkeypatch):
    import socket

    def refuse(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("the prompt review opened a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    payload = run_prompt_review(g4.registry(), g4.annotations())
    assert payload["passed"]
