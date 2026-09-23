"""Synthetic cases for the Gate 3 experiment tests.

Gate 3 builds the experiment machinery and tests it on synthetic cases: choosing
the twelve real development contracts is a later gate's job, so nothing here is
CUAD data and nothing here is downloaded. What this module supplies is a small,
deliberately varied :class:`~pilot01.experiment.cases.CaseRegistry` that exercises
every structural branch the machinery has:

``CASE-A``
    A paired case over the fixture contract: a present change-of-control clause,
    run under both error conditions, so its E1 arm deletes a claim that asserted
    presence.
``CASE-B``
    A second paired case over the *same* contract, targeting a different clause
    category. Two cases sharing one contract is the ordinary arrangement -- a
    contract presents several auditable categories -- and it is what lets a test
    check that a run's paragraph ids belong to its own contract and to no other.
``CASE-C``
    A negative sentinel over a *second* contract: the target clause is genuinely
    absent, so the gold action is ACCEPT, the case runs the E0 arm only, and it
    declares no gold evidence span. That is a legitimate state, and the evidence
    metric has to report it as one rather than as a failure to overlap.

Everything downstream -- the plan, the batch runner, the scorer, the summary --
is driven from :func:`build_registry`, so a change to the fixture moves the whole
suite with it rather than leaving a stale hard-coded id somewhere.

**Declared, not derived.** :func:`scripted_responses` states what the two agents
say. It is a fixture, not a model of legal reasoning: nothing here decides what a
missing claim means, because that inference is the phenomenon under study. The
default scenario has the Manager conclude the gold clause status and Compliance
confirm it, which is the least interesting behaviour available and therefore the
right default for plumbing tests -- a test that wants error propagation says so
out loud by passing a different status.
"""

from __future__ import annotations

from functools import lru_cache

import sample_case
from pilot01.config import load_conditions_v1, load_models_v1, load_workflow_v1
from pilot01.experiment import (
    CaseRegistry,
    CaseSpec,
    ExperimentPaths,
    ExperimentRunner,
    RunSpec,
    ScriptedClientFactory,
    build_run_plan,
    gold_offsets_for_paragraph,
    score_experiment,
)
from pilot01.schemas import (
    AnalystMemo,
    ClauseStatus,
    Decision,
    ErrorCondition,
    MemoClaim,
    VerificationStatus,
)
from pilot01.source import ContractDocument, SourceLibrary
from pilot01.source.tools import OPEN_SOURCE_SPAN, SEARCH_CONTRACT

__all__ = [
    "CASE_A",
    "CASE_B",
    "CASE_C",
    "CASE_IDS",
    "CONTRACT_ID",
    "SECOND_CONTRACT_ID",
    "SECOND_CONTRACT_TEXT",
    "TARGET_PARAGRAPH_ID",
    "LAW_PARAGRAPH_ID",
    "SECOND_LAW_PARAGRAPH_ID",
    "GOLD_SPAN",
    "OPENABLE_PARAGRAPH",
    "TARGET_CLAIM",
    "NON_TARGET_CLAIM",
    "CHUNKING",
    "document_for",
    "paragraph_containing",
    "build_memo",
    "build_case",
    "build_registry",
    "registry_for",
    "scripted_responses",
    "default_responses",
    "script_for_plan",
    "conditions",
    "models",
    "workflow",
    "make_plan",
    "execute",
    "experiment_paths",
    "scored",
]


# --------------------------------------------------------------------------
# The contracts
# --------------------------------------------------------------------------

CONTRACT_ID = sample_case.CONTRACT_ID
SECOND_CONTRACT_ID = "CONTRACT-002"

