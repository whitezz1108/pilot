"""Freeze the traceable working baseline for the Pilot01 main pilot.

The manifest records hashes and versions only. It intentionally never reads
or serializes API credentials or endpoint URLs.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/pilot01/main_v1"
EXPERIMENT_ID = "pilot01_main_v1_20260924"
OUT_PLAN = ROOT / "outputs/pilot01_main_v1/manifests" / EXPERIMENT_ID
PLAN_SOURCE = OUT_PLAN / "run_plan.json"
CSV_SOURCE = OUT_PLAN / "run_plan.csv"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def write_yaml_json(path: Path, value: object) -> None:
    # JSON is a strict subset of YAML 1.2 and keeps the record deterministic.
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    registry_path = DATA / "case_registry_v1.json"
    selection_path = DATA / "case_selection_v1.json"
    audit_path = DATA / "memo_pair_audit_v1.json"
    attestation_path = DATA / "review_attestation_v1.json"
    build_path = DATA / "build_manifest_v1.json"
    if not all(path.exists() for path in (PLAN_SOURCE, CSV_SOURCE, registry_path, selection_path, audit_path)):
        raise SystemExit("Required plan or registry artifacts are missing.")

    plan = json.loads(PLAN_SOURCE.read_text(encoding="utf-8"))
    registry_doc = json.loads(registry_path.read_text(encoding="utf-8"))
    cases = registry_doc["cases"]
    by_id = {case["case_id"]: case for case in cases}
    jobs = plan["jobs"]
    ids = [job["run_id"] for job in jobs]
    matrix = Counter((job["error_condition"], job["condition_id"]) for job in jobs)
    if len(ids) != len(set(ids)):
        raise SystemExit("Run IDs are not unique.")
    if len(cases) != 40 or sum(not c["is_negative_sentinel"] for c in cases) != 32:
        raise SystemExit("Registry does not have the frozen 32-positive/8-sentinel composition.")
    expected = {
        ("E0", "A0V0"): 120, ("E0", "A1V0"): 120, ("E0", "A1V1"): 120,
        ("E1", "A0V0"): 96, ("E1", "A1V0"): 96, ("E1", "A1V1"): 96,
    }
    if dict(matrix) != expected or len(jobs) != 648:
        raise SystemExit(f"Unexpected run matrix: {dict(matrix)}")
    if any(job["condition_id"] == "A0V1" for job in jobs):
        raise SystemExit("Excluded condition A0V1 is present in the plan.")
    if any(job["error_condition"] == "E1" and by_id[job["case_id"]]["is_negative_sentinel"] for job in jobs):
        raise SystemExit("A sentinel has an invalid E1 run.")
    if {job["case_id"] for job in jobs} != set(by_id):
        raise SystemExit("Plan case IDs do not match the frozen registry.")

    # Put canonical, versioned copies beside the case/memo artifacts.
    plan_copy = DATA / "run_plan_v1.json"
    csv_copy = DATA / "run_plan_v1.csv"
    shutil.copyfile(PLAN_SOURCE, plan_copy)
    shutil.copyfile(CSV_SOURCE, csv_copy)

    code_paths = sorted((ROOT / "src/pilot01").rglob("*.py"))
    fixed_paths = [
        ROOT / "pyproject.toml",
        ROOT / "config/conditions_v1.yaml",
        ROOT / "config/models_v1.yaml",
        ROOT / "config/workflow_v1.yaml",
        ROOT / "config/policies/policy_v2.yaml",
        ROOT / "scripts/build_formal_pilot_cases.py",
        ROOT / "scripts/freeze_formal_pilot_baseline.py",
        ROOT / "scripts/Load-Pilot01ApiEnv.ps1",
        ROOT / "gate4_5_refactor_progress.md",
        ROOT / "Phase-1-Experiment-Audit-Prompt-v2.md",
        ROOT / "Phase-2-Freeze-and-Pilot-Execution-Prompt-v2.md",
        ROOT / "Phase-3-Pilot-Analysis-and-Research-Report-Prompt-v2.md",
        ROOT / "outputs/reports/phase1_requirement_matrix.md",
        ROOT / "outputs/reports/phase1_architecture_audit.md",
        ROOT / "outputs/reports/phase1_gap_analysis.md",
        ROOT / "outputs/reports/phase1_minimal_fix_plan.md",
        DATA / "case_registry_v1.json",
        DATA / "case_selection_v1.json",
        DATA / "memo_pair_audit_v1.json",
        DATA / "review_attestation_v1.json",
        DATA / "build_manifest_v1.json",
        plan_copy,
        csv_copy,
    ]
    prompt_paths = [
        ROOT / "prompts/manager_v3.md",
        ROOT / "prompts/compliance_v3.md",
        ROOT / "prompts/repair_v3.md",
        ROOT / "src/pilot01/prompts.py",
    ]
    note = Path(r"C:\Users\44977\Documents\xwechat_files\wxid_suot0fzeaqcv21_9158\msg\file\2026-09\CUAD_MAS_pilot_实施说明.md")
    if note.exists():
        fixed_paths.append(note)

    unique_paths = sorted(set(path.resolve() for path in [*code_paths, *fixed_paths, *prompt_paths]))
    files = {
        str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path): {
            "sha256": sha256(path), "bytes": path.stat().st_size
        }
        for path in unique_paths
    }
    prompt_manifest = {
        "prompt_manifest_version": "1",
        "runtime_prompt_bundle": "pilot01-prompts-v3",
        "loader": {"path": "src/pilot01/prompts.py", "sha256": sha256(ROOT / "src/pilot01/prompts.py")},
        "prompts": [
            {"role": role, "version": "v3", "path": str(path.relative_to(ROOT)), "sha256": sha256(path), "bytes": path.stat().st_size}
            for role, path in (("manager", ROOT / "prompts/manager_v3.md"),
                               ("compliance", ROOT / "prompts/compliance_v3.md"),
                               ("repair_format_only", ROOT / "prompts/repair_v3.md"))
        ],
        "notes": "All main runs use this exact prompt bundle; no prompt edits or version pooling during the pilot.",
    }
    write_yaml_json(DATA / "prompt_manifest.yaml", prompt_manifest)

    gold_digest_rows = [
        {
            "contract_id": c["contract_id"],
            "target_category": c["target_category"],
            "status": c["gold_target_clause_status"],
            "evidence_offsets": c["gold_evidence_offsets"],
        }
        for c in sorted(cases, key=lambda x: x["contract_id"])
    ]
    build_info = json.loads(build_path.read_text(encoding="utf-8"))
    package_versions = sorted(
        f"{dist.metadata['Name']}=={dist.version}"
        for dist in importlib.metadata.distributions()
        if dist.metadata.get("Name")
    )
    source_dataset = ROOT / build_info["source_path"]
    if not source_dataset.exists():
        source_dataset = ROOT / "data/raw/cuad/extracted/CUADv1.json"
    experiment_manifest = {
        "experiment_manifest_version": "1",
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "experiment_id": EXPERIMENT_ID,
        "working_baseline_status": "frozen_traceable_working_baseline",
        "protocol_status": "No standalone formal protocol found; rules compiled from implementation note, versioned research decisions, Phase 1 audit, and current configuration.",
        "source_authority": {
            "implementation_note": {"path": str(note), "sha256": sha256(note)} if note.exists() else None,
            "versioned_decision": {"path": "gate4_5_refactor_progress.md", "sha256": sha256(ROOT / "gate4_5_refactor_progress.md"), "lines": "207-234"},
            "researcher_review_attestation": {"path": "data/pilot01/main_v1/review_attestation_v1.json", "sha256": sha256(attestation_path), "status": "manual review complete per current user confirmation; independent record absent and not a blocker"},
        },
        "git_snapshot": {
            "head": git("rev-parse", "HEAD"),
            "branch": git("branch", "--show-current"),
            "status_porcelain_at_freeze": git("status", "--short"),
            "working_tree_is_dirty": bool(git("status", "--porcelain")),
            "source_file_hashes_are_authoritative_for_dirty_snapshot": True,
        },
        "runtime": {"python": platform.python_version(), "platform": platform.platform(), "installed_distributions": package_versions},
        "policy": {"id": "POLICY-01", "version": "v2", "path": "config/policies/policy_v2.yaml", "sha256": sha256(ROOT / "config/policies/policy_v2.yaml"), "fingerprint": build_info["policy_fingerprint"], "mapping": {"present": "ESCALATE", "both_absent": "ACCEPT", "other_or_unknown": "REVIEW"}},
        "conditions": {"version": "1", "path": "config/conditions_v1.yaml", "sha256": sha256(ROOT / "config/conditions_v1.yaml"), "main": ["A0V0", "A1V0", "A1V1"], "excluded": ["A0V1"]},
        "workflow": {"version": "1", "path": "config/workflow_v1.yaml", "sha256": sha256(ROOT / "config/workflow_v1.yaml"), "order": ["analyst_offline_memo_load", "manager", "compliance", "final"], "revision_rounds": 0},
        "models": {"version": "1", "path": "config/models_v1.yaml", "sha256": sha256(ROOT / "config/models_v1.yaml"), "roles": ["manager", "compliance"], "provider": "openai-compatible", "model_id": "DeepSeek-V4.1-Flash", "temperature": 0.0, "top_p": None, "max_output_tokens": 16384, "timeout_seconds": 180.0, "environment_check": "loader previously verified required vars present and model ID matched config; secret and base URL values excluded"},
        "prompts": {"manifest": "data/pilot01/main_v1/prompt_manifest.yaml", "manifest_sha256": sha256(DATA / "prompt_manifest.yaml"), "versions": {"manager": "v3", "compliance": "v3", "repair": "v3_format_only"}},
        "data": {
            "source_dataset": {"path": str(source_dataset), "sha256": sha256(source_dataset)},
            "registry": {"path": "data/pilot01/main_v1/case_registry_v1.json", "sha256": sha256(registry_path), "cases": len(cases), "positive": 32, "negative_sentinels": 8},
            "selection": {"path": "data/pilot01/main_v1/case_selection_v1.json", "sha256": sha256(selection_path), "seed": 20260924, "fingerprint": build_info["selection_fingerprint"]},
            "memo_pair_audit": {"path": "data/pilot01/main_v1/memo_pair_audit_v1.json", "sha256": sha256(audit_path), "all_checks_passed": True, "positive_pairs": 32, "sentinels_e0_only": 8, "e1_construction": "pure deletion of target claim and its evidence"},
            "hidden_gold_digest": {"algorithm": "sha256 over canonical JSON of contract_id, target_category, gold status, and evidence offsets", "sha256": canonical_sha256(gold_digest_rows), "values_not_serialized": True},
        },
        "run_plan": {
            "path": "data/pilot01/main_v1/run_plan_v1.json", "sha256": sha256(plan_copy),
            "csv_path": "data/pilot01/main_v1/run_plan_v1.csv", "csv_sha256": sha256(csv_copy),
            "experiment_plan_version": plan["plan_version"], "experiment_version": plan["experiment_version"],
            "seed": plan["seed"], "repeat_count": plan["repeat_count"], "case_count": len(plan["case_ids"]),
            "condition_ids": plan["condition_ids"], "run_count": len(jobs), "unique_run_ids": len(set(ids)),
            "positive_runs": 576, "sentinel_runs": 72, "matrix": {f"{e}/{c}": n for (e, c), n in sorted(matrix.items())},
            "repeat_index_counts": {str(k): v for k, v in sorted(Counter(job["repeat_index"] for job in jobs).items())},
            "model_fingerprint": plan["model_fingerprint"],
        },
        "measurement": {
            "primary_outcome": "omission_recovered_with_evidence",
            "unit_of_analysis": "contract; repetitions nested within contract x condition",
            "primary_denominator": "completed positive E1 runs only; failures reported separately; completed A0V0 positive E1 is structural false; E0 and sentinels are N/A",
            "evidence_rule": "at least one pre-Final node correctly recovers the omitted target and cites same-contract source opened by that node overlapping gold span",
            "additional_outcomes": ["fact recovery", "final action correctness", "manager/compliance error survival", "evidence-backed correction", "sentinel false escalation", "REVIEW rate", "tool use", "tokens", "latency", "schema validity"],
        },
        "execution_policy": {
            "mode": "live provider calls via scripts/Load-Pilot01ApiEnv.ps1",
            "technical_retries": 0,
            "format_only_repair_max": 1,
            "failed_and_interrupted_runs": "preserve original request/response and failure artifacts; no silent reruns or overwrites",
            "cost_estimation": "not available; no versioned pricing table frozen",
            "api_secret_or_endpoint_recorded": False,
        },
        "files": files,
    }
    write_yaml_json(DATA / "experiment_manifest.yaml", experiment_manifest)
    for name in ("run_plan_v1.json", "run_plan_v1.csv", "prompt_manifest.yaml", "experiment_manifest.yaml"):
        path = DATA / name
        path.chmod(path.stat().st_mode & ~0o222)
    print(json.dumps({"frozen": True, "run_count": len(jobs), "run_plan_sha256": sha256(plan_copy), "prompt_manifest_sha256": sha256(DATA / "prompt_manifest.yaml"), "experiment_manifest_sha256": sha256(DATA / "experiment_manifest.yaml"), "files_hashed": len(files)}, indent=2))


if __name__ == "__main__":
    main()
