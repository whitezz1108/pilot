"""The human review pack: twelve cases, laid out so a person can actually check them.

Gate 4 builds real cases from a real corpus, and the honest position on them is
that **no human has reviewed any of them**. This module exists to make that
review possible and cheap, and to make its absence visible: every case ships as
``PENDING_HUMAN_REVIEW``, the pack says so at the top, and no code here can set
any other state. A review state that a program can promote is not a review.

**What a reviewer needs is not what the registry stores.** The registry holds
the case's identity and its labels; a reviewer needs the *evidence* -- the
annotated clause in its surrounding text, the offsets, the decision the policy
requires, and the memo the agents will actually read. That is what the pack
prints, case by case, in a fixed order, with the machine-readable version beside
it so the two can be compared.

**The pack is deliberately explicit about the manipulation.** For a positive
case it shows the E0 memo, the E1 memo and the diff between them. A reviewer's
job on a matched-omission pair is to confirm that the only thing that changed is
the one registered claim, and that requires seeing both arms side by side rather
than being told the diff is clean.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from ...schemas import ErrorCondition
from ..cases import CaseRegistry
from .memos import MEMO_ORIGIN
from .registry import DevelopmentCaseRecord, DevelopmentCaseSet

__all__ = [
    "REVIEW_STATES",
    "render_review_pack",
    "write_review_pack",
    "review_pack_payload",
]

REVIEW_STATES = ("PENDING_HUMAN_REVIEW", "REVIEWED", "REJECTED")
"""The only states a case may be in.

``REVIEWED`` and ``REJECTED`` are listed so the vocabulary is complete and so a
reader can see the pack is not silently omitting them, but nothing in this
codebase sets either. Only a human may, by editing the record.
"""

_CONTEXT_WINDOW = 320
"""Characters of contract text to show either side of a gold span.

Enough to see what the clause sits among -- a termination provision next to a
renewal provision reads differently from one next to an indemnity -- without
turning the pack into a copy of the contract.
"""


def _context(text: str, start: int, end: int, window: int = _CONTEXT_WINDOW) -> str:
    """The span with its surroundings, marked, and with the joins made visible.

    Ellipses at a truncated end, and newlines collapsed, so a reviewer reading
    the pack on a terminal cannot mistake the window's edge for the clause's.
    """
    left = max(0, start - window)
    right = min(len(text), end + window)
    body = text[left:right].replace("\r\n", "\n")
    marked = (
        body[: start - left] + "⟦" + body[start - left : end - left] + "⟧" + body[end - left :]
    )
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(text) else ""
    return prefix + marked + suffix


def _wrap(text: str, width: int = 96, indent: str = "    ") -> str:
    """Hard-wrap for a terminal, without reflowing the marked span out of view."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        while len(paragraph) > width:
            lines.append(indent + paragraph[:width])
            paragraph = paragraph[width:]
        lines.append(indent + paragraph)
    return "\n".join(lines)


def _record_block(record: DevelopmentCaseRecord) -> str:
    lines: list[str] = []
    add = lines.append
    add("=" * 100)
    add(f"CASE ID          {record.case_id}")
    add(f"CONTRACT TITLE   {record.cuad_title}")
    add(f"CONTRACT ID      {record.contract_id}")
    add(f"TARGET CATEGORY  {record.target_category}")
    add(
        "CASE CLASS       "
        + ("NEGATIVE SENTINEL (both target clauses absent)" if record.is_negative_sentinel
           else f"POSITIVE OMISSION (exactly one target present: {record.target_category})")
    )
    add(f"GOLD STATUS      {record.gold_clause_status.value}")
    add(f"GOLD ACTION      {record.gold_action.value}   (POLICY-01, version {record.policy_version})")
    add(f"ERROR ARMS       {', '.join(record.error_conditions)}")
    add(f"REVIEW STATUS    {record.review_status}")
    add(f"MEMO ORIGIN      {record.memo_origin}")
    add(f"TEXT HASH        {record.contract_text_hash}")
    add(f"SOURCE SNAPSHOT  {record.source_snapshot_fingerprint}")
    add(f"SELECTION        {record.selection_reason}")
    add("")
    add("GOLD EVIDENCE")
    if record.gold_evidence_offsets:
        offsets = record.gold_evidence_offsets
        for index in range(0, len(offsets), 2):
            start, end = offsets[index], offsets[index + 1]
            add(f"  span {index // 2}: [{start}, {end})  ({end - start} chars)")
            add(f"  text: {record.gold_evidence_texts[index // 2]!r}")
        add(f"  source paragraph ids: {', '.join(record.paragraph_ids)}")
        add("")
        add("  surrounding context (gold span marked ⟦ ⟧):")
        # The context needs the contract text, which the record does not carry;
        # it is attached by the caller through the registry. See render_review_pack.
    else:
        add("  none -- this case has no gold evidence span, by design (sentinel)")
    add("")
    add("DATA-QUALITY CHECKS (each applied to every contract in the pool)")
    for note in record.exclusion_notes:
        add(f"  - {note}")
    add("")
    if record.memo_pair_diff is not None:
        diff = record.memo_pair_diff
        add("MEMO PAIR")
        add(f"  memo id (shared by both arms): {diff.memo_id}")
        add(f"  shape: {diff.shape}")
        add(f"  claims: E0={diff.e0_claim_count}  E1={diff.e1_claim_count}")
        add(f"  changed positions: {list(diff.changed_indices)}")
        add(f"  length: E0={diff.e0_chars} chars/{diff.e0_words} words  "
            f"E1={diff.e1_chars} chars/{diff.e1_words} words  "
            f"(Δ {diff.char_delta} chars, {diff.word_delta} words)")
        add(f"  omitted claim: {record.omission_target_claim_id}")
        for entry in diff.removed:
            add(f"  E0 only: {entry}")
        for entry in diff.added:
            add(f"  E1 only: {entry}")
        add(f"  unchanged claims: {len(diff.unchanged_claim_ids)}")
    else:
        add("MEMO PAIR")
        add("  none -- this sentinel runs E0 only; no E1 arm was fabricated")
    add("")
    add("MEMO CANDIDATE (E0, as the Manager will see it)")
    for provenance in record.claim_provenance:
        add(f"  {provenance.claim_id}  [{provenance.category}]  status={provenance.status.value}"
            f"  origin={provenance.origin}")
        if provenance.gold_offsets:
            offsets = provenance.gold_offsets
            spans = ", ".join(
                f"[{offsets[i]}, {offsets[i + 1]})" for i in range(0, len(offsets), 2)
            )
            add(f"      cites {spans} ({provenance.evidence_chars} chars)")
        else:
            add("      cites nothing (an assertion of absence has no passage)")
    add("")
    return "\n".join(lines)


def render_review_pack(case_set: DevelopmentCaseSet) -> str:
    """The full pack, as one readable document.

    Case order is registry order, which is the selection's order: the four
    Change-of-Control positives, then the four Termination-for-Convenience
    positives, then the four sentinels. A reviewer working through it sees the
    design's shape before seeing any individual case.
    """
    registry = case_set.registry
    lines: list[str] = []
    add = lines.append

    add("=" * 100)
    add("GATE 4 -- DEVELOPMENT CASE REVIEW PACK")
    add("=" * 100)
    add("")
    add("STATUS: DEVELOPMENT. This is not the confirmatory sample.")
    add("")
    add("NO HUMAN REVIEW HAS BEEN PERFORMED ON THESE CASES.")
    add("Every case below is PENDING_HUMAN_REVIEW. That state was set by the build")
    add("and has not been changed by anything since. No legal review, by a lawyer or")
    add("by anyone else, has taken place. Nothing in this pack should be read as")
    add("legal advice or as a reviewed legal determination.")
    add("")
    add(f"MEMO ORIGIN: {MEMO_ORIGIN}")
    add("")
    add("POLICY")
    add(f"  id/version: {case_set.policy.policy_id} / {case_set.policy.policy_version}"
        f"  (status: {case_set.policy.policy_status})")
    add(f"  target categories: {', '.join(case_set.policy.target_clause_categories)}")
    for status, decision in case_set.policy.clause_status_mapping.items():
        add(f"  {status.value:>8} -> {decision.value}")
    add("")
    add("SELECTION")
    add(f"  seed: {case_set.selection.seed}")
    add(f"  fingerprint: {case_set.selection.fingerprint}")
    for key, count in sorted(case_set.selection.composition().items()):
        add(f"  {key}: {count}")
    add(f"  pool sizes: {case_set.selection.to_payload()['pool_sizes']}")
    add("  exclusions applied:")
    for reason, count in sorted(case_set.selection.exclusion_counts().items()):
        add(f"    {reason}: {count}")
    add("")

    for record in case_set.cases:
        lines.append(_record_block(record))
        if record.gold_evidence_offsets:
            text = registry.get(record.case_id).contract_text
            offsets = record.gold_evidence_offsets
            add("GOLD EVIDENCE IN CONTEXT")
            for index in range(0, len(offsets), 2):
                add(_wrap(_context(text, offsets[index], offsets[index + 1])))
                add("")
        add("E0 MEMO (verbatim, as frozen into the registry)")
        for claim in registry.memo(record.case_id, ErrorCondition.E0).claims:
            add(f"  {claim.claim_id}")
            add(f"    category: {claim.category}")
            add(f"    status:   {claim.status.value}")
            add(f"    summary:  {claim.summary}")
            add(f"    sources:  {list(claim.source_ids)}")
        add("")
        if ErrorCondition.E1 in [
            ErrorCondition(value) for value in record.error_conditions
        ]:
            add("E1 MEMO (verbatim, the omission arm)")
            for claim in registry.memo(record.case_id, ErrorCondition.E1).claims:
                add(f"  {claim.claim_id}")
                add(f"    category: {claim.category}")
                add(f"    status:   {claim.status.value}")
                add(f"    summary:  {claim.summary}")
                add(f"    sources:  {list(claim.source_ids)}")
            add("")
        add("REVIEW ACTIONS")
        add("  [ ] gold evidence is the clause the case claims it is")
        add("  [ ] gold status matches what the contract actually says")
        add("  [ ] the policy action follows from the gold status")
        if record.memo_pair_diff is not None:
            add("  [ ] the E0 memo is a faithful reading of this contract")
            add("  [ ] the E1 memo differs only in the registered claim")
        else:
            add("  [ ] both target clauses really are absent (sentinel)")
        add("  [ ] no CUAD label or gold status is visible to an agent")
        add("")
        add("  To record a review, change review_status in development_cases_v1.json")
        add("  to REVIEWED or REJECTED. Nothing in this codebase will do it for you.")
        add("")

    return "\n".join(lines) + "\n"


def review_pack_payload(case_set: DevelopmentCaseSet) -> dict:
    """The same content as the pack, as data, for a diff or a checklist tool."""
    return {
        "gate": 4,
        "document": "Gate 4 development case review pack",
        "status": "DEVELOPMENT",
        "human_review_performed": False,
        "review_states": list(REVIEW_STATES),
        "memo_origin": MEMO_ORIGIN,
        "policy": case_set.policy.model_dump(mode="json"),
        "selection_fingerprint": case_set.selection.fingerprint,
        "selection_seed": case_set.selection.seed,
        "cases": [
            {
                **record.model_dump(mode="json"),
                "review_actions": _review_actions(record),
            }
            for record in case_set.cases
        ],
    }


def _review_actions(record: DevelopmentCaseRecord) -> list[str]:
    actions = [
        "gold evidence is the clause the case claims it is",
        "gold status matches what the contract actually says",
        "the policy action follows from the gold status",
    ]
    if record.memo_pair_diff is not None:
        actions += [
            "the E0 memo is a faithful reading of this contract",
            "the E1 memo differs only in the registered claim",
        ]
    else:
        actions.append("both target clauses really are absent (sentinel)")
    actions.append("no CUAD label or gold status is visible to an agent")
    return actions


def write_review_pack(
    case_set: DevelopmentCaseSet, directory: str | Path
) -> tuple[Path, Path]:
    """Write both forms of the pack. Returns ``(markdown_path, json_path)``."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    markdown = target / "case_review_pack.md"
    markdown.write_text(render_review_pack(case_set), encoding="utf-8")
    payload = target / "case_review_pack.json"
    payload.write_text(
        json.dumps(review_pack_payload(case_set), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return markdown, payload
