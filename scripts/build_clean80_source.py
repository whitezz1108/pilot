"""Freeze old40 + a seeded, previously unused CUAD40. Offline only."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random

from pilot01.experiment.cases import CaseRegistry, CaseSpec
from pilot01.experiment.development.cuad_annotations import load_annotations
from pilot01.experiment.development.memos import SUMMARY_PRESENT, SUMMARY_ABSENT
from pilot01.experiment.development.neutral_materials import neutral_id_mapping, export_candidate_tasks
from pilot01.experiment.development.selection import near_duplicate_groups, NEAR_DUPLICATE_THRESHOLD
from pilot01.schemas import AnalystMemo, MemoClaim, ClauseStatus, ErrorCondition
from pilot01.source import build_document

PRIOR_GATE4 = {"CUAD-0496", "CUAD-0188", "CUAD-0286", "CUAD-0371", "CUAD-0436", "CUAD-0334",
               "CUAD-0011", "CUAD-0212", "CUAD-0123", "CUAD-0395", "CUAD-0204", "CUAD-0405"}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--old-registry", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--seed", type=int, default=20261017)
    args = p.parse_args()
    out = args.out
    if (out / "source_registry_80.json").exists():
        p.error("selection already frozen; use another directory")
    old = CaseRegistry.load_json(args.old_registry)
    if len(old.cases) != 40 or len({c.contract_id for c in old.cases}) != 40:
        raise ValueError("expected forty distinct original contracts")
    categories = old.cases[0].policy.target_clause_categories
    annotations = load_annotations(categories=categories)
    old_ids = {c.contract_id for c in old.cases}
    seen = old_ids | PRIOR_GATE4
    duplicate_of = near_duplicate_groups({c.contract_id: c.context for c in annotations.contracts})
    def canonical(cid):
        while cid in duplicate_of:
            cid = duplicate_of[cid]
        return cid
    seen_clusters = {canonical(cid) for cid in seen}
    pools = {"coc_only": [], "tfc_only": [], "both_absent": []}
    excluded = []
    for c in annotations.contracts:
        flags = []
        present = [cat for cat in categories if c.status(cat)]
        if c.contract_id in seen:
            flags.append("previously_used_contract")
        if canonical(c.contract_id) in seen_clusters and c.contract_id not in seen:
            flags.append("near_duplicate_of_previous_contract")
        if len(c.context) < 4000:
            flags.append("context_under_4000_chars")
        if len(present) == 2:
            flags.append("both_targets_present_outside_fixed_design")
        if len(present) == 1:
            spans = c.clause(present[0]).spans
            if len(spans) > 2 or any(end-start < 30 for start, end in spans):
                flags.append("positive_target_spans_outside_original_mechanical_range")
        if flags:
            excluded.append({"contract_id": c.contract_id, "reasons": flags})
            continue
        group = "both_absent" if not present else "coc_only" if present[0] == "Change Of Control" else "tfc_only"
        pools[group].append(c)
    selected, clusters = [], set(seen_clusters)
    for group, count in (("coc_only", 16), ("tfc_only", 16), ("both_absent", 8)):
        pool = sorted(pools[group], key=lambda c: c.contract_id)
        random.Random(f"clean80:{args.seed}:{group}").shuffle(pool)
        chosen = []
        for c in pool:
            root = canonical(c.contract_id)
            if root in clusters:
                continue
            clusters.add(root)
            chosen.append(c)
            if len(chosen) == count:
                break
        if len(chosen) != count:
            raise ValueError(f"insufficient eligible distinct contracts: {group}: {len(chosen)}/{count}")
        selected.extend((group, c) for c in chosen)
    cases = list(old.cases)
    for index, (group, c) in enumerate(selected):
        case_id = f"RESEARCH-{c.contract_id}"
        target = (categories[index % 2] if group == "both_absent" else
                  "Change Of Control" if group == "coc_only" else "Termination For Convenience")
        doc = build_document(c.contract_id, c.context, **old.chunking)
        statuses = {cat: ClauseStatus.PRESENT if c.status(cat) else ClauseStatus.ABSENT for cat in categories}
        claims = []
        for i, cat in enumerate(categories, 1):
            spans = c.clause(cat).spans if c.status(cat) else ()
            pids = tuple(p.paragraph_id for p in doc.paragraphs if any(start < p.end_char and p.start_char < end for start,end in spans))
            claims.append(MemoClaim(claim_id=f"TARGET-{i}", category=cat, status=statuses[cat],
                summary=(SUMMARY_PRESENT if c.status(cat) else SUMMARY_ABSENT).format(category=cat), source_ids=pids))
        memo = AnalystMemo(case_id=case_id, memo_id=f"SOURCE-{c.contract_id}", claims=tuple(claims))
        negative = group == "both_absent"
        offsets = () if negative else tuple(x for span in c.clause(target).spans for x in span)
        cases.append(CaseSpec(case_id=case_id, contract_id=c.contract_id, contract_text=c.context, target_category=target,
            policy=old.cases[0].policy, memo=memo, gold_target_clause_status=statuses, gold_evidence_offsets=offsets,
            is_negative_sentinel=negative, error_conditions=(ErrorCondition.E0,) if negative else (ErrorCondition.E0, ErrorCondition.E1),
            omission_target_claim_id=None if negative else next(x.claim_id for x in claims if x.category==target)))
    registry = CaseRegistry(cases, **old.chunking)
    out.mkdir(parents=True, exist_ok=True)
    source = registry.write_json(out / "source_registry_80.json")
    mapping = neutral_id_mapping(registry, args.seed)
    cohorts = [{"case_id": mapping[c.case_id], "contract_id": c.contract_id,
                "cohort": "old40_development" if c.contract_id in old_ids else "new40_main",
                "is_negative_sentinel": c.is_negative_sentinel, "target_category": c.target_category,
                "contract_text_hash": c.contract_text_hash} for c in registry.cases]
    selection = {"seed": args.seed, "old_contracts": sorted(old_ids), "new_contracts": [c.contract_id for _,c in selected],
        "new_pool_sizes": {k:len(v) for k,v in pools.items()}, "new_quotas": {"coc_only":16,"tfc_only":16,"both_absent":8},
        "excluded":excluded, "near_duplicate_threshold":NEAR_DUPLICATE_THRESHOLD, "duplicate_of":duplicate_of,
        "source_registry_sha256":"sha256:"+hashlib.sha256(source.read_bytes()).hexdigest(),
        "prior_gate4_excluded":sorted(PRIOR_GATE4), "semantic_relabelling":False,
        "selection_uses_model_outcomes":False, "expected_contracts":80,"expected_memos":144}
    (out/"selection_manifest.json").write_text(json.dumps(selection,ensure_ascii=False,indent=2),encoding="utf-8")
    (out/"researcher_cohorts.json").write_text(json.dumps(cohorts,ensure_ascii=False,indent=2),encoding="utf-8")
    export_candidate_tasks(registry, out, args.seed)
    print(json.dumps({"contracts":len(registry.cases),"new_pool_sizes":selection["new_pool_sizes"],"new_quotas":selection["new_quotas"],"expected_memos":144}))


if __name__ == "__main__":
    main()
