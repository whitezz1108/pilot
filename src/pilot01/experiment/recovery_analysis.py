"""Offline recovery analysis. Gold labels never enter the experiment runtime.

All outcomes use the frozen plan denominator. Paragraph overlap is a provenance
proxy and still requires semantic review before a claim of supported recovery.
"""
from __future__ import annotations

import csv
import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

from .artifacts import read_raw_run
from .scoring_v2 import analyze_plan

ROLES = ("manager", "compliance")
CONTRASTS = (("A1V1D0", "A1V0D0"), ("A1V2D0", "A1V1D0"),
             ("A1V2D0", "A1V0D0"))


def _partial_records(path):
    records, malformed = [], 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("record must be an object")
                records.append(record)
            except (ValueError, TypeError):
                malformed += 1
    return records, malformed


def process_metrics(calls, *, gold_ids, relied_ids=(), has_output=False,
                    applicable=True):
    """Count observed requests, including failures and explicit pagination."""
    searches, pages, opens = [], [], []
    returned, opened = set(), set()
    queries, first_gold_open = [], None
    for index, call in enumerate(calls):
        tool = call.get("tool")
        args = call.get("arguments") or {}
        is_page = tool == "next_search_page" or (
            tool == "search_contract" and bool(args.get("cursor")))
        if is_page:
            pages.append(call)
        elif tool == "search_contract":
            searches.append(call)
            queries.append((args.get("target_category"),
                            " ".join(str(args.get("query", "")).casefold().split())))
        elif tool == "open_source_span":
            opens.append(call)
        if call.get("ok"):
            ids = set(call.get("result_ids") or ())
            if tool in ("search_contract", "next_search_page"):
                returned.update(ids)
            elif tool == "open_source_span":
                opened.update(ids)
                if first_gold_open is None and ids & gold_ids:
                    first_gold_open = index + 1
    by_target = defaultdict(set)
    for target, query in queries:
        by_target[target].add(query)
    relied = set(relied_ids)
    known_latencies = [c["latency_seconds"] for c in calls
                       if c.get("latency_seconds") is not None]
    result = {
        "tool_calls": len(calls),
        "failed_tool_calls": sum(c.get("ok") is not True for c in calls),
        "search_calls": len(searches), "page_calls": len(pages),
        "open_calls": len(opens),
        "failed_search_calls": sum(c.get("ok") is not True for c in searches),
        "failed_page_calls": sum(c.get("ok") is not True for c in pages),
        "failed_open_calls": sum(c.get("ok") is not True for c in opens),
        "distinct_queries": len(set(queries)),
        "repeated_search_calls": len(queries) - len(set(queries)),
        "additional_distinct_queries_within_target": sum(max(0, len(q) - 1) for q in by_target.values()),
        "distinct_returned_paragraphs": len(returned),
        "distinct_opened_paragraphs": len(opened),
        "distinct_reported_relied_paragraphs": len(relied) if has_output else None,
        "reported_relied_without_open": len(relied - opened) if has_output else None,
        "tool_latency_missing_calls": len(calls) - len(known_latencies),
        "known_tool_latency_seconds": sum(known_latencies),
        "tool_latency_seconds": sum(known_latencies) if len(calls) == len(known_latencies) else None,
        "gold_returned": bool(returned & gold_ids) if applicable else None,
        "gold_opened": bool(opened & gold_ids) if applicable else None,
        "gold_opened_and_adopted_proxy": bool(opened & relied & gold_ids) if applicable and has_output else None,
        "first_gold_open_call": first_gold_open if applicable else None,
        "calls_after_first_gold_open": len(calls) - first_gold_open if first_gold_open is not None else None,
    }
    return result


