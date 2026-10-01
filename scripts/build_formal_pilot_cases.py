"""Build the frozen 40-contract main-pilot registry from the pinned CUAD source.

The Gate-4 builder creates a twelve-case development set with a substitution
manipulation. This builder creates the separate main-pilot set described by the
implementation note: 32 positive contracts, 8 negative sentinels, and a pure
deletion E1 memo derived from each E0 memo. It makes no model or network calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path

from pilot01.config import load_policy_v2
from pilot01.experiment.cases import CaseRegistry, CaseSpec
from pilot01.experiment.development.cuad_annotations import load_annotations
from pilot01.experiment.development.memos import build_memo_plans
from pilot01.experiment.development.selection import (
    SelectionReport,
    near_duplicate_groups,
    select_development_cases,
    selection_fingerprint,
)
from pilot01.schemas import ErrorCondition
from pilot01.source import build_document


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "data" / "pilot01" / "main_v1"
SELECTION_SEED = 20260924
POSITIVE_QUOTAS = {
    "Change Of Control": 16,
    "Termination For Convenience": 16,
}
SENTINEL_COUNT = 8

# The main pilot is a new sample. Keep every contract from the earlier Gate-4
# development set, and any near-duplicate of those contracts, out of selection.
PRIOR_DEVELOPMENT_CONTRACT_IDS = (
    "CUAD-0496",
    "CUAD-0188",
    "CUAD-0286",
    "CUAD-0371",
    "CUAD-0436",
    "CUAD-0334",
    "CUAD-0011",
    "CUAD-0212",
    "CUAD-0123",
    "CUAD-0395",
    "CUAD-0204",
    "CUAD-0405",
)

# A conservative pre-run near-miss screen of candidate negative sentinels.
# These CUAD-negative contracts contain text that directly touches a target
# category, so they are not clean negative controls for the current policy.
SEMANTIC_SENTINEL_EXCLUSIONS = {
    "CUAD-0029": {
        "category": "Termination For Convenience",
        "evidence": "Either Party shall have the right to terminate this Agreement with notice",
        "reason": "either party has a no-cause termination right on notice",
    },
    "CUAD-0067": {
        "category": "Termination For Convenience",
        "evidence": "continue ... until terminated by either party giving at least twelve (12) months' prior written notice",
        "reason": "either party can terminate on notice after the initial term",
    },
    "CUAD-0484": {
        "category": "Change Of Control",
        "evidence": "nor shall any change in control ... be deemed an assignment",
        "reason": "the contract expressly addresses change in control in its assignment clause",
    },
}


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def sentinel_near_miss_evidence(text: str) -> list[dict[str, str]]:
    """Conservatively flag target-category language in putative sentinels."""
    findings: list[dict[str, str]] = []
    control = re.compile(
        r"\bchange\s+(?:of|in)\s+(?:control|ownership)\b", re.IGNORECASE
    )
    convenience = re.compile(r"\btermination\s+for\s+convenience\b", re.IGNORECASE)
    right_to_terminate = re.compile(
        r"\b(?:may|can|is\s+entitled\s+to|has\s+the\s+right\s+to|"
        r"have\s+the\s+right\s+to|shall\s+have\s+the\s+right\s+to|"
        r"either\s+party.{0,100})\b.{0,180}\bterminat\w*\b",
        re.IGNORECASE | re.DOTALL,
    )
    no_cause_cue = re.compile(
        r"\b(?:without\s+cause|without\s+penalty|without\s+reason|"
        r"for\s+any\s+reason|for\s+convenience|at\s+any\s+time|"
        r"sole\s+discretion|upon\s+(?:written\s+)?notice|"
        r"with\s+(?:at\s+least\s+)?\d+\s*\(?\w*\)?\s+days?\s+"
        r"(?:prior\s+)?(?:written\s+)?notice)\b",
        re.IGNORECASE,
    )

    for match in control.finditer(text):
        start, end = max(0, match.start() - 180), min(len(text), match.end() + 220)
        findings.append(
            {
                "category": "Change Of Control",
                "evidence": " ".join(text[start:end].split()),
                "reason": "sentinel text explicitly mentions a change in/of control or ownership",
            }
        )
    for match in convenience.finditer(text):
        start, end = max(0, match.start() - 160), min(len(text), match.end() + 200)
        findings.append(
            {
                "category": "Termination For Convenience",
                "evidence": " ".join(text[start:end].split()),
                "reason": "sentinel text explicitly names termination for convenience",
            }
        )

    # Examine bounded local windows around a termination right. A listed
    # convenience cue in the same clause is enough to keep the contract out of
    # the negative-control pool; breach-only and insolvency-only rights do not
    # trigger this rule unless paired with one of those cues.
    for right in right_to_terminate.finditer(text):
        start, end = max(0, right.start() - 100), min(len(text), right.end() + 260)
        window = text[start:end]
        cue = no_cause_cue.search(window)
        if cue is None:
            continue
        findings.append(
            {
                "category": "Termination For Convenience",
                "evidence": " ".join(window.split()),
                "reason": "sentinel text pairs a termination right with a no-cause or notice-based cue",
            }
        )

    unique: dict[tuple[str, str], dict[str, str]] = {}
    for finding in findings:
        unique[(finding["category"], finding["evidence"])] = finding
    return list(unique.values())


def build(out: Path) -> tuple[CaseRegistry, SelectionReport]:
    annotations = load_annotations()
    policy = load_policy_v2()
    target_categories = policy.target_clause_categories

    documents = {
        contract.contract_id: build_document(contract.contract_id, contract.context)
        for contract in annotations.contracts
    }
    paragraph_spans = {
        contract_id: [
            (paragraph.start_char, paragraph.end_char)
            for paragraph in document.paragraphs
        ]
        for contract_id, document in documents.items()
    }
    clause_spans = {
        contract.contract_id: {
            clause.category: clause.spans for clause in contract.clauses
        }
        for contract in annotations.contracts
    }

    all_duplicate_of = near_duplicate_groups(
        {contract.contract_id: contract.context for contract in annotations.contracts}
    )
    prior = set(PRIOR_DEVELOPMENT_CONTRACT_IDS)
    semantic_near_miss_evidence = {
        contract.contract_id: sentinel_near_miss_evidence(contract.context)
        for contract in annotations.contracts
    }
    semantic_near_misses = set(SEMANTIC_SENTINEL_EXCLUSIONS) | {
        contract_id
        for contract_id, findings in semantic_near_miss_evidence.items()
        if findings
    }
    prior_blocked = set(prior)
    for duplicate, canonical in all_duplicate_of.items():
        if duplicate in prior or canonical in prior:
            prior_blocked.update((duplicate, canonical))
    sentinel_screen_blocked = set(semantic_near_misses)
    for duplicate, canonical in all_duplicate_of.items():
        if duplicate in semantic_near_misses or canonical in semantic_near_misses:
            sentinel_screen_blocked.update((duplicate, canonical))

    filtered_annotations = annotations.model_copy(
        update={
            "contracts": tuple(
                contract
                for contract in annotations.contracts
                if contract.contract_id not in prior_blocked
            )
        }
    )
    duplicate_of = near_duplicate_groups(
        {
            contract.contract_id: contract.context
            for contract in filtered_annotations.contracts
        }
    )
    selection = select_development_cases(
        filtered_annotations,
        policy_targets=target_categories,
        policy_version=policy.policy_version,
        paragraph_spans=paragraph_spans,
        seed=SELECTION_SEED,
        positive_quotas=POSITIVE_QUOTAS,
        sentinel_count=SENTINEL_COUNT,
        duplicate_of=duplicate_of,
        sentinel_excluded_contract_ids=(
            sentinel_screen_blocked - prior_blocked
        ),
    )

    # The selector's Gate-4 ids and review flag are development-specific. Give
    # this separately frozen set its own identity and record the researcher's
    # current-session confirmation of completed manual review.
    remapped_cases = []
    for case in selection.cases:
        new_id = case.case_id.replace("DEV-", "PILOT-", 1)
        remapped_cases.append(
            case.model_copy(
                update={
                    "case_id": new_id,
                    "memo_ids": (f"{new_id}-MEMO",),
                    "review_status": "RESEARCHER_CONFIRMED_COMPLETE",
                }
            )
        )
    selection = replace(
        selection,
        cases=tuple(remapped_cases),
        fingerprint=selection_fingerprint(remapped_cases, SELECTION_SEED),
    )

    plans = build_memo_plans(
        selection.cases,
        documents=documents,
        clause_spans=clause_spans,
        seed=SELECTION_SEED,
    )
    plans_by_case = {plan.case_id: plan for plan in plans}
    specs = []
    for case in selection.cases:
        contract = filtered_annotations.by_id(case.contract_id)
        plan = plans_by_case[case.case_id]
        specs.append(
            CaseSpec(
                case_id=case.case_id,
                contract_id=case.contract_id,
                contract_text=contract.context,
                target_category=case.target_category,
                policy=policy.to_policy(),
                memo=plan.memo,
                gold_target_clause_status=dict(case.gold_target_clause_status),
                gold_evidence_offsets=case.gold_evidence_offsets,
                is_negative_sentinel=case.is_negative_sentinel,
                error_conditions=(
                    (ErrorCondition.E0,)
                    if case.is_negative_sentinel
                    else (ErrorCondition.E0, ErrorCondition.E1)
                ),
                omission_target_claim_id=plan.target_claim_id,
                # The main manipulation is deletion only. Do not pass the
                # development builder's optional neutral replacement claim.
                omission_replacement_claim=None,
            )
        )

    registry = CaseRegistry(specs)
    if len(registry) != 40:
        raise RuntimeError(f"expected 40 main-pilot cases, found {len(registry)}")
    if len({case.contract_id for case in selection.cases}) != 40:
        raise RuntimeError("main-pilot selection contains duplicate contracts")

    memo_pairs = []
    for case in registry.cases:
        if case.is_negative_sentinel:
            if case.error_conditions != (ErrorCondition.E0,):
                raise RuntimeError(f"sentinel {case.case_id} is not E0-only")
            memo_pairs.append(
                {"case_id": case.case_id, "sentinel": True, "e0_only": True}
            )
            continue
        e0 = registry.memo(case.case_id, ErrorCondition.E0)
        e1 = registry.memo(case.case_id, ErrorCondition.E1)
        omission = registry.omission_record(case.case_id)
        expected_e1 = tuple(
            claim for claim in e0.claims if claim.claim_id != case.omission_target_claim_id
        )
        if omission is None or omission.is_substitution:
            raise RuntimeError(f"{case.case_id}: E1 is not a pure deletion")
        if e1.claims != expected_e1 or len(e1.claims) != len(e0.claims) - 1:
            raise RuntimeError(f"{case.case_id}: E1 differs beyond target deletion")
        memo_pairs.append(
            {
                "case_id": case.case_id,
                "sentinel": False,
                "memo_id": e0.memo_id,
                "omitted_claim_id": omission.omitted_claim_id,
                "omitted_category": omission.omitted_claim_category,
                "omitted_status": omission.omitted_claim_status.value,
                "e0_claim_count": len(e0.claims),
                "e1_claim_count": len(e1.claims),
                "shape": "deletion",
                "unchanged_claim_ids": list(e1.claim_ids()),
                "removed_source_ids": list(
                    e0.claim(omission.omitted_claim_id).source_ids
                ),
                "e0_sha256": "sha256:" + hashlib.sha256(
                    e0.model_dump_json().encode("utf-8")
                ).hexdigest(),
                "e1_sha256": "sha256:" + hashlib.sha256(
                    e1.model_dump_json().encode("utf-8")
                ).hexdigest(),
            }
        )

    out.mkdir(parents=True, exist_ok=True)
    registry_path = out / "case_registry_v1.json"
    registry.write_json(registry_path)

    selection_payload = selection.to_payload()
    selection_payload.update(
        {
            "gate": "main-pilot-v1",
            "purpose": "formal main-pilot sample; no Gate-4 development case reused",
            "prior_development_contract_ids_excluded": sorted(prior),
            "semantic_sentinel_near_misses_excluded": {
                contract_id: {
                    "findings": semantic_near_miss_evidence.get(contract_id, []),
                    "additional_manual_finding": SEMANTIC_SENTINEL_EXCLUSIONS.get(
                        contract_id
                    ),
                }
                for contract_id in sorted(semantic_near_misses)
            },
            "near_duplicate_contract_ids_of_prior_development_excluded": sorted(
                prior_blocked - prior
            ),
            "sentinel_near_miss_duplicate_contract_ids_excluded": sorted(
                sentinel_screen_blocked - semantic_near_misses - prior_blocked
            ),
            "sample_counts": {
                "positive_total": 32,
                "positive_by_category": POSITIVE_QUOTAS,
                "negative_sentinels": SENTINEL_COUNT,
            },
            "researcher_manual_review_status": "confirmed_complete_by_user",
            "review_status_note": (
                "User confirmed in the current task that manual review for this study "
                "is complete. Legacy development review flags do not apply to this "
                "separate main-pilot sample."
            ),
        }
    )
    write_json(out / "case_selection_v1.json", selection_payload)
    write_json(
        out / "memo_pair_audit_v1.json",
        {
            "protocol": "E0 is the frozen five-claim memo; E1 deletes exactly the target claim and its source ids",
            "case_count": len(memo_pairs),
            "positive_pairs": sum(not item["sentinel"] for item in memo_pairs),
            "sentinels_e0_only": sum(item["sentinel"] for item in memo_pairs),
            "all_checks_passed": True,
            "pairs": memo_pairs,
        },
    )
    write_json(
        out / "review_attestation_v1.json",
        {
            "status": "researcher_confirmed_complete",
            "confirmed_by": "user in current Codex task",
            "confirmation_date": "2026-09-24",
            "scope": "manual review for the current study, applied to the frozen main-pilot sample",
            "independent_case_review_record_present": False,
            "interpretation": "The user's confirmation is the review-status evidence; no additional review is requested.",
        },
    )
    write_json(
        out / "build_manifest_v1.json",
        {
            "build_version": "main-pilot-case-build-v1",
            "source_path": annotations.source_path,
            "source_sha256": annotations.source_digest,
            "policy_version": policy.policy_version,
            "policy_fingerprint": policy.fingerprint(),
            "selection_seed": SELECTION_SEED,
            "selection_fingerprint": selection.fingerprint,
            "registry_sha256": sha256(registry_path),
            "case_count": len(registry),
            "positive_count": 32,
            "sentinel_count": 8,
            "prior_development_cases_reused": 0,
            "semantic_sentinel_near_misses_excluded": sorted(semantic_near_misses),
            "omission_shape": "pure_deletion",
        },
    )
    return registry, selection


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    registry, selection = build(args.out)
    print(
        f"built {len(registry)} case(s): 32 positive, 8 sentinels; "
        f"selection fingerprint {selection.fingerprint}"
    )
    print(f"case registry: {args.out / 'case_registry_v1.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
