"""Offline export and validation of a frozen real CUAD memo set. No API calls."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from pilot01.experiment.cases import CaseRegistry
from pilot01.experiment.development.cuad_annotations import load_annotations
from pilot01.experiment.development.neutral_materials import verify_ready_registry, neutral_id_mapping, NeutralCandidate, check_candidate
from pilot01.schemas import ErrorCondition


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--registry", required=True)
    p.add_argument("--source-registry", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    path, source_path, out = Path(args.registry), Path(args.source_registry), Path(args.out)
    if (out / "dataset_validation.json").exists():
        p.error("choose a new export directory; validation snapshots are not overwritten")
    clean = verify_ready_registry(path)
    source = CaseRegistry.load_json(source_path)
    mapping = neutral_id_mapping(source)
    old_by_new = {mapping[c.case_id]: c for c in source.cases}
    if set(old_by_new) != set(clean.case_ids):
        raise ValueError("source cases were removed or invented")
    annotations = load_annotations(categories=source.cases[0].policy.target_clause_categories)
    candidates = [NeutralCandidate.model_validate_json(line) for line in
        (path.parent / "neutral_claim_review.jsonl").read_text(encoding="utf-8").split("\n") if line.strip()]
    approved = {c.candidate_id: c for c in candidates if c.approved}
    qc = json.loads((path.parent / "memo_qc.json").read_text(encoding="utf-8"))
    cohort_path = path.parent / "researcher_cohorts.json"
    cohorts = {row["case_id"]:row["cohort"] for row in json.loads(cohort_path.read_text(encoding="utf-8"))} if cohort_path.exists() else {}
    selected = {row["case_id"]: row["candidate_ids"] for row in qc["qc"]}
    rows, review_md, changes = [], [], []
    for case in sorted(clean.cases, key=lambda c: c.case_id):
        old = old_by_new[case.case_id]
        original = annotations.by_id(old.contract_id)
        expected = {category: "present" if original.status(category) else "absent" for category in old.policy.target_clause_categories}
        actual = {k: v.value for k, v in case.gold_target_clause_status.items()}
        if (actual != expected or case.contract_text_hash != original.text_hash
                or case.contract_text != old.contract_text or case.gold_evidence_offsets != old.gold_evidence_offsets
                or case.is_negative_sentinel != old.is_negative_sentinel):
            raise ValueError(f"source annotations/text changed: {case.case_id}")
        for category in old.policy.target_clause_categories:
            old_claim = next(c for c in old.memo.claims if c.category == category)
            new_claim = next(c for c in case.memo.claims if c.category == category)
            if old_claim.model_dump(exclude={"claim_id"}) != new_claim.model_dump(exclude={"claim_id"}):
                raise ValueError("target claim changed")
        for cid in selected[case.case_id]:
            errors = check_candidate(approved[cid], case=old, document=source.document(old.case_id), neutral_case_id=case.case_id)
            if errors:
                raise ValueError(f"selected candidate invalid: {cid}: {errors}")
        review_md.append(f"## {case.case_id}\n\n原合同：{case.contract_id}（仅供研究侧核对）；分组：{cohorts.get(case.case_id, '未分组')}；审核类型：AI，尚未人工复核。\n")
        memos = []
        for error in case.error_conditions:
            memo = clean.memo(case.case_id, error)
            if len(memo.claims) != 5:
                raise ValueError("memo must contain five claims")
            memos.append(memo)
            rows.append({"case_id": case.case_id, "error_condition": error.value, "memo": memo.model_dump(mode="json")})
            review_md.append(f"### {error.value}\n\n| 槽位 | 类别 | 状态 | 摘要 | 引用段落 |\n|---|---|---|---|---|\n")
            for claim in memo.claims:
                summary = claim.summary.replace("|", "\\|").replace("\n", " ")
                review_md.append(f"| {claim.claim_id} | {claim.category} | {claim.status.value} | {summary} | {', '.join(claim.source_ids)} |\n")
            review_md.append("\n")
        if not case.is_negative_sentinel:
            e0, e1 = memos
            different = [i for i, (a, b) in enumerate(zip(e0.claims, e1.claims)) if a != b]
            if e0.claim_ids() != e1.claim_ids() or len(different) != 1:
                raise ValueError("memo pair must differ in one unchanged ID slot")
            i = different[0]
            if e0.claims[i].category != case.target_category or e1.claims[i].category in case.policy.target_clause_categories:
                raise ValueError("wrong omission replacement")
            changes.append({"case_id": case.case_id, "changed_slot": e0.claims[i].claim_id,
                            "before": e0.claims[i].summary, "after": e1.claims[i].summary})
    out.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")
    (out / "memos_v2.jsonl").write_bytes(body)
    (out / "memo_pairs_review.md").write_text("# 新 memo 全量核对表\n\n研究侧材料，勿整份提供给实验 agent。本版经授权由 AI 审核后冻结，人工复核尚未完成。\n\n" + "".join(review_md), encoding="utf-8")
    (out / "e1_changes.json").write_text(json.dumps(changes, ensure_ascii=False, indent=2), encoding="utf-8")
    usage = Counter()
    call_counts = Counter()
    failures = []
    for file in sorted(path.parent.glob("*.jsonl")):
        if not (file.name.startswith("generation_") or file.name.startswith("review_")):
            continue
        for line in file.read_text(encoding="utf-8").split("\n"):
            if not line.strip():
                continue
            record = json.loads(line)
            if "call_index" not in record:
                continue
            call_counts["generation" if file.name.startswith("generation_") else "ai_review"] += 1
            for key, value in (record.get("usage") or {}).items():
                if value is not None:
                    usage[key] += value
            if record.get("error") or record.get("finish_reason") == "length":
                failures.append({"file": file.name, "finish_reason": record.get("finish_reason"), "error": record.get("error"), "usage": record.get("usage")})
    manifest = json.loads((path.parent / "material_manifest.json").read_text(encoding="utf-8"))
    result = {"status": "passed", "contracts": len(clean.cases),
        "positive_contracts": sum(not c.is_negative_sentinel for c in clean.cases),
        "negative_sentinels": sum(c.is_negative_sentinel for c in clean.cases), "memos": len(rows),
        "e1_pairs": len(changes), "selected_neutral_claims": sum(len(v) for v in selected.values()),
        "original_cuad_labels_and_contracts_match": True, "all_selected_source_checks_passed": True,
        "all_memos_five_claims": True, "e1_single_slot_change": True,
        "review_kinds": manifest["review_kinds"], "reviewers": manifest["reviewers"],
        "human_review_completed": manifest["human_review_completed"],
        "length_review_warnings": sum(row["length_review_warning"] for row in qc["qc"]),
        "material_preparation_api_calls": dict(call_counts), "reported_token_usage": dict(usage),
        "preserved_failed_calls": failures, "formal_experiment_runs_started": 0,
        "registry_sha256": manifest["registry_sha256"],
        "memos_sha256": "sha256:" + hashlib.sha256(body).hexdigest()}
    if cohorts:
        if set(cohorts)!=set(clean.case_ids):
            raise ValueError("cohort mapping does not match the registry")
        result["cohorts"] = {}
        for cohort in sorted(set(cohorts.values())):
            members = {cid for cid,group in cohorts.items() if group==cohort}
            group_rows = [row for row in rows if row["case_id"] in members]
            group_body = "".join(json.dumps(row,ensure_ascii=False)+"\n" for row in group_rows).encode("utf-8")
            (out/f"memos_{cohort}.jsonl").write_bytes(group_body)
            result["cohorts"][cohort] = {"contracts":len(members),"memos":len(group_rows),
                "e1_pairs":sum(row["error_condition"]=="E1" for row in group_rows),
                "memos_sha256":"sha256:"+hashlib.sha256(group_body).hexdigest()}
    (out / "dataset_validation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