SECOND_CONTRACT_SECTIONS: tuple[str, ...] = (
    (
        "1. TERM AND RENEWAL\n"
        "This Agreement begins on the Effective Date and continues for an initial "
        "term of two years. At the end of the initial term it renews automatically "
        "for successive periods of twelve months unless either party gives the other "
        "at least sixty days' written notice of non-renewal before the end of the "
        "then-current term. Neither party is obliged to give a reason for a notice of "
        "non-renewal, and a notice given in accordance with this Section is effective "
        "whether or not the other party objects to it. If the parties wish to vary "
        "the length of the initial term they must do so in writing signed by an "
        "authorised representative of each of them, and any such variation takes "
        "effect only from the date it is signed. Termination of this Agreement for "
        "any reason does not affect the provisions of Section 3 or Section 4, each of "
        "which survives termination."
    ),
    (
        "2. FEES AND PAYMENT\n"
        "The Customer shall pay the fees set out in the applicable order form within "
        "thirty days after the date of a correct invoice, without set-off or "
        "deduction. All amounts are stated and payable in United States dollars and "
        "are exclusive of any sales, use, or value-added tax, which the Customer "
        "shall pay in addition where the law requires it. The Supplier may increase "
        "the recurring fees once in any twelve-month period by giving the Customer at "
        "least sixty days' written notice, provided that no increase may exceed the "
        "percentage increase in the Consumer Price Index over the preceding twelve "
        "months. Amounts that remain unpaid for more than fifteen days after their "
        "due date accrue interest at the lesser of one and one-half percent per month "
        "and the maximum rate permitted by applicable law."
    ),
    (
        "3. CONFIDENTIALITY\n"
        "Each party shall keep the other party's Confidential Information "
        "confidential and shall use it only to perform its obligations or exercise "
        "its rights under this Agreement. A party may disclose Confidential "
        "Information to its employees, Affiliates, and professional advisers who need "
        "to know it for the purposes of this Agreement, provided that each such "
        "recipient is bound by confidentiality obligations at least as protective as "
        "those in this Section. Confidential Information does not include information "
        "that is or becomes public through no fault of the receiving party, that the "
        "receiving party already held without a duty of confidence, or that the "
        "receiving party independently develops without use of the disclosing party's "
        "Confidential Information. On termination each party shall return or destroy "
        "the other party's Confidential Information in its possession."
    ),
    (
        "4. GOVERNING LAW\n"
        "This Agreement, and any dispute or claim arising out of or in connection "
        "with it, is governed by and construed in accordance with the laws of the "
        "State of New York, without regard to its conflict-of-laws principles. The "
        "parties irrevocably submit to the exclusive jurisdiction of the state and "
        "federal courts sitting in New York County, New York, and waive any objection "
        "to venue in those courts. Each party waives any right to a trial by jury in "
        "any proceeding arising out of or in connection with this Agreement. Nothing "
        "in this Section prevents either party from seeking injunctive or other "
        "equitable relief in any court of competent jurisdiction. This Agreement is "
        "the entire agreement between the parties on its subject matter."
    ),
)

SECOND_CONTRACT_TEXT = "\n\n".join(SECOND_CONTRACT_SECTIONS)
"""A second contract, deliberately without an assignment or change-of-control clause.

``CASE-C`` is a negative sentinel for exactly that reason: its target clause is
genuinely absent from the contract it is about, which is what makes its gold
action ACCEPT rather than a fiction asserted over a contract that contains the
clause.
"""

CHUNKING: dict[str, int] = {
    "target_chars": sample_case.CONTRACT_TARGET_CHARS,
    "max_chars": sample_case.CONTRACT_MAX_CHARS,
}
"""One paragraph per section, for both fixture contracts.

Recorded on the registry as well, because paragraph ids are derived from the
chunking: a registry built with different sizes would mint different ids for the
same text, so the sizes are part of the frozen input rather than a detail of
whichever process built the library.
"""


@lru_cache(maxsize=1)
def _library() -> SourceLibrary:
    return SourceLibrary.from_texts(
        {CONTRACT_ID: sample_case.CONTRACT_TEXT, SECOND_CONTRACT_ID: SECOND_CONTRACT_TEXT},
        **CHUNKING,
    )


def document_for(contract_id: str) -> ContractDocument:
    """One fixture contract, paragraphized exactly as the registry will."""
    return _library().get(contract_id)


def paragraph_containing(contract_id: str, phrase: str) -> str:
    """The id of the paragraph of ``contract_id`` whose text contains ``phrase``.

    Derived rather than hard-coded, so a change to a fixture contract moves the
    tests with it instead of silently pointing them at the wrong paragraph.
    """
    matches = [
        paragraph.paragraph_id
        for paragraph in document_for(contract_id).paragraphs
        if phrase in paragraph.text
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one paragraph of {contract_id} containing {phrase!r}, "
            f"found {len(matches)}"
        )
    return matches[0]


