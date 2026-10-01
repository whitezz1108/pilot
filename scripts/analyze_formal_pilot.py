"""Recompute Pilot01 contract-level descriptive and paired analyses.

This Phase 3 script reads only the frozen plan, scorer output, and raw run
artifacts. Bootstrap samples contracts, never API calls or replicates.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

from pilot01.experiment.runspec import RunPlan


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_ID = "pilot01_main_v1_20260924"
HIDDEN_KEYS = (
    "gold_target_clause_status",
    "gold_evidence_offsets",
    "is_negative_sentinel",
    "error_condition",
    "omission_target_claim_id",
    "omission_replacement_claim",
)
SECRET_PATTERNS = tuple(re.compile(x) for x in (
    r"\bsk-[A-Za-z0-9_\-]{16,}", r"\bsk-ant-[A-Za-z0-9_\-]{16,}",
    r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}",
    r"(?i)\b(api[_-]?key|authorization|x-api-key)\b\s*[:=]\s*\S{8,}",
    r"\bAKIA[0-9A-Z]{16}\b", r"\bghp_[A-Za-z0-9]{20,}",
))


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path):
    if not path.exists():
        return []
    # JSONL uses LF as its delimiter. splitlines() also splits valid U+2028
    # characters embedded in model text and therefore corrupts intact records.
    return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def proportion(values):
    values = [v for v in values if v is not None]
    return {"numerator": sum(v is True for v in values), "denominator": len(values),
            "rate": None if not values else sum(v is True for v in values) / len(values)}


def primary(row):
    if row["error_condition"] != "E1" or row["is_negative_sentinel"] or not row["completed"]:
        return None
    vals = (row["manager_corrected_with_evidence"], row["compliance_corrected_with_evidence"])
    if all(v is None for v in vals):
        return None
    return any(v is True for v in vals)


def any_bool(row, keys):
    vals = [row.get(k) for k in keys]
    vals = [v for v in vals if v is not None]
    return None if not vals else any(v is True for v in vals)


def pct(values, p):
    values = sorted(values)
    if not values:
        return None
    index = (len(values) - 1) * p
    lo, hi = math.floor(index), math.ceil(index)
    return values[lo] if lo == hi else values[lo] + (values[hi] - values[lo]) * (index - lo)


def metric_value(row, name):
    if name == "omission_recovered_with_evidence":
        return primary(row)
    if name == "target_fact_correct_any_node":
        return any_bool(row, ("manager_fact_recovery_correct", "compliance_fact_recovery_correct"))
    if name == "manager_error_survival":
        return row["error_survival_manager"]
    if name == "compliance_error_survival":
        return row["error_survival_compliance"]
    if name == "final_action_correct":
        return row["final_action_correct"]
    if name == "manager_all_target_statuses_correct":
        return row["manager_clause_status_correct"]
    if name == "compliance_all_target_statuses_correct":
        return row["compliance_clause_status_correct"]
    raise KeyError(name)


def contract_means(rows, metric, error):
    grouped = defaultdict(list)
    for row in rows:
        if row["is_negative_sentinel"] or row["error_condition"] != error:
            continue
        value = metric_value(row, metric)
        if value is not None:
            grouped[(row["contract_id"], row["condition_id"])].append(float(value))
    return {key: sum(values) / len(values) for key, values in grouped.items()}


def paired_bootstrap(rows, *, metric, error, condition_a, condition_b, seed, samples):
    means = contract_means(rows, metric, error)
    contracts = sorted({c for c, cond in means if cond == condition_a} &
                       {c for c, cond in means if cond == condition_b})
    diffs = [means[(c, condition_a)] - means[(c, condition_b)] for c in contracts]
    if not diffs:
        return {"metric": metric, "error_condition": error, "contrast": f"{condition_a} - {condition_b}",
                "contracts_paired": 0, "mean_risk_difference": None, "bootstrap_95_percentile_ci": None}
    rng = random.Random(seed)
    boot = [sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(samples)]
    return {
        "metric": metric, "error_condition": error,
        "contrast": f"{condition_a} - {condition_b}",
        "contracts_paired": len(diffs),
        "mean_risk_difference": sum(diffs) / len(diffs),
        "bootstrap_95_percentile_ci": [pct(boot, 0.025), pct(boot, 0.975)],
        "bootstrap_samples": samples, "bootstrap_seed": seed,
        "replicates_aggregated_within_contract_condition": True,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="data/pilot01/main_v1/experiment_manifest.yaml")
    ap.add_argument("--plan", default="data/pilot01/main_v1/run_plan_v1.json")
    ap.add_argument("--scores", default=f"outputs/pilot01_main_v1/processed/{EXPERIMENT_ID}/run_scores.jsonl")
    ap.add_argument("--raw-root", default=f"outputs/pilot01_main_v1/raw/{EXPERIMENT_ID}")
    ap.add_argument("--out-dir", default=f"outputs/pilot01_main_v1/analysis/{EXPERIMENT_ID}")
    ap.add_argument("--bootstrap-samples", type=int, default=10000)
    ap.add_argument("--bootstrap-seed", type=int, default=2026092403)
    args = ap.parse_args()

    manifest_path, plan_path, scores_path, raw_root, out_dir = [ROOT / x for x in
        (args.manifest, args.plan, args.scores, args.raw_root, args.out_dir)]
    manifest, plan = read_json(manifest_path), read_json(plan_path)
    score_rows = read_jsonl(scores_path)
    plan_jobs = plan["jobs"]
    planned_ids = [job["run_id"] for job in plan_jobs]
    planned_by_id = {job["run_id"]: job for job in plan_jobs}
    scored_by_id = {row["run_id"]: row for row in score_rows}
    raw_files = {p.parent.name: p for p in raw_root.glob("*/run.json")}
    failures = []
    raw_call_total = 0
    role_outputs = Counter()
    role_expected = Counter()
    raw_calls_by_id = {}
    bad_secret_records = []
    hidden_key_hits = []
    config_mismatches = []
    raw_usage_missing = 0
    raw_prompt_tokens = raw_completion_tokens = 0
    format_repairs = 0

    for run_id in planned_ids:
        artifact_path = raw_files.get(run_id)
        calls = []
        if artifact_path:
            calls = read_jsonl(artifact_path.parent / "model_calls.jsonl")
        raw_calls_by_id[run_id] = calls
        raw_call_total += len(calls)
        run = read_json(artifact_path) if artifact_path else None
        if run:
            failure = run.get("failure") or {}
            execution_status = (run.get("execution") or {}).get("status")
            if (execution_status not in (None, "completed") or failure.get("protocol_failure")
                    or failure.get("failure_type") or failure.get("error")
                    or failure.get("model_output_failure")):
                failures.append({"run_id": run_id, "type": failure.get("failure_type"), "stage": failure.get("failure_stage"), "protocol_failure": bool(failure.get("protocol_failure"))})
        for role in ("manager", "compliance"):
            role_expected[role] += 1
            role_calls = [c for c in calls if c.get("role") == role]
            final_calls = [c for c in role_calls if not c.get("is_tool_turn")]
            final = final_calls[-1] if final_calls else None
            role_outputs[role] += bool(final and final.get("parsed_output") is not None and not final.get("parse_error") and not final.get("error"))
            for c in role_calls:
                raw_input = c.get("rendered_input") or ""
                if any(key in raw_input for key in HIDDEN_KEYS) or run_id in raw_input:
                    hidden_key_hits.append({"run_id": run_id, "role": role, "call_index": c.get("call_index")})
                serialized = json.dumps(c, ensure_ascii=False)
                if any(p.search(serialized) for p in SECRET_PATTERNS):
                    bad_secret_records.append({"run_id": run_id, "role": role, "call_index": c.get("call_index")})
                params = c.get("params") or {}
                model_id = params.get("model_id")
                max_tokens = params.get("max_output_tokens")
                prompt_version = c.get("prompt_version")
                if model_id not in (None, manifest["models"]["model_id"]) or max_tokens not in (None, manifest["models"]["max_output_tokens"]):
                    config_mismatches.append({"run_id": run_id, "role": role, "model_id": model_id, "max_output_tokens": max_tokens})
                if prompt_version and "v3" not in prompt_version.lower():
                    config_mismatches.append({"run_id": run_id, "role": role, "prompt_version": prompt_version})
                if c.get("is_repair"):
                    format_repairs += 1
                usage = c.get("usage") or {}
                if usage.get("prompt_tokens") is None or usage.get("completion_tokens") is None:
                    raw_usage_missing += 1
                else:
                    raw_prompt_tokens += usage["prompt_tokens"]
                    raw_completion_tokens += usage["completion_tokens"]

    rows = [scored_by_id[x] for x in planned_ids if x in scored_by_id]
    rows_by_cell = defaultdict(list)
    for row in rows:
        rows_by_cell[("sentinel" if row["is_negative_sentinel"] else row["target_category"], row["error_condition"], row["condition_id"])].append(row)

    cells = []
    for (sample, error, condition), group in sorted(rows_by_cell.items()):
        metrics = {}
        for metric in ("omission_recovered_with_evidence", "target_fact_correct_any_node", "final_action_correct", "manager_all_target_statuses_correct", "compliance_all_target_statuses_correct", "manager_error_survival", "compliance_error_survival"):
            metrics[metric] = proportion([metric_value(r, metric) for r in group])
        metrics["false_escalation"] = proportion([r["false_escalation"] for r in group])
        metrics["review_action"] = proportion([None if not r["completed"] else r["final_decision"] == "REVIEW" for r in group])
        cells.append({"sample": sample, "error_condition": error, "condition_id": condition,
                      "planned_runs": len(group), "completed_runs": sum(r["completed"] for r in group),
                      "failed_runs": sum(not r["completed"] for r in group), "metrics": metrics})

    pairs = []
    for metric in ("omission_recovered_with_evidence", "target_fact_correct_any_node", "final_action_correct"):
        error = "E1" if metric != "final_action_correct" else "E0"
        pairs.append(paired_bootstrap(rows, metric=metric, error=error, condition_a="A1V0", condition_b="A0V0", seed=args.bootstrap_seed, samples=args.bootstrap_samples))
        pairs.append(paired_bootstrap(rows, metric=metric, error=error, condition_a="A1V1", condition_b="A1V0", seed=args.bootstrap_seed + 1, samples=args.bootstrap_samples))
    for condition in ("A0V0", "A1V0", "A1V1"):
        pairs.append(paired_bootstrap(rows, metric="final_action_correct", error="E1", condition_a=condition, condition_b=condition, seed=args.bootstrap_seed + 2, samples=args.bootstrap_samples))
    # Replace self-contrasts with preplanned within-condition E1 minus E0 action accuracy.
    pairs = pairs[:6]
    for condition in ("A0V0", "A1V0", "A1V1"):
        means = contract_means(rows, "final_action_correct", "E1")
        e0_means = contract_means(rows, "final_action_correct", "E0")
        contracts = sorted({c for c, cond in means if cond == condition} & {c for c, cond in e0_means if cond == condition})
        diffs = [means[(c, condition)] - e0_means[(c, condition)] for c in contracts]
        if diffs:
            rng = random.Random(args.bootstrap_seed + 100 + len(pairs))
            boot = [sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(args.bootstrap_samples)]
            pairs.append({"metric": "final_action_correct", "error_condition": "E1 - E0", "contrast": condition,
                          "contracts_paired": len(diffs), "mean_risk_difference": sum(diffs) / len(diffs),
                          "bootstrap_95_percentile_ci": [pct(boot, .025), pct(boot, .975)],
                          "bootstrap_samples": args.bootstrap_samples, "replicates_aggregated_within_contract_condition": True})
        else:
            pairs.append({"metric": "final_action_correct", "error_condition": "E1 - E0", "contrast": condition,
                          "contracts_paired": 0, "mean_risk_difference": None, "bootstrap_95_percentile_ci": None})

    out_dir.mkdir(parents=True, exist_ok=True)
    contract_csv = out_dir / "contract_condition_metrics.csv"
    metrics_for_csv = ("omission_recovered_with_evidence", "target_fact_correct_any_node", "final_action_correct", "manager_error_survival", "compliance_error_survival")
    with contract_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["contract_id", "case_id", "target_category", "sample", "error_condition", "condition_id", "planned_repeats", "completed_repeats", *metrics_for_csv])
        writer.writeheader()
        groups = defaultdict(list)
        for row in rows:
            groups[(row["contract_id"], row["case_id"], row["target_category"], row["is_negative_sentinel"], row["error_condition"], row["condition_id"])].append(row)
        for key, group in sorted(groups.items()):
            contract_id, case_id, target, sentinel, error, condition = key
            output = {"contract_id": contract_id, "case_id": case_id, "target_category": target,
                      "sample": "sentinel" if sentinel else "positive", "error_condition": error,
                      "condition_id": condition, "planned_repeats": plan["repeat_count"],
                      "completed_repeats": sum(r["completed"] for r in group)}
            for metric in metrics_for_csv:
                vals = [metric_value(r, metric) for r in group]
                vals = [float(x) for x in vals if x is not None]
                output[metric] = "" if not vals else sum(vals) / len(vals)
            writer.writerow(output)

    baseline_files = manifest.get("files", {})
    baseline_hash_mismatches = []
    for path_text, spec in baseline_files.items():
        path = Path(path_text)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists() or sha256(path) != spec["sha256"]:
            baseline_hash_mismatches.append(path_text)
    dataset_path = Path(manifest["data"]["source_dataset"]["path"])
    dataset_hash_matches = dataset_path.exists() and sha256(dataset_path) == manifest["data"]["source_dataset"]["sha256"]
    plan_expected_hash = manifest["run_plan"]["sha256"]
    plan_hash_matches = sha256(plan_path) == plan_expected_hash
    input_checks = {
        "manifest_sha256": sha256(manifest_path),
        "frozen_plan_sha256_matches_manifest": plan_hash_matches,
        "frozen_baseline_files_checked": len(baseline_files),
        "frozen_baseline_hash_mismatches": baseline_hash_mismatches,
        "source_dataset_sha256_matches_manifest": dataset_hash_matches,
        "prompt_manifest_sha256_matches": sha256(ROOT / manifest["prompts"]["manifest"]) == manifest["prompts"]["manifest_sha256"],
    }
    planned_set, scored_set, raw_set = set(planned_ids), set(scored_by_id), set(raw_files)
    failure_counts = Counter(f["type"] or "unspecified" for f in failures)
    output = {
        "analysis_version": "1",
        "experiment_id": plan["experiment_id"],
        "plan": {"planned_runs": len(planned_ids), "scored_runs": len(scored_by_id), "raw_run_records": len(raw_files),
                 "canonical_fingerprint": RunPlan.load_json(plan_path).fingerprint(),
                 "missing_score_ids": sorted(planned_set - scored_set), "extra_score_ids": sorted(scored_set - planned_set),
                 "missing_raw_ids": sorted(planned_set - raw_set), "extra_raw_ids": sorted(raw_set - planned_set),
                 "matrix_counts": {f"{e}/{c}": sum(j["error_condition"] == e and j["condition_id"] == c for j in plan_jobs)
                                   for e in ("E0", "E1") for c in plan["condition_ids"]}},
        "input_checks": input_checks,
        "execution": {"failed_runs": len(failures), "completed_or_nonfailed_runs": len(raw_files) - len(failures),
                      "failures_by_type": dict(failure_counts), "failures": failures,
                      "raw_model_calls": raw_call_total, "format_repair_calls": format_repairs,
                      "raw_tool_calls_scored": sum(r["tool_calls"] for r in rows),
                      "tool_failures_scored": sum(r["tool_failures"] for r in rows),
                      "total_prompt_tokens_reported": raw_prompt_tokens, "total_completion_tokens_reported": raw_completion_tokens,
                      "model_call_usage_missing_count": raw_usage_missing,
                      "total_estimated_cost_usd": None,
                      "cost_status": "Unable to estimate: no versioned pricing table was frozen."},
        "quality_checks": {"role_output_success": {role: {"successes": role_outputs[role], "expected": role_expected[role],
                                                             "rate": None if not role_expected[role] else role_outputs[role] / role_expected[role]}
                                                        for role in ("manager", "compliance")},
                           "credential_shaped_content_findings": bad_secret_records,
                           "hidden_treatment_or_gold_request_findings": hidden_key_hits,
                           "model_or_prompt_config_mismatches": config_mismatches},
        "results_by_sample_error_condition_governance": cells,
        "contract_paired_comparisons": pairs,
        "bootstrap": {"sampling_unit": "contract", "repetitions_aggregated_within_contract_condition": True,
                      "samples": args.bootstrap_samples, "seed": args.bootstrap_seed,
                      "interval": "two-sided percentile 95% interval; descriptive pilot interval, not a hypothesis test"},
        "uncompleted_calls_not_scored_as_wrong_answers": True,
        "artifacts": {"contract_condition_metrics_csv": str(contract_csv.relative_to(ROOT))},
    }
    out_path = out_dir / "phase3_analysis.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"analysis": str(out_path), "contract_condition_csv": str(contract_csv),
                      "planned": len(planned_ids), "scored": len(scored_by_id), "raw": len(raw_files),
                      "failed": len(failures), "baseline_hash_mismatches": len(baseline_hash_mismatches),
                      "plan_hash_matches": plan_hash_matches}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
