"""Record a pre-outcome eligibility amendment, preserving the original plan."""
import hashlib
import json
from pathlib import Path

from pilot01.experiment.development.neutral_materials import verify_ready_registry
from pilot01.experiment.layout import ExperimentPaths
from pilot01.experiment.runspec import RunPlan, build_run_plan, write_run_plan_csv
from pilot01.runtime import ExperimentProfile


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/recovery_v4"
EXCLUSIONS = {
    "CASE-0015": "Annotated withdrawal of a securities registration is not termination of this contract.",
    "CASE-0006": "Unadjudicated conflict between the CoC definition and assignment by operation of law.",
    "CASE-0055": "Unadjudicated scope of early termination with a substantial cancellation fee.",
    "CASE-0066": "Source is an amendment/tail fragment, insufficient for full-agreement absence.",
    "CASE-0056": "Unadjudicated CoC scope of an absolute assignment-by-operation-of-law prohibition.",
    "CASE-0076": "Amendment preserves missing underlying license agreements and supplies no standalone termination terms.",
    "CASE-0049": "Appendix A statement of work incorporates a missing Software Development Agreement.",
}


def main():
    original = json.loads((OUT / "study_design.json").read_text(encoding="utf-8"))
    for experiment_id in ("r4m-1109", "r4m2-1109"):
        raw = OUT / "raw" / experiment_id
        if raw.exists() and any(raw.iterdir()):
            raise ValueError("medium outcomes already exist; this is no longer a pre-outcome amendment")
    registry = verify_ready_registry(OUT / "materials/case_registry_v2.json")
    profile = ExperimentProfile(ROOT / "config/experiment_recovery_v4.yaml")
    if profile.fingerprint() != original["profile_fingerprint"]:
        raise ValueError("runtime changed after initial freeze")
    original_plan = RunPlan.load_json(OUT / "manifests/r4m-1109/run_plan.json")
    original_plan.check(profile.conditions, registry)
    if (original_plan.profile_fingerprint != profile.fingerprint()
            or list(original_plan.case_ids) != original["medium"]["cases"]
            or original_plan.repeat_count != original["medium"]["repeats"]
            or original_plan.seed != original["selection_seed"]
            or len(original_plan.jobs) != original["medium"]["expected_runs"]):
        raise ValueError("original plan does not match the frozen design")
    registry_fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(registry.to_payload(), sort_keys=True).encode()).hexdigest()
    if original_plan.registry_fingerprint != registry_fingerprint:
        raise ValueError("material changed after the original plan freeze")
    groups = {}
    small = set(original["small"]["cases"])
    seed = original["selection_seed"]
    for case in registry.cases:
        if case.case_id in small or case.case_id in EXCLUSIONS:
            continue
        group = "negative" if case.is_negative_sentinel else case.target_category
        groups.setdefault(group, []).append(case.case_id)
    selected = []
    for group, cases in sorted(groups.items()):
        cases.sort(key=lambda cid: hashlib.sha256(f"{seed}:{cid}".encode()).hexdigest())
        selected.extend(cases[:4])
    replacements = sorted(set(selected) - set(original["medium"]["cases"]))
    review_path = OUT / "preflight/medium_replacement_review.json"
    if not review_path.exists():
        print(json.dumps({"proposed_cases": selected, "needs_review": replacements}, indent=2))
        raise SystemExit("Replacement source review is required before successor freeze.")
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if set(review.get("approved_cases", [])) != set(replacements):
        raise ValueError("replacement approvals do not match the proposed sample")
    if review.get("registry_sha256") != hashlib.sha256((OUT / "materials/case_registry_v2.json").read_bytes()).hexdigest():
        raise ValueError("replacement review refers to different material")
    plan = build_run_plan(experiment_id="r4m2-1109", registry=registry,
                          conditions=profile.conditions, models=profile.models,
                          case_ids=tuple(selected), repeat_count=2, seed=seed,
                          runtime_config=profile.runtime,
                          profile_fingerprint=profile.fingerprint())
    if len(selected) != 12 or len(plan.jobs) != 120:
        raise ValueError("successor must preserve sample size and strata")
    amendment = {"amendment": "pre-outcome-semantic-eligibility-v1",
                 "superseded_plan": "r4m-1109", "successor_plan": plan.experiment_id,
                 "exclusions": EXCLUSIONS, "medium_cases": selected,
                 "replacements": replacements, "selection_seed": seed,
                 "review_sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
                 "original_plan_sha256": hashlib.sha256((OUT / "manifests/r4m-1109/run_plan.json").read_bytes()).hexdigest(),
                 "registry_sha256": review["registry_sha256"],
                 "original_design_sha256": hashlib.sha256((OUT / "study_design.json").read_bytes()).hexdigest(),
                 "profile_fingerprint": profile.fingerprint(),
                 "note": "Source-only review before medium inference; original plan and labels retained. Small is an independent harness pilot and excluded from primary effect estimates."}
    target = OUT / "study_design_amendment.json"
    content = json.dumps(amendment, ensure_ascii=False, indent=2) + "\n"
    if target.exists() and target.read_text(encoding="utf-8") != content:
        raise ValueError("existing amendment differs")
    paths = ExperimentPaths(root=OUT, experiment_id=plan.experiment_id).ensure()
    if paths.run_plan_json.exists() and RunPlan.load_json(paths.run_plan_json).fingerprint() != plan.fingerprint():
        raise ValueError("existing successor plan differs")
    plan.write_json(paths.run_plan_json)
    write_run_plan_csv(plan, paths.run_plan_csv)
    target.write_text(content, encoding="utf-8")
    print(json.dumps(amendment, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