TARGET_PARAGRAPH_ID = paragraph_containing(CONTRACT_ID, sample_case.CHANGE_OF_CONTROL_PHRASE)
"""The clause paragraph of the first contract. Both A and B are about this one.

The section covers assignment *and* change of control in a single paragraph,
which is why two cases with different target categories can share a gold span
without sharing a memo.
"""

LAW_PARAGRAPH_ID = paragraph_containing(CONTRACT_ID, sample_case.GOVERNING_LAW_PHRASE)
SECOND_LAW_PARAGRAPH_ID = paragraph_containing(
    SECOND_CONTRACT_ID, "construed in accordance with the laws of the State of New York"
)
"""A paragraph that is emphatically *not* the target clause, for overlap tests."""


# --------------------------------------------------------------------------
# The cases
# --------------------------------------------------------------------------

CASE_A = "CASE-A"
CASE_B = "CASE-B"
CASE_C = "CASE-C"
CASE_IDS: tuple[str, ...] = (CASE_A, CASE_B, CASE_C)

TARGET_CLAIM: dict[str, str] = {CASE_A: "A-CL-1", CASE_B: "B-CL-1", CASE_C: "C-CL-1"}

#: A claim CASE-A's memo carries on *both* arms, unlike the target claim, which
#: the E1 arm deletes. Adoption tests need this one: adopting a claim the memo
#: still contains is ordinary reading, whereas adopting the deleted one is the
#: integrity finding the scorer reports.
NON_TARGET_CLAIM: dict[str, str] = {
    CASE_A: "A-CL-2",
    CASE_B: "B-CL-2",
    CASE_C: "C-CL-2",
}

GOLD_SPAN: dict[str, str | None] = {
    CASE_A: TARGET_PARAGRAPH_ID,
    CASE_B: TARGET_PARAGRAPH_ID,
    CASE_C: None,
}
"""Which paragraph, if any, is the case's gold evidence.

``None`` for the sentinel: there is no clause to point at, so the case declares
no gold span at all. The evidence metric reports that as
:attr:`~pilot01.experiment.scoring.EvidenceFailure.NO_GOLD_SPAN`, which is a
statement that the question cannot be asked -- not a failure to answer it.
"""

OPENABLE_PARAGRAPH: dict[str, str] = {
    CASE_A: TARGET_PARAGRAPH_ID,
    CASE_B: TARGET_PARAGRAPH_ID,
    CASE_C: SECOND_LAW_PARAGRAPH_ID,
}
"""The paragraph the default scenario has the agents open, per case."""


def build_memo(
    *,
    case_id: str,
    memo_id: str,
    target_claim_id: str,
    target_category: str,
    target_status: ClauseStatus,
    target_summary: str,
    target_source_ids: tuple[str, ...] = (),
    extras: tuple[MemoClaim, ...] = (),
) -> AnalystMemo:
    """A correct (E0) memo: one target claim, then whatever else the case carries."""
    return AnalystMemo(
        memo_id=memo_id,
        case_id=case_id,
        claims=(
            MemoClaim(
                claim_id=target_claim_id,
                category=target_category,
                status=target_status,
                summary=target_summary,
                source_ids=target_source_ids,
            ),
            *extras,
        ),
    )