def enrich_rows(plan, registry, raw_root):
    registry_fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(registry.to_payload(), sort_keys=True).encode()).hexdigest()
    if plan.registry_fingerprint is not None and plan.registry_fingerprint != registry_fingerprint:
        raise ValueError("analysis registry differs from the frozen plan")
    jobs = {job.run_id: job for job in plan.jobs}
    rows, base_summary = analyze_plan(plan, registry, Path(raw_root))
    for row in rows:
        case = registry.get(row["case_id"])
        row["is_negative_sentinel"] = case.is_negative_sentinel
        row["target_category"] = case.target_category
        directory = Path(raw_root) / row["run_id"]
        document = registry.document(row["case_id"])
        gold_ids = {p.paragraph_id for p in document.paragraphs
                    if any(start < p.end_char and p.start_char < end
                           for start, end in case.gold_spans())}
        artifact = None
        if (directory / "run.json").exists():
            raw = read_raw_run(directory)
            artifact = raw.artifact
            job = jobs[row["run_id"]]
            identity_fields = (
                "run_id", "experiment_id", "case_id", "contract_id", "condition_id",
                "error_condition", "repeat_index", "source_access", "verification_required",
                "recovery_required", "models_version", "model_fingerprint",
            )
            for field in identity_fields:
                if getattr(artifact, field) != getattr(job, field):
                    raise ValueError(f"raw artifact {field} differs from the planned job")
            if artifact.profile_fingerprint != plan.profile_fingerprint:
                raise ValueError("raw artifact profile differs from the frozen plan")
            expected_runtime = plan.runtime_config.model_dump(mode="json") if plan.runtime_config else None
            if artifact.runtime_config != expected_runtime:
                raise ValueError("raw artifact runtime differs from the frozen plan")
            expected_disclosure = (plan.disclosure_levels or {}).get(job.condition_id)
            if artifact.disclosure_level != expected_disclosure:
                raise ValueError("raw artifact disclosure differs from the frozen plan")
            calls = [c.model_dump(mode="json") for c in raw.tool_calls]
            model_calls = [c.model_dump(mode="json") for c in raw.model_calls]
            malformed_tools = malformed_models = 0
        else:
            calls, malformed_tools = _partial_records(directory / "tool_calls.jsonl")
            model_calls, malformed_models = _partial_records(directory / "model_calls.jsonl")
        for record in (*calls, *model_calls):
            if record.get("run_id") is not None and record["run_id"] != row["run_id"]:
                raise ValueError("log record run_id differs from the planned directory")
        row["malformed_tool_log_lines"] = malformed_tools
        row["malformed_model_log_lines"] = malformed_models
        row["model_latency_missing_calls"] = sum(c.get("latency_seconds") is None for c in model_calls)
        known_model_latency = sum(c.get("latency_seconds") or 0 for c in model_calls)
        row["model_latency_seconds"] = known_model_latency if model_calls and not row["model_latency_missing_calls"] else None
        row["known_model_latency_seconds"] = known_model_latency
        for role in ROLES:
            output = getattr(artifact.execution, role + "_output") if artifact else None
            relied = output.evidence_provenance.relied_paragraph_ids or () if output else ()
            metrics = process_metrics(
                [c for c in calls if c.get("node") == role], gold_ids=gold_ids,
                relied_ids=relied, has_output=output is not None,
                applicable=not case.is_negative_sentinel)
            row.update({role + "_" + key: value for key, value in metrics.items()})
            row[role + "_tool_budget_reached"] = (
                metrics["tool_calls"] >= plan.runtime_config.max_tool_requests_per_node
            ) if plan.runtime_config is not None else None
            row[role + "_false_positive_categories"] = [
                category for category, status in output.target_clause_status.items()
                if category in case.policy.target_clause_categories
                and status.value == "present" and case.gold_status_for(category).value == "absent"
            ] if output else None
            row[role + "_any_false_positive"] = bool(row[role + "_false_positive_categories"]) if output else None
        row["compliance_lost_recovery"] = (
            row.get("manager_fact_recovered") is True and row.get("compliance_fact_recovered") is False
        ) if row.get("compliance_target_status") is not None and not case.is_negative_sentinel else None
        row["manager_recovered_without_final_delivery"] = (
            row.get("manager_fact_recovered") is True and not row["final_recovered_delivered"]
        ) if not case.is_negative_sentinel else None
        row["final_recovery_with_open_overlap_proxy"] = bool(
            row["final_recovered_delivered"] and row.get("compliance_gold_opened_and_adopted_proxy"))
        row["final_any_false_positive_delivered"] = bool(
            row["execution_status"] == "completed" and row.get("compliance_any_false_positive"))
    return rows, base_summary


def rate(rows, field):
    numerator = sum(row.get(field) is True for row in rows)
    return {"numerator": numerator, "denominator": len(rows),
            "rate": numerator / len(rows) if rows else None}


