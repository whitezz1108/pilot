"""The canonical pilot case used across the test suite.

One correct (E0) analyst memo over one contract, plus the identifiers needed to
derive its E1 omission counterpart. Everything here is synthetic: no CUAD data,
no downloaded contracts.

The memo carries exactly **one** claim in the target clause categories, which is
what makes the omission experiment meaningful: deleting that single claim leaves
no target claim at all. What a downstream agent *does* with that gap is the
empirical question the pilot exists to answer, so nothing in ``src/`` answers it
-- the fake agents replay whatever a test declares (see
:mod:`pilot01.workflow.nodes.script`), and these tests declare it explicitly.

The gold label is ESCALATE for *both* arms: the omission changes the memo, not
the contract.

**Clause categories.** ``TARGET_CLAUSE_CATEGORIES`` below is a synthetic
placeholder standing in for the audited CUAD categories, which are frozen only
after the dataset audit. It lives here, in a test fixture, and deliberately not
in ``src/``: a category list shipped as a code default would silently become the
policy under test.
"""

from __future__ import annotations

import hashlib

from pilot01.schemas import (
    AnalystMemo,
    ClauseStatus,
    ComplianceOutput,
    Decision,
    ExperimentalPolicy,
    ManagerOutput,
    MemoClaim,
    VerificationStatus,
    policy_01,
)
from pilot01.source import ContractDocument, SourceLibrary

CASE_ID = "CASE-001"
CONTRACT_ID = "CONTRACT-001"

TARGET_CLAUSE_CATEGORIES = ("change_of_control", "assignment")
"""Fixture-only placeholder. Gate 2 replaces these after the dataset audit."""

TARGET_CATEGORY = "change_of_control"
"""The category of the single claim the E1 arm deletes."""

TARGET_CLAIM_ID = "CL-1"
TARGET_CLAIM_SOURCE_IDS = ("S-3.2", "S-3.3")


def build_policy() -> ExperimentalPolicy:
    """POLICY-01 with this case's target categories supplied explicitly."""
    return policy_01(target_clause_categories=TARGET_CLAUSE_CATEGORIES)


def build_e0_memo() -> AnalystMemo:
    """The correct frozen analyst memo."""
    return AnalystMemo(
        memo_id="MEMO-001",
        case_id=CASE_ID,
        claims=(
            MemoClaim(
                claim_id=TARGET_CLAIM_ID,
                category=TARGET_CATEGORY,
                status=ClauseStatus.PRESENT,
                summary=(
                    "Section 3.2 requires written consent for a change of control; "
                    "Section 3.3 treats a majority share transfer as a change of control."
                ),
                source_ids=TARGET_CLAIM_SOURCE_IDS,
            ),
            MemoClaim(
                claim_id="CL-2",
                category="termination_for_convenience",
                status=ClauseStatus.ABSENT,
                summary="No termination-for-convenience right was found.",
                source_ids=(),
            ),
            MemoClaim(
                claim_id="CL-3",
                category="governing_law",
                status=ClauseStatus.PRESENT,
                summary="Governing law is the State of Delaware.",
                source_ids=("S-9.1",),
            ),
            MemoClaim(
                claim_id="CL-4",
                category="liability_cap",
                status=ClauseStatus.UNKNOWN,
                summary="The liability cap is referenced but its amount is not stated.",
                source_ids=("S-7.4",),
            ),
        ),
    )


# --------------------------------------------------------------------------
# Declared fake-agent scenarios
#
# The fake agents replay these verbatim. Nothing here is derived from the memo,
# the policy or the governance flags, so a test that wants "the Manager reads
# the gap as absent" has to say so out loud.
# --------------------------------------------------------------------------


def manager_output(
    *,
    clause_status: ClauseStatus = ClauseStatus.PRESENT,
    decision: Decision = Decision.ESCALATE,
    rule_id: str = "POLICY-01",
    verification_status: VerificationStatus = VerificationStatus.NOT_CHECKED,
    evidence_ids: tuple[str, ...] = TARGET_CLAIM_SOURCE_IDS,
    adopted_upstream_claim_ids: tuple[str, ...] = (TARGET_CLAIM_ID,),
    confidence: float = 0.9,
    reason_summary: str = "Declared by the test fixture; the fake Manager infers nothing.",
    uncertainties: tuple[str, ...] = (),
) -> ManagerOutput:
    """A scripted Manager handoff."""
    return ManagerOutput(
        clause_status=clause_status,
        decision=decision,
        rule_id=rule_id,
        verification_status=verification_status,
        evidence_ids=evidence_ids,
        adopted_upstream_claim_ids=adopted_upstream_claim_ids,
        confidence=confidence,
        reason_summary=reason_summary,
        uncertainties=uncertainties,
    )


def compliance_output(
    *,
    clause_status: ClauseStatus = ClauseStatus.PRESENT,
    decision: Decision = Decision.ESCALATE,
    rule_id: str = "POLICY-01",
    verification_status: VerificationStatus = VerificationStatus.NOT_CHECKED,
    evidence_ids: tuple[str, ...] = TARGET_CLAIM_SOURCE_IDS,
    adopted_upstream_claim_ids: tuple[str, ...] = (TARGET_CLAIM_ID,),
    confidence: float = 0.9,
    reason_summary: str = "Declared by the test fixture; the fake Compliance infers nothing.",
    uncertainties: tuple[str, ...] = (),
) -> ComplianceOutput:
    """A scripted Compliance decision."""
    return ComplianceOutput(
        clause_status=clause_status,
        decision=decision,
        rule_id=rule_id,
        verification_status=verification_status,
        evidence_ids=evidence_ids,
        adopted_upstream_claim_ids=adopted_upstream_claim_ids,
        confidence=confidence,
        reason_summary=reason_summary,
        uncertainties=uncertainties,
    )


# --------------------------------------------------------------------------
# Declared raw model responses
#
# The Gate 2 tests script these into a ScriptedModelClient, which replays the
# text verbatim through the real parsing and repair path. Like the fake agent
# scripts above, they are declarations by the test, not derived from anything.
# --------------------------------------------------------------------------


def manager_response_text(**kwargs) -> str:
    """A raw JSON response body for the Manager role."""
    return manager_output(**kwargs).model_dump_json()


def compliance_response_text(**kwargs) -> str:
    """A raw JSON response body for the Compliance role."""
    return compliance_output(**kwargs).model_dump_json()


def fenced(text: str, language: str = "json") -> str:
    """Wrap a response in a markdown fence, as models often do unprompted."""
    return f"```{language}\n{text}\n```"


# --------------------------------------------------------------------------
# The fixture contract
#
# A synthetic agreement, written here rather than downloaded, so the source
# layer's tests need no CUAD data and no network. Its clauses deliberately
# agree with the E0 memo above: the change-of-control clause is present, the
# governing law is Delaware, the liability cap is referenced without stating an
# amount, and there is no termination-for-convenience right.
#
# One section per blank-line-separated block, and every section body is longer
# than ``CONTRACT_TARGET_CHARS``, so paragraphization yields exactly one
# paragraph per section -- which is what lets a test say "open the paragraph
# that states the change-of-control clause" and mean it. The real CUAD path uses
# the layer's own default sizes; the sizes here are chosen for legibility of the
# fixture, not to model the corpus.
# --------------------------------------------------------------------------

CONTRACT_SECTIONS: tuple[str, ...] = (
    (
        "1. DEFINITIONS\n"
        "As used in this Agreement, the following terms have the meanings set out "
        "below. \"Affiliate\" means any entity that directly or indirectly controls, "
        "is controlled by, or is under common control with a party, where \"control\" "
        "means the ownership of more than fifty percent of the voting securities of an "
        "entity. \"Business Day\" means any day other than a Saturday, a Sunday, or a "
        "day on which banks in the State of Delaware are authorised or required by law "
        "to remain closed. \"Confidential Information\" means non-public information "
        "disclosed by one party to the other in connection with this Agreement, whether "
        "disclosed in writing, orally, or by inspection of tangible objects, and "
        "whether or not marked as confidential at the time of disclosure. \"Effective "
        "Date\" means the date on which the last party signs this Agreement."
    ),
    (
        "2. TERM\n"
        "This Agreement begins on the Effective Date and continues for an initial term "
        "of three years unless it is terminated earlier in accordance with its terms. "
        "At the end of the initial term, this Agreement renews automatically for "
        "successive periods of twelve months, unless either party gives the other "
        "written notice of non-renewal at least ninety days before the end of the "
        "then-current term. No renewal, and no extension of the term by any other "
        "means, alters the provisions of Section 3 or Section 5, each of which survives "
        "for so long as any obligation under this Agreement remains outstanding. The "
        "parties may agree in writing to a longer initial term, but any such agreement "
        "must be signed by an authorised representative of each party and must refer "
        "expressly to this Section."
    ),
    (
        "3. ASSIGNMENT AND CHANGE OF CONTROL\n"
        "Neither party may assign this Agreement, in whole or in part, whether by "
        "operation of law or otherwise, without the prior written consent of the other "
        "party. A transfer of a majority of the voting securities of a party, a merger "
        "or consolidation to which a party is a constituent party, or a sale of all or "
        "substantially all of a party's assets, is a change of control of that party. A "
        "change of control of either party is deemed an assignment of this Agreement "
        "for the purposes of this Section, and the party undergoing the change of "
        "control must give the other party written notice within ten Business Days "
        "after the change of control takes effect. Consent to an assignment may be "
        "withheld in the other party's sole discretion, and any purported assignment "
        "made without the required consent is void."
    ),
    (
        "4. FEES AND PAYMENT\n"
        "The Customer shall pay the fees set out in the applicable order form, without "
        "set-off or deduction, within thirty days after the date of a correct invoice. "
        "All amounts are stated and payable in United States dollars and are exclusive "
        "of any sales, use, or value-added tax, which the Customer shall pay in "
        "addition where the law requires it. The Supplier may increase the recurring "
        "fees once in any twelve-month period by giving the Customer at least sixty "
        "days' written notice, provided that no increase may exceed the percentage "
        "increase in the Consumer Price Index over the preceding twelve months. Amounts "
        "that remain unpaid for more than fifteen days after their due date accrue "
        "interest at the lesser of one and one-half percent per month and the maximum "
        "rate permitted by applicable law."
    ),
    (
        "5. LIMITATION OF LIABILITY\n"
        "Except for the excluded matters described in this Section, neither party is "
        "liable to the other for any indirect, incidental, special, consequential, or "
        "punitive damages, or for any loss of profit, revenue, data, or business "
        "opportunity, however arising and whether or not the party was advised of the "
        "possibility of such loss. Each party's total aggregate liability arising out "
        "of or in connection with this Agreement is limited to the fees paid and "
        "payable by the Customer in the twelve months immediately preceding the event "
        "giving rise to the claim. The excluded matters are: a party's indemnification "
        "obligations, a party's breach of Section 7, and a party's gross negligence or "
        "wilful misconduct, none of which is subject to the limitation in this Section. "
        "The liability cap stated in this Section is a fixed amount and is not subject "
        "to any multiplier, escalation, or adjustment."
    ),
    (
        "6. TERMINATION\n"
        "Either party may terminate this Agreement immediately on written notice if the "
        "other party commits a material breach of this Agreement and fails to cure that "
        "breach within thirty days after receiving written notice describing it in "
        "reasonable detail. Either party may terminate this Agreement immediately on "
        "written notice if the other party becomes insolvent, makes a general "
        "assignment for the benefit of creditors, or has a receiver appointed over all "
        "or substantially all of its assets. On termination, the Customer shall pay all "
        "fees accrued through the effective date of termination, and each party shall "
        "return or destroy the other party's Confidential Information in accordance "
        "with Section 7. Sections 5, 7, and 8 survive termination of this Agreement. "
        "Termination of this Agreement does not relieve either party of any obligation "
        "that arose before the effective date of termination."
    ),
    (
        "7. CONFIDENTIALITY\n"
        "Each party shall keep the other party's Confidential Information "
        "confidential, shall use it only to perform its obligations or exercise its "
        "rights under this Agreement, and shall protect it using at least the degree of "
        "care it uses to protect its own confidential information of like importance, "
        "and in no event less than a reasonable degree of care. A party may disclose "
        "Confidential Information to its employees, Affiliates, and professional "
        "advisers who need to know it for the purposes of this Agreement, provided that "
        "each such recipient is bound by confidentiality obligations at least as "
        "protective as those in this Section. Confidential Information does not include "
        "information that is or becomes public through no fault of the receiving party, "
        "that the receiving party already held without a duty of confidence, or that "
        "the receiving party independently develops without use of the disclosing "
        "party's Confidential Information."
    ),
    (
        "8. GOVERNING LAW\n"
        "This Agreement, and any dispute or claim arising out of or in connection with "
        "it, is governed by and construed in accordance with the laws of the State of "
        "Delaware, without regard to its conflict-of-laws principles. The parties "
        "irrevocably submit to the exclusive jurisdiction of the state and federal "
        "courts sitting in New Castle County, Delaware, and waive any objection to "
        "venue in those courts. Each party waives any right to a trial by jury in any "
        "proceeding arising out of or in connection with this Agreement. Nothing in "
        "this Section prevents either party from seeking injunctive or other equitable "
        "relief in any court of competent jurisdiction to protect its Confidential "
        "Information. This Agreement is the entire agreement between the parties on "
        "its subject matter and supersedes all prior discussions and proposals relating "
        "to it."
    ),
)

CONTRACT_TEXT = "\n\n".join(CONTRACT_SECTIONS)
"""The fixture contract, exactly as the source layer will paragraphize it."""

CONTRACT_TEXT_HASH = "sha256:" + hashlib.sha256(CONTRACT_TEXT.encode("utf-8")).hexdigest()
"""The real digest of :data:`CONTRACT_TEXT`, so the fixture's identity is not a fiction."""

CONTRACT_TARGET_CHARS = 600
CONTRACT_MAX_CHARS = 1800
"""Chunking sizes for the fixture. See the note above for why they are not the defaults."""

CHANGE_OF_CONTROL_PHRASE = "change of control"
GOVERNING_LAW_PHRASE = "construed in accordance with the laws of the State of Delaware"
LIABILITY_CAP_PHRASE = "total aggregate liability"


def build_source_library() -> SourceLibrary:
    """The one-contract library the fixture case is about."""
    return SourceLibrary.from_texts(
        {CONTRACT_ID: CONTRACT_TEXT},
        target_chars=CONTRACT_TARGET_CHARS,
        max_chars=CONTRACT_MAX_CHARS,
    )


def build_contract_document() -> ContractDocument:
    """The fixture contract as the source layer sees it."""
    return build_source_library().get(CONTRACT_ID)


def paragraph_id_containing(phrase: str) -> str:
    """The id of the fixture paragraph whose text contains ``phrase``.

    Tests use this instead of hard-coding an id, so a change to the fixture text
    moves the tests with it rather than silently pointing them at the wrong
    paragraph.
    """
    matches = [
        paragraph.paragraph_id
        for paragraph in build_contract_document().paragraphs
        if phrase in paragraph.text
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one fixture paragraph containing {phrase!r}, found "
            f"{len(matches)}"
        )
    return matches[0]


def tool_request_text(tool: str, **arguments) -> str:
    """A raw model response that asks for a tool call instead of answering.

    The envelope is the one the prompts describe: a single JSON object whose only
    key is ``tool_request``.
    """
    import json

    return json.dumps({"tool_request": {"tool": tool, "arguments": arguments}})