def build_case(
    *,
    case_id: str,
    memo: AnalystMemo,
    target_category: str,
    gold_clause_status: ClauseStatus,
    gold_paragraph_id: str | None,
    error_conditions: tuple[ErrorCondition, ...] = (ErrorCondition.E0, ErrorCondition.E1),
    contract_id: str = CONTRACT_ID,
    contract_text: str | None = None,
) -> CaseSpec:
    """One case, with its gold span derived from the paragraph it is about.

    ``gold_clause_status`` is the pre-per-category spelling and states the status
    of ``target_category``; the policy's *other* target category is ``absent``,
    because no fixture memo carries a claim in it. That is what makes
    ``target_category`` the case's only policy trigger, and therefore what makes
    the E1 omission a manipulation of the decision rather than a no-op.

    The other category is derived from ``target_category`` rather than fixed, so
    that a case built about the second category (``CASE_B`` is about
    ``assignment``) labels the first one absent instead of colliding with itself.
    """
    text = contract_text
    if text is None:
        text = (
            sample_case.CONTRACT_TEXT if contract_id == CONTRACT_ID else SECOND_CONTRACT_TEXT
        )
    offsets: tuple[int, ...] = ()
    if gold_paragraph_id is not None:
        offsets = gold_offsets_for_paragraph(document_for(contract_id), gold_paragraph_id)
    other_category = next(
        category
        for category in sample_case.TARGET_CLAUSE_CATEGORIES
        if category != target_category
    )
    return CaseSpec(
        case_id=case_id,
        contract_id=contract_id,
        contract_text=text,
        target_category=target_category,
        policy=sample_case.build_policy(),
        memo=memo,
        gold_target_clause_status={
            target_category: gold_clause_status,
            other_category: ClauseStatus.ABSENT,
        },
        gold_evidence_offsets=offsets,
        is_negative_sentinel=gold_clause_status is ClauseStatus.ABSENT,
        error_conditions=error_conditions,
        omission_target_claim_id=(
            memo.claims[0].claim_id if ErrorCondition.E1 in error_conditions else None
        ),
    )


def _case_a() -> CaseSpec:
    return build_case(
        case_id=CASE_A,
        memo=build_memo(
            case_id=CASE_A,
            memo_id="MEMO-A",
            target_claim_id=TARGET_CLAIM[CASE_A],
            target_category="change_of_control",
            target_status=ClauseStatus.PRESENT,
            target_summary=(
                "Section 3 requires written consent for a change of control, and "
                "treats a majority share transfer as one."
            ),
            target_source_ids=("S-3.2", "S-3.3"),
            extras=(
                MemoClaim(
                    claim_id="A-CL-2",
                    category="governing_law",
                    status=ClauseStatus.PRESENT,
                    summary="Governing law is the State of Delaware.",
                    source_ids=("S-8.1",),
                ),
                MemoClaim(
                    claim_id="A-CL-3",
                    category="liability_cap",
                    status=ClauseStatus.UNKNOWN,
                    summary="A liability cap is referenced but no amount is stated.",
                    source_ids=("S-5.4",),
                ),
            ),
        ),
        target_category="change_of_control",
        gold_clause_status=ClauseStatus.PRESENT,
        gold_paragraph_id=GOLD_SPAN[CASE_A],
    )


def _case_b() -> CaseSpec:
    return build_case(
        case_id=CASE_B,
        memo=build_memo(
            case_id=CASE_B,
            memo_id="MEMO-B",
            target_claim_id=TARGET_CLAIM[CASE_B],
            target_category="assignment",
            target_status=ClauseStatus.PRESENT,
            target_summary=(
                "Section 3 prohibits assignment without the other party's prior "
                "written consent."
            ),
            target_source_ids=("S-3.1",),
            extras=(
                MemoClaim(
                    claim_id="B-CL-2",
                    category="confidentiality",
                    status=ClauseStatus.PRESENT,
                    summary="Confidentiality obligations survive termination.",
                    source_ids=("S-7.1",),
                ),
            ),
        ),
        target_category="assignment",
        gold_clause_status=ClauseStatus.PRESENT,
        gold_paragraph_id=GOLD_SPAN[CASE_B],
    )


def _case_c() -> CaseSpec:
    return build_case(
        case_id=CASE_C,
        contract_id=SECOND_CONTRACT_ID,
        memo=build_memo(
            case_id=CASE_C,
            memo_id="MEMO-C",
            target_claim_id=TARGET_CLAIM[CASE_C],
            target_category="change_of_control",
            target_status=ClauseStatus.ABSENT,
            target_summary="No change-of-control or assignment clause was found.",
            extras=(
                MemoClaim(
                    claim_id="C-CL-2",
                    category="governing_law",
                    status=ClauseStatus.PRESENT,
                    summary="Governing law is the State of New York.",
                    source_ids=("S-4.1",),
                ),
            ),
        ),
        target_category="change_of_control",
        gold_clause_status=ClauseStatus.ABSENT,
        gold_paragraph_id=GOLD_SPAN[CASE_C],
        error_conditions=(ErrorCondition.E0,),
    )


