"""Build only from reviewed candidates; never calls a provider."""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from pilot01.experiment.cases import CaseRegistry
from pilot01.experiment.development.neutral_materials import NeutralCandidate, build_clean_registry


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--registry", required=True)
    p.add_argument("--candidates", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--seed", type=int, default=20261017)
    args = p.parse_args()
    out = Path(args.out)
    if (out / "case_registry_v2.json").exists():
        p.error("output already contains frozen materials; use a new directory")
    registry = CaseRegistry.load_json(args.registry)
    with Path(args.candidates).open(encoding="utf-8") as f:
        candidates = [NeutralCandidate.model_validate_json(line) for line in f if line.strip()]
    clean, report = build_clean_registry(registry, candidates, seed=args.seed)
    out.mkdir(parents=True, exist_ok=True)
    (out / "memo_qc.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if clean is None:
        print(f"BLOCKED: {len(report['blocked'])} cases; see memo_qc.json")
        return 2
    generation_files = {}
    for c in candidates:
        if not c.approved:
            continue
        base = Path(args.candidates).parent.resolve()
        source = (base / (c.generation_record_file or f"generation_{c.case_id}.jsonl")).resolve()
        if source.parent != base:
            raise ValueError("generation records must be in the candidate directory")
        if not source.is_file() or "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest() != c.generation_record_sha256:
            raise ValueError(f"missing or changed generation record for {c.case_id}")
        generation_files[source.name] = source
        first_attempt = base / f"generation_{c.case_id}.jsonl"
        if first_attempt.exists():
            generation_files[first_attempt.name] = first_attempt
        if c.review_kind == "ai":
            base = Path(args.candidates).parent.resolve()
            review = (base / (c.review_record_file or f"review_{c.case_id}.jsonl")).resolve()
            if review.parent != base:
                raise ValueError("AI review records must be in the candidate directory")
            if not review.is_file() or "sha256:" + hashlib.sha256(review.read_bytes()).hexdigest() != c.review_record_sha256:
                raise ValueError(f"missing or changed AI review record for {c.case_id}")
            generation_files[review.name] = review
            first_review = base / f"review_{c.case_id}.jsonl"
            if first_review.exists():
                generation_files[first_review.name] = first_review
    # Keep rejected attempts and normalization evidence as well as adopted evidence.
    for cid in {c.case_id for c in candidates}:
        for pattern in (f"generation_{cid}*.jsonl", f"review_{cid}*.jsonl", f"normalization_{cid}.json", f"proposals_{cid}*.jsonl", f"status_{cid}.json"):
            for source in Path(args.candidates).parent.glob(pattern):
                generation_files[source.name] = source
    target = clean.write_json(out / "case_registry_v2.json")
    review_hashes = {}
    for name, source in (("neutral_claim_review.jsonl", args.candidates),):
        body = Path(source).read_bytes()
        (out / name).write_bytes(body)
        review_hashes[name] = "sha256:" + hashlib.sha256(body).hexdigest()
    for name, source in generation_files.items():
        body = source.read_bytes()
        (out / name).write_bytes(body)
        review_hashes[name] = "sha256:" + hashlib.sha256(body).hexdigest()
    for source in Path(args.candidates).parent.glob("recipe_*"):
        if source.is_file():
            body = source.read_bytes()
            (out / source.name).write_bytes(body)
            review_hashes[source.name] = "sha256:" + hashlib.sha256(body).hexdigest()
    for name in ("preparation_authorization.json", "ai_revision_decisions.json", "curation_status.json",
                 "preparation_recipe.json", "selection_manifest.json", "researcher_cohorts.json", "sample_validation.json"):
        source = Path(args.candidates).parent / name
        if source.is_file():
            body = source.read_bytes()
            (out / name).write_bytes(body)
            review_hashes[name] = "sha256:" + hashlib.sha256(body).hexdigest()
    for name in ("ai_review_revisions.json", "protocol_amendments.json"):
        source = Path(args.candidates).parent / name
        if source.is_file():
            body = source.read_bytes()
            (out / name).write_bytes(body)
            review_hashes[name] = "sha256:" + hashlib.sha256(body).hexdigest()
    manifest = {"status": "ready", "material_version": "2", "seed": args.seed,
                "review_kinds": sorted({c.review_kind for c in candidates if c.approved}),
                "reviewers": sorted({c.reviewer for c in candidates if c.approved}),
                "human_review_completed": all(c.review_kind == "human" for c in candidates if c.approved),
                "label_policy": "retain original CUAD labels via frozen source registry; no additional adjudication",
                "registry_sha256": "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest(),
                "review_file_hashes": review_hashes,
                "source_registry_sha256": "sha256:" + hashlib.sha256(Path(args.registry).read_bytes()).hexdigest()}
    cohorts = out / "researcher_cohorts.json"
    if cohorts.exists():
        cohort_rows = json.loads(cohorts.read_text(encoding="utf-8"))
        if {row["case_id"] for row in cohort_rows} != set(clean.case_ids):
            raise ValueError("cohort mapping does not match the frozen clean registry")
        manifest["cohort_counts"] = dict(Counter(row["cohort"] for row in cohort_rows))
    (out / "material_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
