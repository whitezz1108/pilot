"""Freeze outcome-independent sampling and paired plans before live calls."""
import hashlib
import json
from pathlib import Path

from pilot01.experiment.development.neutral_materials import verify_ready_registry
from pilot01.experiment.layout import ExperimentPaths
from pilot01.experiment.runspec import build_run_plan, write_run_plan_csv
from pilot01.runtime import ExperimentProfile


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/recovery_v4"
SEED = 20261109


def main():
    registry_path = OUT / "materials/case_registry_v2.json"
    registry = verify_ready_registry(registry_path)
    profile = ExperimentProfile(ROOT / "config/experiment_recovery_v4.yaml")
    groups = {}
    for case in registry.cases:
        group = "negative" if case.is_negative_sentinel else case.target_category
        groups.setdefault(group, []).append(case.case_id)
    for cases in groups.values():
        cases.sort(key=lambda cid: hashlib.sha256(f"{SEED}:{cid}".encode()).hexdigest())
    small, medium = [], []
    for group, cases in sorted(groups.items()):
        small.extend(cases[:1])
        medium.extend(cases[1:5])
    if len(small) != 3 or len(medium) != 12 or set(small) & set(medium):
        raise ValueError("expected two positive strata and a negative stratum")
    design = {
        "selection_seed": SEED,
        "selection": "SHA256(seed:case_id), stratified, disjoint small and medium",
        "small": {"cases": small, "repeats": 1, "expected_runs": 15},
        "medium": {"cases": medium, "repeats": 2, "expected_runs": 120},
        "primary_contrast": "A1V2D0 minus A1V1D0",
        "primary_endpoint": "E1 final fact recovered and delivered / all planned E1 runs",
        "secondary": ["completed-run recovery", "evidence overlap proxy", "correct actions",
                      "E0 fact retention", "sentinel false positives", "tools, tokens and latency"],
        "inference": "paired contract-cluster bootstrap, 4000 draws, seed 1109, exploratory",
        "promotion_gate": "all small jobs terminal, >=80% delivered; inspect every failure and actual request boundaries; no unresolved harness or leakage defect",
        "failure_policy": "retain original attempts; any corrected rerun gets a new plan ID; no replacement of failures",
        "generation": "temperature 1.0; no per-request sampling seed; selection seed controls plan order only",
        "material_scope": "uniform category/status summaries; inherited CUAD gold and AI reviews; no new human adjudication",
        "profile_fingerprint": profile.fingerprint(),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / "study_design.json"
    content = json.dumps(design, ensure_ascii=False, indent=2) + "\n"
    if target.exists() and target.read_text(encoding="utf-8") != content:
        raise ValueError("existing frozen design differs; use an explicitly versioned successor")
    target.write_text(content, encoding="utf-8")
    for stage, cases, repeats, count in (("s", small, 1, 15), ("m", medium, 2, 120)):
        plan = build_run_plan(experiment_id=f"r4{stage}-1109", registry=registry,
                              conditions=profile.conditions, models=profile.models,
                              case_ids=tuple(cases), repeat_count=repeats, seed=SEED,
                              runtime_config=profile.runtime,
                              profile_fingerprint=profile.fingerprint())
        if len(plan.jobs) != count:
            raise ValueError("unexpected planned run count")
        paths = ExperimentPaths(root=OUT, experiment_id=plan.experiment_id).ensure()
        if paths.run_plan_json.exists():
            from pilot01.experiment.runspec import RunPlan
            if RunPlan.load_json(paths.run_plan_json).fingerprint() != plan.fingerprint():
                raise ValueError("existing frozen plan differs")
        plan.write_json(paths.run_plan_json)
        write_run_plan_csv(plan, paths.run_plan_csv)
        print(plan.experiment_id, len(plan.jobs), paths.run_plan_json)
    print(json.dumps({"small": small, "medium": medium}, ensure_ascii=False))


if __name__ == "__main__":
    main()