def condition_summary(rows):
    e1 = [r for r in rows if r["error_condition"] == "E1"]
    e1_completed = [r for r in e1 if r["execution_status"] == "completed"]
    e0 = [r for r in rows if r["error_condition"] == "E0"]
    e0_positive = [r for r in e0 if not r["is_negative_sentinel"]]
    negative = [r for r in rows if r["is_negative_sentinel"]]
    completed = [r for r in rows if r["execution_status"] == "completed"]
    summary = {
        "planned": len(rows), "execution_statuses": dict(Counter(r["execution_status"] for r in rows)),
        "e1_recovery_per_planned": rate(e1, "final_recovered_delivered"),
        "e1_recovery_among_completed": rate(e1_completed, "final_recovered_delivered"),
        "e1_recovery_with_open_overlap_proxy": rate(e1, "final_recovery_with_open_overlap_proxy"),
        "correct_action_per_planned": rate(rows, "correct_action_delivered"),
        "correct_action_among_completed": rate(completed, "correct_action_delivered"),
        "e0_correct_action_per_planned": rate(e0, "correct_action_delivered"),
        "e0_positive_fact_retention_per_planned": rate(e0_positive, "final_recovered_delivered"),
        "e0_false_positive_per_planned": rate(e0, "final_any_false_positive_delivered"),
        "negative_false_positive_per_planned": rate(negative, "final_any_false_positive_delivered"),
        "negative_false_positive_among_completed": rate(
            [r for r in negative if r["execution_status"] == "completed"], "final_any_false_positive_delivered"),
        "runs_missing_total_tokens": sum(r.get("total_tokens") is None for r in rows),
        "known_total_tokens": sum(r.get("known_total_tokens", 0) for r in rows),
        "model_latency_missing_calls": sum(r["model_latency_missing_calls"] for r in rows),
        "runs_without_model_calls": sum(not r.get("model_calls") for r in rows),
        "known_model_latency_seconds": sum(r["known_model_latency_seconds"] for r in rows),
    }
    for role in ROLES:
        metrics = {}
        for name in ("tool_calls", "search_calls", "page_calls", "open_calls", "failed_tool_calls",
                     "repeated_search_calls", "additional_distinct_queries_within_target",
                     "distinct_returned_paragraphs", "distinct_opened_paragraphs",
                     "tool_latency_missing_calls", "known_tool_latency_seconds"):
            values = [r[role + "_" + name] for r in rows]
            metrics[name] = {"sum": sum(values), "mean_per_planned": sum(values) / len(rows) if rows else None}
        for name in ("gold_returned", "gold_opened", "gold_opened_and_adopted_proxy", "fact_recovered"):
            metrics["e1_" + name] = rate(e1, role + "_" + name)
        metrics["tool_budget_reached"] = rate(rows, role + "_tool_budget_reached")
        summary[role] = metrics
    return summary


def paired_effect(rows, treatment, control, *, field="final_recovered_delivered",
                  error_condition="E1", completed_only=False, bootstrap_samples=4000, seed=1109):
    """Paired risk difference with whole-contract resampling.

    The estimand weights planned case/repeat pairs equally. Resampling preserves
    all dependent cases and repeats from the same contract. Missing counterpart
    cells cause refusal instead of silent dropping. Completed-only analysis uses
    pairs where both arms completed, and reports that reduced denominator.
    """
    if bootstrap_samples < 0:
        raise ValueError("bootstrap_samples must be nonnegative")
    selected = [r for r in rows if r["condition_id"] in (treatment, control)
                and r["error_condition"] == error_condition]
    batch_complete = all(r["execution_status"] in ("completed", "failed") for r in selected)
    cells = {}
    for row in selected:
        key = (row["case_id"], row["error_condition"], row["repeat_index"], row["condition_id"])
        if key in cells:
            raise ValueError("duplicate paired cell")
        cells[key] = row
    pair_keys = sorted({key[:3] for key in cells})
    clusters = defaultdict(list)
    gains = losses = treatment_success = control_success = excluded = 0
    for key in pair_keys:
        if (*key, treatment) not in cells or (*key, control) not in cells:
            raise ValueError("missing counterpart in paired comparison")
        t, c = cells[(*key, treatment)], cells[(*key, control)]
        if t["contract_id"] != c["contract_id"]:
            raise ValueError("paired cases refer to different contracts")
        if completed_only and (t["execution_status"] != "completed" or c["execution_status"] != "completed"):
            excluded += 1
            continue
        tv, cv = int(t.get(field) is True), int(c.get(field) is True)
        clusters[t["contract_id"]].append(tv - cv)
        treatment_success += tv
        control_success += cv
        gains += tv > cv
        losses += tv < cv
    n = sum(len(v) for v in clusters.values())
    estimate = (treatment_success - control_success) / n if n else None
    interval = None
    if batch_complete and len(clusters) >= 2 and bootstrap_samples > 0:
        rng = random.Random(seed)
        blocks = [clusters[k] for k in sorted(clusters)]
        draws = []
        for _ in range(bootstrap_samples):
            sampled = [rng.choice(blocks) for _ in blocks]
            draws.append(sum(sum(v) for v in sampled) / sum(len(v) for v in sampled))
        draws.sort()
        interval = [draws[int((len(draws) - 1) * .025)], draws[int((len(draws) - 1) * .975)]]
    return {"treatment": treatment, "control": control, "field": field,
            "error_condition": error_condition, "completed_pairs_only": completed_only,
            "planned_pairs": len(pair_keys), "excluded_incomplete_pairs": excluded,
            "pairs": n, "contracts": len(clusters), "treatment_successes": treatment_success,
            "control_successes": control_success, "gains": gains, "losses": losses,
            "risk_difference": estimate, "contract_bootstrap_95_interval": interval,
            "bootstrap_samples": bootstrap_samples, "bootstrap_seed": seed,
            "all_selected_runs_terminal": batch_complete,
            "interval_note": "Exploratory percentile interval; no adjustment for multiple comparisons."
            if interval is not None else "Interval unavailable for unfinished batches, fewer than two contracts, or zero bootstrap samples."}


