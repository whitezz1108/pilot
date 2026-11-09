"""Derive uniform, permuted recovery materials without changing source labels."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from pilot01.experiment.cases import CaseRegistry, CaseSpec
from pilot01.experiment.development.neutral_materials import verify_ready_registry
from pilot01.schemas import ErrorCondition, MemoClaim


TRANSFORMATION_VERSION = "recovery-materials-v1"


def file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def normalized_claim(claim: MemoClaim, claim_id: str) -> MemoClaim:
    return claim.model_copy(update={
        "claim_id": claim_id,
        "summary": (
            f"The analyst assesses {claim.category} as {claim.status.value}. "
            "Source references are listed separately."
        ),
    })


def derive_registry(source: CaseRegistry, seed: int) -> tuple[CaseRegistry, dict]:
    cases = []
    mappings = []
    for case in source.cases:
        # The permutation uses neutral identifiers only. Gold, E0/E1, category,
        # status and source contents cannot influence the ordering.
        ordered = sorted(case.memo.claims, key=lambda claim: hashlib.sha256(
            f"{TRANSFORMATION_VERSION}:{seed}:{case.case_id}:{claim.claim_id}".encode()
        ).digest())
        ids = {claim.claim_id: f"CLAIM-{index:02d}"
               for index, claim in enumerate(ordered, 1)}
        claims = tuple(normalized_claim(claim, ids[claim.claim_id]) for claim in ordered)
        replacement = case.omission_replacement_claim
        if case.runs(ErrorCondition.E1) and replacement is None:
            raise ValueError(f"{case.case_id} needs an equal-slot replacement")
        payload = case.model_dump()
        payload.update(
            memo=case.memo.model_copy(update={"claims": claims}),
            omission_target_claim_id=ids.get(case.omission_target_claim_id),
            omission_replacement_claim=(normalized_claim(
                replacement, ids[case.omission_target_claim_id]
            ) if replacement is not None else None),
        )
        cases.append(CaseSpec.model_validate(payload))
        mappings.append({"case_id": case.case_id, "claim_id_mapping": ids})
    derived = CaseRegistry(cases, **source.chunking)
    audit = audit_materials(source, derived, mappings)
    return derived, {"transformation_version": TRANSFORMATION_VERSION,
                     "seed": seed, "cases": mappings, "audit": audit}


def audit_materials(source: CaseRegistry, derived: CaseRegistry, mappings: list[dict]) -> dict:
    if source.case_ids != derived.case_ids or source.chunking != derived.chunking:
        raise ValueError("case order or chunking changed")
    pairs = 0
    target_positions = {}
    for original, case, mapping in zip(source.cases, derived.cases, mappings):
        before = original.model_dump(exclude={"memo", "omission_target_claim_id", "omission_replacement_claim"})
        after = case.model_dump(exclude={"memo", "omission_target_claim_id", "omission_replacement_claim"})
        if before != after:
            raise ValueError(f"{case.case_id} changed contract, policy or hidden labels")
        for old in original.memo.claims:
            new = case.memo.claim(mapping["claim_id_mapping"][old.claim_id])
            if new != normalized_claim(old, new.claim_id):
                raise ValueError(f"{case.case_id} changed claim semantics or sources")
        if original.omission_replacement_claim is not None:
            expected = normalized_claim(original.omission_replacement_claim, case.omission_target_claim_id)
            if case.omission_replacement_claim != expected:
                raise ValueError(f"{case.case_id} changed replacement semantics or sources")
        if not case.runs(ErrorCondition.E1):
            continue
        pairs += 1
        e0, e1 = (derived.memo(case.case_id, error) for error in (ErrorCondition.E0, ErrorCondition.E1))
        if (e0.memo_id != e1.memo_id or e0.case_id != e1.case_id
                or e0.claim_ids() != e1.claim_ids()
                or len(e0.claims) != len(original.memo.claims)
                or sum(a != b for a, b in zip(e0.claims, e1.claims)) != 1):
            raise ValueError(f"{case.case_id} violates paired memo invariants")
        slot = e0.claim_ids().index(case.omission_target_claim_id)
        target_positions[str(slot + 1)] = target_positions.get(str(slot + 1), 0) + 1
        document = derived.document(case.case_id)
        gold_ids = {p.paragraph_id for p in document.paragraphs
                    if any(a < p.end_char and p.start_char < b for a, b in case.gold_spans())}
        remaining_ids = {pid for claim in e1.claims for pid in claim.source_ids}
        if gold_ids & remaining_ids:
            raise ValueError(f"{case.case_id} exposes omitted gold paragraphs through upstream citations")
    return {"case_count": len(derived.cases), "paired_positive_cases": pairs,
            "negative_sentinels": sum(case.is_negative_sentinel for case in derived.cases),
            "omitted_target_positions": target_positions,
            "unchanged_contracts_policies_labels": True,
            "equal_length_one_slot_pairs": True,
            "remaining_e1_source_gold_overlaps": 0,
            "summary_generation_inputs": ["claim.category", "claim.status"],
            "permutation_inputs": ["transformation_version", "seed", "case_id", "claim_id"]}


def prepare(source_path: Path, output: Path, seed: int) -> Path:
    source_path = source_path.resolve()
    output = output.resolve()
    source_manifest_path = source_path.parent / "material_manifest.json"
    source_registry_hash = file_hash(source_path)
    source_manifest_hash = file_hash(source_manifest_path)
    source = verify_ready_registry(source_path)
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    derived, audit = derive_registry(source, seed)
    if output.exists() and any(output.iterdir()):
        raise ValueError("output directory must be empty to preserve frozen materials")
    if output == source_path.parent or output in source_path.parents:
        raise ValueError("derived output cannot replace source materials")
    output.mkdir(parents=True, exist_ok=True)
    lineage = output / "lineage"
    lineage.mkdir()
    copied = {}
    for name, original in (("source_registry.json", source_path),
                           ("source_material_manifest.json", source_manifest_path)):
        target = lineage / name
        shutil.copy2(original, target)
        copied[target.relative_to(output).as_posix()] = file_hash(target)
    for relative, expected in source_manifest.get("review_file_hashes", {}).items():
        original = (source_path.parent / relative).resolve()
        target = lineage / "source_reviews" / original.relative_to(source_path.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)
        if file_hash(target) != expected:
            raise ValueError("source review changed while copying")
        copied[target.relative_to(output).as_posix()] = expected
    audit_path = output / "transformation_audit.json"
    audit_path.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    copied[audit_path.name] = file_hash(audit_path)
    registry_path = derived.write_json(output / "case_registry_v2.json")
    if file_hash(source_path) != source_registry_hash or file_hash(source_manifest_path) != source_manifest_hash:
        raise ValueError("source registry or manifest changed during preparation")
    manifest = {
        "status": "ready", "material_version": "2",
        "transformation_version": TRANSFORMATION_VERSION, "seed": seed,
        "registry_sha256": file_hash(registry_path), "review_file_hashes": copied,
        "review_kinds": source_manifest.get("review_kinds", []),
        "reviewers": source_manifest.get("reviewers", []),
        "human_review_completed": False,
        "review_scope": "Inherited reviews cover original claims. Derived wording and ordering are mechanically audited.",
        "source_registry_sha256": source_registry_hash,
        "source_manifest_sha256": source_manifest_hash,
        "source_registry_path": str(source_path),
        "label_policy": source_manifest.get("label_policy"),
        "limitations": ["No new semantic adjudication of CUAD labels.",
                        "Uniform summaries remove descriptive details from all claim types.",
                        "Policy-category absence remains observable by design.",
                        "Paragraph overlap checks cannot exclude semantic paraphrases or annotation errors."],
    }
    (output / "material_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    verify_ready_registry(registry_path)
    return registry_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=20261109)
    args = parser.parse_args()
    print(prepare(args.source, args.out, args.seed))


if __name__ == "__main__":
    main()