def build_registry() -> CaseRegistry:
    """The fixture registry: two paired cases and one sentinel, over two contracts."""
    return CaseRegistry(
        [_case_a(), _case_b(), _case_c()],
        target_chars=CHUNKING["target_chars"],
        max_chars=CHUNKING["max_chars"],
    )


def registry_for(case_ids: tuple[str, ...]) -> CaseRegistry:
    """A registry holding only the named fixture cases, for a narrower plan."""
    builders = {CASE_A: _case_a, CASE_B: _case_b, CASE_C: _case_c}
    unknown = [case_id for case_id in case_ids if case_id not in builders]
    if unknown:
        raise AssertionError(f"no fixture case {unknown}; known: {sorted(builders)}")
    return CaseRegistry(
        [builders[case_id]() for case_id in case_ids],
        target_chars=CHUNKING["target_chars"],
        max_chars=CHUNKING["max_chars"],
    )


# --------------------------------------------------------------------------
# Declared raw model responses
# --------------------------------------------------------------------------


def scripted_responses(
    *,
    source_access: bool,
    verification_required: bool,
    paragraph_id: str,
    manager_status: ClauseStatus = ClauseStatus.PRESENT,
    manager_decision: Decision = Decision.ESCALATE,
    compliance_status: ClauseStatus | None = None,
    compliance_decision: Decision | None = None,
    manager_adopted: tuple[str, ...] = (),
    compliance_adopted: tuple[str, ...] = (),
    manager_evidence: tuple[str, ...] | None = None,
    compliance_evidence: tuple[str, ...] | None = None,
    query: str = "change of control",
    target_categories: tuple[str, ...] = sample_case.TARGET_CLAUSE_CATEGORIES,
    reason_summary: str = "Declared by the experiment fixture; no inference is made.",
) -> dict[str, tuple[str, ...]]:
    """The raw responses for one run's two roles.

    Under source access each role gets three turns -- search, open, answer --
    which is the shortest sequence that can satisfy V1: a search alone returns
    candidates, and reading one is what turns a candidate into evidence. Without
    source access each role gets a single turn, because there is no tool to call
    and a tool request would be refused.

    The answer's evidence ids follow the same split. Under source access a node
    cites the paragraph it opened, so its verification claim is backed by
    something the log can check. Without it, a node cites a memo source id --
    which is what an agent with no tools actually has, and which the evidence
    metric records as ``not_self_opened`` rather than as a citation of the clause.
    """
    final_manager_status = manager_status
    final_compliance_status = (
        manager_status if compliance_status is None else compliance_status
    )
    final_compliance_decision = (
        manager_decision if compliance_decision is None else compliance_decision
    )
    default_evidence = (paragraph_id,) if source_access else ("S-3.2",)
    manager_ids = default_evidence if manager_evidence is None else manager_evidence
    compliance_ids = default_evidence if compliance_evidence is None else compliance_evidence
    status = (
        VerificationStatus.VERIFIED if verification_required else VerificationStatus.NOT_CHECKED
    )

    manager_text = sample_case.manager_response_text(
        clause_status=final_manager_status,
        decision=manager_decision,
        evidence_ids=manager_ids,
        verification_status=status,
        adopted_upstream_claim_ids=manager_adopted,
        reason_summary=reason_summary,
    )
    compliance_text = sample_case.compliance_response_text(
        clause_status=final_compliance_status,
        decision=final_compliance_decision,
        evidence_ids=compliance_ids,
        verification_status=status,
        adopted_upstream_claim_ids=compliance_adopted,
        reason_summary=reason_summary,
    )

    if not source_access:
        return {"manager": (manager_text,), "compliance": (compliance_text,)}

    def with_tools(text: str) -> tuple[str, ...]:
        searches = [sample_case.tool_request_text(SEARCH_CONTRACT, query=query)]
        if verification_required:
            for category in target_categories:
                if category.replace("_", " ").casefold() not in query.casefold():
                    searches.append(
                        sample_case.tool_request_text(
                            SEARCH_CONTRACT, query=category.replace("_", " ")
                        )
                    )
        return (*searches,
                sample_case.tool_request_text(OPEN_SOURCE_SPAN, paragraph_id=paragraph_id),
                text)

    return {"manager": with_tools(manager_text), "compliance": with_tools(compliance_text)}


def default_responses(job: RunSpec, registry: CaseRegistry) -> dict[str, tuple[str, ...]]:
    """The default scenario for one job: both agents conclude the gold status.

    The least interesting behaviour available, and deliberately so. A plumbing
    test wants the pipeline to be exercised, not a phenomenon to be observed;
    a test that wants the omission to propagate passes ``manager_status=`` a
    different value to :func:`scripted_responses` itself.
    """
    case = registry.get(job.case_id)
    condition = job.condition_id
    source_access = job.source_access
    adopted = (
        (TARGET_CLAIM[job.case_id],)
        if job.error_condition is ErrorCondition.E0
        else ()
    )
    return scripted_responses(
        source_access=source_access,
        verification_required=job.verification_required,
        paragraph_id=OPENABLE_PARAGRAPH[job.case_id],
        manager_status=case.gold_status_for(case.target_category),
        manager_decision=case.gold_action,
        compliance_status=case.gold_status_for(case.target_category),
        compliance_decision=case.gold_action,
        manager_adopted=adopted,
        compliance_adopted=adopted,
        query=case.target_category.replace("_", " "),
    )


def script_for_plan(
    plan,
    registry: CaseRegistry,
    *,
    behaviour=None,
) -> dict[str, dict[str, tuple[str, ...]]]:
    """A complete response script for every job of a plan.

    ``behaviour`` is ``(job, registry) -> {role: responses}``; the default is
    :func:`default_responses`. Every job is covered, so a batch driven by this
    script cannot fall back to an undeclared run.
    """
    choose = default_responses if behaviour is None else behaviour
    return {job.run_id: choose(job, registry) for job in plan.jobs}


# --------------------------------------------------------------------------
# Running and scoring, so a test can say what it means in three lines
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def conditions():
    return load_conditions_v1()


@lru_cache(maxsize=1)
def models():
    return load_models_v1()


@lru_cache(maxsize=1)
def workflow():
    return load_workflow_v1()


def make_plan(
    *,
    experiment_id: str = "EXP-1",
    registry: CaseRegistry | None = None,
    repeat_count: int = 1,
    seed: int = 7,
    condition_ids=None,
    case_ids=None,
):
    """A plan over the fixture registry, with the real v1 configs."""
    return build_run_plan(
        experiment_id=experiment_id,
        registry=build_registry() if registry is None else registry,
        conditions=conditions(),
        repeat_count=repeat_count,
        seed=seed,
        models=models(),
        condition_ids=condition_ids,
        case_ids=case_ids,
    )


def execute(
    plan,
    registry: CaseRegistry,
    paths: ExperimentPaths,
    *,
    behaviour=None,
    factory=None,
    **run_kwargs,
):
    """Run a plan into ``paths`` with the scripted factory, and return the batch."""
    if factory is None:
        factory = ScriptedClientFactory(script_for_plan(plan, registry, behaviour=behaviour))
    runner = ExperimentRunner(
        plan=plan,
        registry=registry,
        conditions=conditions(),
        workflow=workflow(),
        models=models(),
        client_factory=factory,
        paths=paths,
    )
    return runner.run(**run_kwargs)


def experiment_paths(root, experiment_id: str = "EXP-1") -> ExperimentPaths:
    """The three output trees for one experiment, under a test's tmp directory."""
    return ExperimentPaths(root=root, experiment_id=experiment_id)


def scored(
    plan,
    registry: CaseRegistry,
    paths: ExperimentPaths,
    *,
    behaviour=None,
    **run_kwargs,
):
    """Run a plan and score it. The whole pipeline, for tests about the pipeline."""
    execute(plan, registry, paths, behaviour=behaviour, **run_kwargs)
    return score_experiment(paths.raw_dir, registry=registry)