def analyze_recovery(plan, registry, raw_root, *, bootstrap_samples=4000, seed=1109):
    rows, base_summary = enrich_rows(plan, registry, raw_root)
    conditions = set(plan.condition_ids)
    comparisons = []
    for treatment, control in CONTRASTS:
        if {treatment, control} <= conditions:
            for completed_only in (False, True):
                comparisons.append(paired_effect(rows, treatment, control, completed_only=completed_only,
                                                 bootstrap_samples=bootstrap_samples, seed=seed))
    candidates = []
    for row in rows:
        flags = []
        if row["error_condition"] == "E1" and row["final_recovered_delivered"] and not row["final_recovery_with_open_overlap_proxy"]:
            flags.append("recovery_without_self_opened_gold_overlap_proxy")
        if row.get("compliance_lost_recovery"):
            flags.append("manager_recovery_lost_at_compliance")
        if row.get("compliance_gold_opened") and row.get("compliance_fact_recovered") is False:
            flags.append("gold_opened_but_not_recovered")
        if row["final_any_false_positive_delivered"]:
            flags.append("absent_category_reported_present")
        if row["execution_status"] in ("failed", "interrupted"):
            flags.append("execution_failure_or_interruption")
        if flags:
            candidates.append({"run_id": row["run_id"], "flags": flags})
    summary = {"analysis_version": "recovery-process-v1", "plan_fingerprint": plan.fingerprint(),
               "batch_complete": base_summary["batch_complete"],
               "by_condition": {c: condition_summary([r for r in rows if r["condition_id"] == c])
                                for c in plan.condition_ids},
               "paired_comparisons": comparisons, "review_candidates": candidates,
               "interpretation": "Exploratory results. Gold paragraph overlap is a procedural proxy, not semantic proof. "
                                 "Failed and unstarted runs remain in the planned denominator. Partial logs are lower bounds. "
                                 "Cases and repetitions from one contract are dependent. E0 positive status is retention, not omission recovery."}
    return rows, summary


def write_analysis(plan, registry, raw_root, output, *, bootstrap_samples=4000, seed=1109):
    rows, summary = analyze_recovery(plan, registry, raw_root, bootstrap_samples=bootstrap_samples, seed=seed)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "recovery_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "recovery_runs.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    fields = sorted({key for row in rows for key in row})
    with (output / "recovery_runs.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in row.items()})
    lines = ["恢复目标提示词实验分析", "", summary["interpretation"], "",
             "| 条件 | 计划运行 | E1 恢复/计划 | E1 恢复/完成 | 恢复且打开采用 gold 段落 | 正确动作/计划 |",
             "|---|---|---|---|---|---|"]
    for condition, result in summary["by_condition"].items():
        values = [result[name] for name in ("e1_recovery_per_planned", "e1_recovery_among_completed",
                                            "e1_recovery_with_open_overlap_proxy", "correct_action_per_planned")]
        counts = [f'{v["numerator"]}/{v["denominator"]}' for v in values]
        lines.append(f'| {condition} | {result["planned"]} | ' + " | ".join(counts) + " |")
    lines += ["", "配对差异按合同整体重采样。下列结果为 E1 全计划分母，区间供探索性分析。", ""]
    for comparison in summary["paired_comparisons"]:
        if comparison["completed_pairs_only"]:
            continue
        estimate = comparison["risk_difference"]
        text = f"{estimate * 100:.2f} 个百分点" if estimate is not None else "无可比较样本"
        lines.append(f'- {comparison["treatment"]} 对 {comparison["control"]}，差异 {text}，'
                     f'改善 {comparison["gains"]} 组、下降 {comparison["losses"]} 组，'
                     f'合同数 {comparison["contracts"]}，95% 区间 {comparison["contract_bootstrap_95_interval"]}。')
    lines += ["", f'待逐例检查的记录共 {len(summary["review_candidates"])} 条。标记用于定位证据，不构成机制结论。', ""]
    for candidate in summary["review_candidates"]:
        lines.append(f'- `{candidate["run_id"]}` — ' + ", ".join(candidate["flags"]))
    (output / "recovery_analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary
