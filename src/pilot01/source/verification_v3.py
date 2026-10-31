"""Submission checks for disclosure experiments. No gold or semantic judging."""
from __future__ import annotations

from ..schemas import VerificationBasis, VerificationStatus, require_policy_targets
from .verification import (
    VerificationOutcome, VerificationFailure as F, TargetSearchAudit, CitationAudit,
    _paragraph_references,
)

RULES = (
    "Report exactly the policy target categories, without missing or extra categories.",
    "State the policy_id as rule_id.",
    "Perform a successful search tagged with each target_category in this invocation; one request has one target, and paging does not count for another target.",
    "Report at least one relied_paragraph_id. Every claimed opened ID and every relied ID must have been opened by this node in this invocation; every relied ID must also be listed in opened_paragraph_ids.",
    "Every reported source ID must belong to this contract and have been observed here (returned, opened, or received upstream). Inherited IDs must actually have arrived upstream; candidate IDs are not adopted evidence.",
    "Declare verification_basis=self_checked consistently with actual source opening. Explicitly supply relied_paragraph_ids and candidate_source_ids. A candidate cannot simultaneously be relied upon.",
)


def evaluate_verification_v3(*, output, policy, ledger, document, verification_required=True,
                             source_tools_available=True, node="", invocation=0):
    p = output.evidence_provenance
    diagnostics, failures = [], []
    def fail(reason, message):
        if reason not in failures:
            failures.append(reason)
        diagnostics.append(message)
    try:
        require_policy_targets(output.target_clause_status, policy)
    except ValueError as exc:
        fail(F.CHECK_INCOMPLETE, str(exc))
    if output.rule_id != policy.policy_id:
        fail(F.RULE_NOT_STATED, "rule_id differs from policy_id")
    target_audit = tuple(TargetSearchAudit(category=c, matched_queries=tuple(
        e.query for e in ledger.search_events if e.target_category == c
    )) for c in policy.target_clause_categories)
    if any(not a.matched_queries for a in target_audit):
        fail(F.TARGET_NOT_SEARCHED, "missing successful target-tagged search")
    relied, candidates = p.relied_paragraph_ids or (), p.candidate_source_ids or ()
    if p.relied_paragraph_ids is None or p.candidate_source_ids is None:
        fail(F.CHECK_INCOMPLETE, "explicit relied and candidate lists required")
    if not relied:
        fail(F.NO_EVIDENCE_CITED, "no adopted source")
    if set(relied) & set(candidates):
        fail(F.CHECK_INCOMPLETE, "candidate is also adopted")
    if not source_tools_available:
        fail(F.SOURCE_TOOLS_UNAVAILABLE, "source tools unavailable")
    audits = []
    for name, ids in (("opened_paragraph_ids", p.opened_paragraph_ids),
                      ("relied_paragraph_ids", relied), ("inherited_source_ids", p.inherited_source_ids),
                      ("candidate_source_ids", candidates)):
        for pid in ids:
            exists = document is not None and document.paragraph(pid) is not None
            observed = pid in ledger.observed_ids
            audits.append(CitationAudit(source_id=pid, reported_as=name, paragraph_reference=True,
                                        self_opened=pid in ledger.opened_ids, in_contract=exists, observed=observed))
            if not exists:
                fail(F.EVIDENCE_NOT_IN_CONTRACT, f"{name}: source outside contract")
            if not observed:
                fail(F.EVIDENCE_NOT_OBSERVED, f"{name}: source never observed")
            if name in ("opened_paragraph_ids", "relied_paragraph_ids") and pid not in ledger.opened_ids:
                fail(F.EVIDENCE_NOT_OPENED, f"{name}: source not opened here")
            if name == "relied_paragraph_ids" and pid not in p.opened_paragraph_ids:
                fail(F.CHECK_INCOMPLETE, "adopted source missing from opened list")
            if name == "inherited_source_ids" and pid not in ledger.upstream_ids:
                fail(F.CHECK_INCOMPLETE, "source falsely declared inherited")
    derived = VerificationBasis.SELF_CHECKED if ledger.opened_ids else VerificationBasis.UPSTREAM_ONLY
    if p.verification_basis is not VerificationBasis.SELF_CHECKED:
        fail(F.BASIS_NOT_SELF_CHECKED, "self_checked declaration required")
    if p.verification_basis is not derived:
        fail(F.CHECK_INCOMPLETE, "basis differs from opening ledger")
    return VerificationOutcome(
        validator_version="validator_v3", required=verification_required,
        status=VerificationStatus.VERIFIED if ledger.opened_ids else VerificationStatus.NOT_CHECKED,
        target_clause_status=tuple(output.target_clause_status.items()),
        opened_paragraph_ids=p.opened_paragraph_ids, inherited_source_ids=p.inherited_source_ids,
        declared_basis=p.verification_basis, derived_basis=derived,
        node=node, invocation=invocation, rule_id=output.rule_id, checked=ledger.used_source_tools(),
        validation_pass=not failures, audit_failures=tuple(failures),
        failures=tuple(failures) if verification_required else (), diagnostics=tuple(diagnostics),
        target_search_audit=target_audit, citation_audit=tuple(audits),
        actual_opened_paragraph_ids=ledger.opened_ids,
        reason_paragraph_ids=_paragraph_references(output.reason_summary, document),
        uncertainty_paragraph_ids=_paragraph_references(" ".join(output.uncertainties), document),
        note="procedural provenance only; narrative references are diagnostic, not adoption",
    )
