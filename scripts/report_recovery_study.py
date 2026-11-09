"""Render separate small/medium recovery reports from offline analysis snapshots.

Standard library only. This command neither scores model outputs nor runs an
experiment. Frozen plans provide an export-coverage check for CLI reports.
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta, timezone
import hashlib
from html import escape
import json
import math
from pathlib import Path
from statistics import median


TERMINAL = {"completed", "failed"}
CONDITIONS = ("A1V0D0", "A1V1D0", "A1V2D0")
CONTRASTS = (("A1V2D0", "A1V1D0"), ("A1V1D0", "A1V0D0"), ("A1V2D0", "A1V0D0"))
SENSITIVITY_CASES = {"CASE-0044", "CASE-0052"}
PROCESS_FIELDS = {
    "manager_tool_calls": "Manager 工具调用", "manager_search_calls": "Manager 搜索",
    "manager_open_calls": "Manager 打开", "manager_failed_tool_calls": "Manager 失败调用",
    "compliance_tool_calls": "Compliance 工具调用", "compliance_search_calls": "Compliance 搜索",
    "compliance_open_calls": "Compliance 打开", "compliance_failed_tool_calls": "Compliance 失败调用",
    "total_tokens": "累计 token", "model_latency_seconds": "模型请求往返秒数",
}
ROLE_FIELDS = (
    "tool_calls", "search_calls", "page_calls", "open_calls", "failed_tool_calls",
    "failed_search_calls", "failed_page_calls", "failed_open_calls", "repeated_search_calls",
    "additional_distinct_queries_within_target", "distinct_returned_paragraphs",
    "distinct_opened_paragraphs", "reported_relied_without_open", "first_gold_open_call",
    "calls_after_first_gold_open", "known_tool_latency_seconds", "tool_latency_missing_calls",
)
for _role in ("manager", "compliance"):
    for _name in ("page_calls", "failed_search_calls", "failed_page_calls", "failed_open_calls",
                  "repeated_search_calls", "additional_distinct_queries_within_target",
                  "distinct_returned_paragraphs", "distinct_opened_paragraphs"):
        PROCESS_FIELDS[_role + "_" + _name] = _role + " " + _name


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan_fingerprint(plan):
    """Frozen v4 RunPlan.fingerprint wire contract, without runtime dependencies.

    The manifest is the fully serialized plan emitted by RunPlan.write_json.
    Keep this implementation version restricted; a regression checks it against
    the owning RunPlan implementation on the actual frozen study manifest.
    """
    if plan.get("plan_version") != "1" or plan.get("experiment_version") != "4":
        raise ValueError("report requires a fully serialized v4 frozen plan")
    payload = {field: plan[field] for field in (
        "plan_version", "experiment_version", "experiment_id", "seed", "repeat_count", "condition_ids",
        "case_ids", "models_version", "model_fingerprint")}
    payload["jobs"] = [[job[field] for field in ("experiment_id", "case_id", "error_condition", "condition_id", "repeat_index")]
                       for job in plan["jobs"]]
    material = json.dumps(payload, sort_keys=True)
    if plan.get("runtime_config") is not None:
        material += json.dumps({"runtime": plan["runtime_config"], "profile": plan.get("profile_fingerprint"),
                                "registry": plan.get("registry_fingerprint"), "disclosure": plan.get("disclosure_levels")}, sort_keys=True)
    if plan.get("selected_error_conditions") is not None:
        material += json.dumps({"selected_error_conditions": plan["selected_error_conditions"]}, sort_keys=True)
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fraction(rows, field):
    return {"numerator": sum(row.get(field) is True for row in rows), "denominator": len(rows)}


def measurements(rows, field):
    values = [row[field] for row in rows if number(row.get(field))]
    return {"known_sum": sum(values), "available": len(values), "missing": len(rows) - len(values),
            "mean_available": sum(values) / len(values) if values else None,
            "median_available": median(values) if values else None,
            "minimum": min(values) if values else None, "maximum": max(values) if values else None}


def observation_metrics(rows):
    result = {"planned": len(rows), "contracts": len({r["contract_id"] for r in rows}),
              "statuses": dict(Counter(r["execution_status"] for r in rows))}
    for role in ("manager", "compliance"):
        metrics = {name: measurements(rows, role + "_" + name) for name in ROLE_FIELDS}
        metrics.update({name: fraction(rows, role + "_" + name) for name in (
            "gold_returned", "gold_opened", "gold_opened_and_adopted_proxy", "fact_recovered", "tool_budget_reached")})
        metrics["any_tool_calls"] = {"numerator": sum(number(r.get(role + "_tool_calls")) and
                                                        r[role + "_tool_calls"] > 0 for r in rows), "denominator": len(rows)}
        result[role] = metrics
    return result


def summarize(rows):
    result = {}
    for condition in CONDITIONS:
        group = [row for row in rows if row["condition_id"] == condition]
        e1 = [row for row in group if row["error_condition"] == "E1"]
        e0 = [row for row in group if row["error_condition"] == "E0"]
        positive_e0 = [row for row in e0 if row["is_negative_sentinel"] is False]
        negative = [row for row in group if row["is_negative_sentinel"] is True]
        result[condition] = {
            "planned": len(group), "statuses": dict(Counter(row["execution_status"] for row in group)),
            "e1_recovery_per_planned": fraction(e1, "final_recovered_delivered"),
            "e1_recovery_among_completed": fraction([r for r in e1 if r["execution_status"] == "completed"], "final_recovered_delivered"),
            "e1_evidence_overlap_proxy": fraction(e1, "final_recovery_with_open_overlap_proxy"),
            "e0_positive_retention": fraction(positive_e0, "final_recovered_delivered"),
            "e0_false_positive": fraction(e0, "final_any_false_positive_delivered"),
            "negative_false_positive": fraction(negative, "final_any_false_positive_delivered"),
            "negative_false_positive_among_completed": fraction([r for r in negative if r["execution_status"] == "completed"], "final_any_false_positive_delivered"),
            "correct_action": fraction(group, "correct_action_delivered"),
            "e1_correct_action": fraction(e1, "correct_action_delivered"),
            "e1_recovery_correct_action_no_false_positive": {
                "numerator": sum(r.get("final_recovered_delivered") is True and r.get("correct_action_delivered") is True
                                 and r.get("final_any_false_positive_delivered") is False for r in e1), "denominator": len(e1)},
            "total_tokens": measurements(group, "total_tokens"),
            "known_total_tokens": measurements(group, "known_total_tokens"),
            "model_round_trip_seconds": measurements(group, "model_latency_seconds"),
            "known_model_round_trip_seconds": measurements(group, "known_model_latency_seconds"),
            "missing_model_latency_calls": sum(r.get("model_latency_missing_calls", 0) for r in group),
            "model_calls": sum(r.get("model_calls") or 0 for r in group),
            "runs_without_model_calls": sum(not r.get("model_calls") for r in group),
            "malformed_tool_log_lines": sum(r.get("malformed_tool_log_lines") or 0 for r in group),
            "malformed_model_log_lines": sum(r.get("malformed_model_log_lines") or 0 for r in group),
            "usage_missing_calls": sum(r.get("usage_missing_calls") or 0 for r in group),
            "e1_terminal_status": dict(Counter("no_delivery" if r["execution_status"] != "completed"
                                               else r.get("compliance_target_status") or "unrecorded" for r in e1)),
            "failure_categories": dict(Counter(r.get("failure_category") or "unclassified" for r in group if r["execution_status"] in ("failed", "interrupted"))),
        }
        for field in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "known_prompt_tokens",
                      "known_completion_tokens", "known_reasoning_tokens"):
            result[condition][field] = measurements(group, field)
        sources = Counter()
        for row in group:
            sources.update((row.get("reasoning_observation") or {}).get("token_sources") or {})
        result[condition]["reasoning_token_sources"] = dict(sources)
        observations = [r.get("reasoning_observation") or {} for r in group]
        result[condition]["observed_reasoning_tokens"] = measurements(observations, "tokens")
        result[condition]["known_observed_reasoning_tokens"] = measurements(observations, "known_tokens")
        result[condition]["observed_reasoning_tokens_missing_calls"] = sum(r.get("tokens_missing_calls") or 0 for r in observations)
        result[condition]["model_calls_without_reasoning_observation"] = sum((r.get("model_calls") or 0) for r in group if not r.get("reasoning_observation"))
        for role in ("manager", "compliance"):
            metrics = {name: measurements(group, role + "_" + name) for name in ROLE_FIELDS}
            metrics.update({name: fraction(e1, role + "_" + name) for name in (
                "gold_returned", "gold_opened", "gold_opened_and_adopted_proxy", "fact_recovered")})
            result[condition][role] = metrics
        result[condition]["process_strata"] = {name: observation_metrics(subset) for name, subset in (
            ("E1", e1), ("E0_positive", positive_e0), ("E0_negative", negative))}
        result[condition]["role_recovery_transition_e1"] = dict(Counter(
            "output_missing" if not isinstance(r.get("manager_fact_recovered"), bool) or not isinstance(r.get("compliance_fact_recovered"), bool)
            else f'manager_{int(r["manager_fact_recovered"])}_compliance_{int(r["compliance_fact_recovered"])}' for r in e1))
    return result


def pairs(rows, treatment, control, error):
    grouped = defaultdict(dict)
    for row in rows:
        if row["condition_id"] not in (treatment, control) or row["error_condition"] != error:
            continue
        key = (row["case_id"], row["repeat_index"])
        if row["condition_id"] in grouped[key]:
            raise ValueError("duplicate case/error/repeat/condition cell")
        grouped[key][row["condition_id"]] = row
    matched = []
    for key, cells in sorted(grouped.items()):
        if set(cells) != {treatment, control}:
            raise ValueError("missing counterpart in analysis rows")
        t, c = cells[treatment], cells[control]
        if t["contract_id"] != c["contract_id"]:
            raise ValueError("paired contract identities differ")
        matched.append((t, c))
    return matched


def outcome_effect(main_pairs, summary, treatment, control, completed_only=False):
    planned_pairs = len(main_pairs)
    if completed_only:
        main_pairs = [(t, c) for t, c in main_pairs if t["execution_status"] == c["execution_status"] == "completed"]
    differences = [int(t.get("final_recovered_delivered") is True) - int(c.get("final_recovered_delivered") is True)
                   for t, c in main_pairs]
    estimate = sum(differences) / len(differences) if differences else None
    primary = {"treatment": treatment, "control": control, "error_condition": "E1",
               "planned_pairs": planned_pairs, "excluded_incomplete_pairs": planned_pairs - len(main_pairs),
               "contracts": len({t["contract_id"] for t, _ in main_pairs}), "completed_pairs_only": completed_only,
               "treatment_successes": sum(t["final_recovered_delivered"] is True for t, _ in main_pairs),
               "control_successes": sum(c["final_recovered_delivered"] is True for _, c in main_pairs),
               "pairs": len(differences), "gains": differences.count(1), "losses": differences.count(-1),
               "risk_difference": estimate, "contract_bootstrap_95_interval": None}
    upstream = next((item for item in summary.get("paired_comparisons", [])
                     if item["treatment"] == treatment and item["control"] == control
                     and item["error_condition"] == "E1" and item["completed_pairs_only"] is completed_only
                     and item["field"] == "final_recovered_delivered"), None)
    if upstream:
        if any(upstream[field] != primary[field] for field in ("pairs", "planned_pairs", "risk_difference", "gains", "losses")):
            raise ValueError("summary primary comparison differs from analysis rows")
        if upstream["all_selected_runs_terminal"] is not True:
            raise ValueError("summary paired terminal status differs from analysis rows")
        primary["contract_bootstrap_95_interval"] = upstream["contract_bootstrap_95_interval"]
        primary["bootstrap_samples"] = upstream["bootstrap_samples"]
        primary["bootstrap_seed"] = upstream["bootstrap_seed"]
    return primary


def contract_results(rows, ready):
    grouped = defaultdict(list)
    for row in rows:
        if row["error_condition"] == "E1":
            grouped[row["contract_id"]].append(row)
    result = []
    for contract, group in sorted(grouped.items()):
        item = {"contract_id": contract, "case_ids": sorted({r["case_id"] for r in group}),
                "target_categories": sorted({r.get("target_category") or "unrecorded" for r in group}), "conditions": {}}
        for condition in CONDITIONS:
            sub = [r for r in group if r["condition_id"] == condition]
            item["conditions"][condition] = {**fraction(sub, "final_recovered_delivered"),
                "completed": sum(r["execution_status"] == "completed" for r in sub)}
        t, c = item["conditions"]["A1V2D0"], item["conditions"]["A1V1D0"]
        item["v2_minus_v1"] = t["numerator"] / t["denominator"] - c["numerator"] / c["denominator"] if ready and t["denominator"] and c["denominator"] else None
        result.append(item)
    return result


def effects(rows, summary, ready):
    if not ready:
        return {"status": "withheld_until_batch_terminal", "primary": None, "completed_pairs_secondary": None,
                "secondary_comparisons": [], "process_pairs": []}
    main_pairs = pairs(rows, "A1V2D0", "A1V1D0", "E1")
    primary = outcome_effect(main_pairs, summary, "A1V2D0", "A1V1D0")
    completed = outcome_effect(main_pairs, summary, "A1V2D0", "A1V1D0", True)
    secondary = [outcome_effect(pairs(rows, t, c, "E1"), summary, t, c) for t, c in CONTRASTS[1:]]
    process = []
    for treatment, control in CONTRASTS:
        for error in ("E0", "E1"):
            matched = pairs(rows, treatment, control, error)
            for field, label in PROCESS_FIELDS.items():
                available = [(t[field], c[field]) for t, c in matched if number(t.get(field)) and number(c.get(field))]
                deltas = [t - c for t, c in available]
                process.append({"treatment": treatment, "control": control, "error_condition": error,
                    "field": field, "label": label, "planned_pairs": len(matched), "available_pairs": len(available),
                    "missing_pairs": len(matched) - len(available),
                    "mean_paired_difference": sum(deltas) / len(deltas) if deltas else None})
    return {"status": "exploratory_completed_batch", "primary": primary,
            "completed_pairs_secondary": completed, "secondary_comparisons": secondary, "process_pairs": process}


def load_rows(directory):
    jsonl = directory / "recovery_runs.jsonl"
    if jsonl.exists():
        return [json.loads(line) for line in jsonl.read_text(encoding="utf-8-sig").split("\n") if line.strip()], jsonl
    path = directory / "recovery_runs.csv"
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = []
        for record in csv.DictReader(stream):
            row = {}
            for field, value in record.items():
                if value in ("True", "False"):
                    row[field] = value == "True"
                elif value == "":
                    row[field] = None
                else:
                    try:
                        row[field] = json.loads(value)
                    except ValueError:
                        row[field] = value
            rows.append(row)
    return rows, path


def load_phase(directory, label, plan_path=None, *, sensitivity=False, purpose="exploratory_effect"):
    plan = read_json(plan_path) if plan_path else None
    summary_path = directory / "recovery_summary.json"
    if not summary_path.exists():
        return {"label": label, "purpose": purpose, "status": "snapshot_unavailable", "ready": False,
                "planned": len(plan["jobs"]) if plan else None, "conditions": {}, "effects": effects([], {}, False)}
    summary = read_json(summary_path)
    rows, rows_path = load_rows(directory)
    run_ids = [row["run_id"] for row in rows]
    if len(run_ids) != len(set(run_ids)):
        raise ValueError("duplicate run IDs")
    if plan:
        jobs = {job["run_id"]: job for job in plan["jobs"]}
        if len(jobs) != len(plan["jobs"]):
            raise ValueError("duplicate run IDs in frozen plan")
        if set(run_ids) != set(jobs):
            raise ValueError("analysis rows do not cover the frozen plan")
        for row in rows:
            if any(row[field] != jobs[row["run_id"]][field] for field in (
                    "case_id", "contract_id", "condition_id", "error_condition", "repeat_index")):
                raise ValueError("analysis row identity differs from frozen job")
        if summary.get("plan_fingerprint") != plan_fingerprint(plan):
            raise ValueError("analysis summary fingerprint differs from frozen plan")
    for row in rows:
        if row["condition_id"] not in CONDITIONS or row["error_condition"] not in ("E0", "E1"):
            raise ValueError("unexpected recovery-study condition")
        if not isinstance(row.get("is_negative_sentinel"), bool):
            raise ValueError("negative-sentinel marker must be boolean")
        for field in ("final_recovered_delivered", "correct_action_delivered", "final_any_false_positive_delivered", "final_recovery_with_open_overlap_proxy"):
            if not isinstance(row.get(field), bool):
                raise ValueError(f"outcome {field} must be boolean")
        if row["execution_status"] not in TERMINAL | {"not_started", "interrupted"}:
            raise ValueError("unexpected execution status")
        if row["execution_status"] != "completed" and (row["final_recovered_delivered"] or row["correct_action_delivered"] or row["final_any_false_positive_delivered"]):
            raise ValueError("undelivered run cannot have a delivered outcome")
    aggregates = summarize(rows)
    for condition in CONDITIONS:
        upstream = summary.get("by_condition", {}).get(condition)
        if upstream is None or upstream["planned"] != aggregates[condition]["planned"]:
            raise ValueError("summary counts differ from analysis rows")
        if upstream["execution_statuses"] != aggregates[condition]["statuses"]:
            raise ValueError("summary execution statuses differ from analysis rows")
    terminal = sum(row["execution_status"] in TERMINAL for row in rows)
    ready = bool(rows) and terminal == len(rows) and summary.get("batch_complete") is True
    phase = {"label": label, "purpose": purpose, "status": "completed" if ready else "in_progress", "ready": ready,
             "planned": len(rows), "terminal": terminal, "conditions": aggregates,
             "plan_fingerprint": summary["plan_fingerprint"],
             "source_sha256": {rows_path.name: sha256(rows_path), summary_path.name: sha256(summary_path)},
             "effects": effects(rows, summary, ready), "review_candidates": summary.get("review_candidates", [])}
    phase["contract_e1_results"] = contract_results(rows, ready)
    phase["frozen_plan"] = {"path": str(plan_path.resolve()), "sha256": sha256(plan_path), "exact_job_coverage_verified": True} if plan_path else None
    phase["semantic_review"] = {"status": "not_attached", "note": "原评分表示目标输出为 present 且交付；语义支持依据另附的原文审查。"}
    phase["log_coverage"] = {"observed_runs_with_model_calls": sum(bool(r.get("model_calls")) for r in rows),
        "partial_or_unstarted_runs": sum(r["execution_status"] not in TERMINAL for r in rows),
        "malformed_tool_lines": sum(r.get("malformed_tool_log_lines") or 0 for r in rows),
        "malformed_model_lines": sum(r.get("malformed_model_log_lines") or 0 for r in rows),
        "note": "已有快照不区分日志文件缺失与合法零调用；partial 日志计数及已知 token/时间是观察下界。"}
    if sensitivity:
        excluded_contracts = {row["contract_id"] for row in rows if row["case_id"] in SENSITIVITY_CASES}
        excluded = [row for row in rows if row["contract_id"] in excluded_contracts]
        if any(row["is_negative_sentinel"] is not True or row["error_condition"] != "E0" for row in excluded):
            raise ValueError("sensitivity exclusions must contain only E0 negative controls")
        retained = [row for row in rows if row["contract_id"] not in excluded_contracts]
        phase["sensitivity"] = {"excluded_cases": sorted(SENSITIVITY_CASES), "excluded_runs": len(excluded),
            "excluded_contracts": sorted(excluded_contracts),
            "retained_runs": len(retained), "conditions": summarize(retained), "effects": effects(retained, summary, ready),
            "note": "固定排除 CASE-0044 与 CASE-0052 的负例敏感性分析。主分析保留全部冻结样本；E1配对样本不受此排除影响。"}
    return phase


def rate_text(value, *, provisional=False):
    n, d = value["numerator"], value["denominator"]
    if provisional:
        return f"已观察 {n}/{d}" if d else "已观察 0/0（无样本）"
    return f"{n}/{d}（{n / d:.1%}）" if d else "0/0（无样本）"


def numeric(value):
    return f"{value:,.2f}" if number(value) else "缺失"


def table(headers, rows):
    return '<div class="scroll"><table><thead><tr>' + ''.join(f'<th>{escape(str(h))}</th>' for h in headers) + \
        '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(f'<td>{escape(str(v))}</td>' for v in row) + '</tr>' for row in rows) + '</tbody></table></div>'


def result_tables(conditions, *, provisional=False):
    return table(["条件", "E1恢复/计划", "E1恢复/完成", "E1证据重合代理", "E0正例事实保留", "E0误报", "负例误报", "动作正确/计划"],
        [[condition] + [rate_text(values[key], provisional=provisional) for key in ("e1_recovery_per_planned", "e1_recovery_among_completed",
         "e1_evidence_overlap_proxy", "e0_positive_retention", "e0_false_positive", "negative_false_positive", "correct_action")]
         for condition, values in conditions.items()])


def effect_html(effect, *, pilot=False):
    if effect["primary"] is None:
        return '<p class="notice">批次仍在进行或尚无完整分析快照。主效应、置信区间及过程配对差值等待全部运行终态后再展示。</p>'
    primary = effect["primary"]
    interval = primary["contract_bootstrap_95_interval"]
    interval_text = f"[{interval[0] * 100:.2f}, {interval[1] * 100:.2f}] 个百分点" if interval else "不可用"
    difference = primary["risk_difference"]
    difference_text = f"{difference * 100:.2f} 个百分点" if difference is not None else "无E1配对样本"
    output = ('<h3>E1 流程描述 V2 − V1（小批次）</h3>' if pilot else '<h3>E1 主比较 V2 − V1</h3>') + table(
        ["计划配对", "独立合同", "改善对", "下降对", "恢复交付率差", "按合同重采样95%区间"],
        [[primary["pairs"], primary["contracts"], primary["gains"], primary["losses"], difference_text, interval_text]])
    completed = effect["completed_pairs_secondary"]
    output += '<h4>选择敏感性：两臂均完成的配对</h4>' + table(
        ["保留/计划配对", "排除配对", "V2成功/保留配对", "V1成功/保留配对", "配对率差（百分点）"],
        [[f'{completed["pairs"]}/{completed["planned_pairs"]}', completed["excluded_incomplete_pairs"],
          f'{completed["treatment_successes"]}/{completed["pairs"]}', f'{completed["control_successes"]}/{completed["pairs"]}',
          numeric(completed["risk_difference"] * 100) if completed["risk_difference"] is not None else "无可用配对"]])
    output += '<p>双臂完成配对采用共同分母；上表各条件完成者恢复率各用自身完成分母。区间为上游分析生成的探索性合同聚类区间。合同内重复相关，改善/下降计数为案例×重复配对数。退化区间与含零区间均不构成普遍有效、等效或无效证明。</p>'
    output += '<details><summary>次级 E1 比较</summary>' + table(
        ["比较", "计划配对", "合同", "改善对", "下降对", "差（百分点）"],
        [[i["treatment"] + " − " + i["control"], i["pairs"], i["contracts"], i["gains"], i["losses"],
          numeric(i["risk_difference"] * 100) if i["risk_difference"] is not None else "无配对"] for i in effect["secondary_comparisons"]]) + '</details>'
    output += '<p>过程配对均值按同一案例、材料条件和重复编号匹配，以原数值相减；缺失值不补零，按字段报告有效配对分母。以下过程比较是描述性探索，未做多重比较校正。</p>'
    output += '<details><summary>查看 E0 / E1 过程配对差值</summary>' + table(
        ["比较", "材料", "指标", "平均配对差", "可用/计划配对", "缺失配对"],
        [[item["treatment"] + " − " + item["control"], item["error_condition"], item["label"],
          numeric(item["mean_paired_difference"]), f'{item["available_pairs"]}/{item["planned_pairs"]}', item["missing_pairs"]]
         for item in effect["process_pairs"]]) + '</details>'
    return output


def strata_html(conditions, *, provisional=False):
    labels = {"E1": "E1遗漏正例", "E0_positive": "E0阳性保持", "E0_negative": "E0负例"}
    output = '<h3>按材料分层的角色过程</h3><p>计数展示可观测均值〔中位数；最小–最大〕。缺失日志和未完成运行的零计数仅表示当前观察，过程量可能为下界。证据标记按 gold 段落偏移重合计算；合规可继承上游证据，未自行打开需结合实际可见来源审查。</p>'
    for stratum, label in labels.items():
        records = []
        for condition, values in conditions.items():
            metrics = values["process_strata"][stratum]
            for role in ("manager", "compliance"):
                counts = []
                for field in ("tool_calls", "search_calls", "page_calls", "open_calls", "failed_tool_calls"):
                    m = metrics[role][field]
                    counts.append(f'{numeric(m["mean_available"])}〔{numeric(m["median_available"])}；{numeric(m["minimum"])}–{numeric(m["maximum"])}〕 N={m["available"]}')
                records.append([condition, role, f'{metrics["planned"]}/{metrics["contracts"]}'] + counts +
                               [rate_text(metrics[role]["any_tool_calls"], provisional=provisional), rate_text(metrics[role]["tool_budget_reached"], provisional=provisional)])
        output += '<h4>' + label + '</h4>' + table(["条件", "角色", "运行/合同", "工具", "搜索", "翻页", "打开", "失败", "有工具调用", "达到工具预算"], records)
    output += '<details><summary>细分过程和状态（包括失败类型、不同 query、打开后的调用）</summary>' + table(
        ["条件", "材料", "角色", "指标", "记录总数", "均值", "可用/计划"],
        [[condition, labels[stratum], role, name, numeric(m["known_sum"]), numeric(m["mean_available"]), f'{m["available"]}/{metric["planned"]}']
         for condition, values in conditions.items() for stratum, metric in values["process_strata"].items()
         for role in ("manager", "compliance") for name, m in metric[role].items() if "known_sum" in m]) + '</details>'
    return output


def phase_html(phase):
    status = {"completed": "全部终态", "in_progress": "进行中", "snapshot_unavailable": "尚无离线分析快照"}[phase["status"]]
    output = f'<section><h2>{escape(phase["label"])} <span class="badge">{status}</span></h2>'
    provisional = not phase["ready"]
    def rate(value):
        return rate_text(value, provisional=provisional)
    pilot = phase.get("purpose") == "workflow_pilot"
    if pilot:
        output += '<p class="notice">small 仅作为流程试验和运行门槛；其数值比较为流程描述。主要效果估计使用 medium，两个阶段分别报告。</p>'
    if not phase["conditions"]:
        return output + f'<p>冻结计划运行数为 {phase["planned"] if phase["planned"] is not None else "待提供"}。请在离线分析生成后重新运行本报告脚本。</p>' + effect_html(phase["effects"]) + '</section>'
    output += f'<p>终态 {phase["terminal"]}/{phase["planned"]}。未完成批次的累计计数仅表示当前观察，计划分母保留尚未交付的运行。未终态/部分日志可能来自仍在运行的任务，单凭此快照无法判定是否中断。</p>'
    if provisional:
        output += '<p class="notice">当前各条件运行进度不同，比例表仅显示已观察计数/表头所列分母，暂不显示百分率或效果结论。</p>'
    output += table(["条件", "计划", "完成交付", "失败", "未终态/部分日志", "尚未开始/其他"],
        [[condition, value["planned"], value["statuses"].get("completed", 0), value["statuses"].get("failed", 0),
          value["statuses"].get("interrupted", 0), sum(n for s, n in value["statuses"].items() if s not in TERMINAL | {"interrupted"})]
         for condition, value in phase["conditions"].items()])
    output += '<h3>结果指标</h3><p>评分终点为目标分类输出为 present 且工作流完成交付。下表沿用冻结标签；原文是否支持该判断由另附语义复核记录说明。E0阳性指标表示事实保留，负例误报按运行计数。</p>' + result_tables(phase["conditions"], provisional=provisional) + effect_html(phase["effects"], pilot=pilot)
    output += table(["条件", "E1动作正确/计划", "E1恢复且动作正确且无其它误报", "负例误报/已完成", "E1最终目标状态/无交付"],
        [[c, rate(v["e1_correct_action"]), rate(v["e1_recovery_correct_action_no_false_positive"]),
          rate(v["negative_false_positive_among_completed"]), json.dumps(v["e1_terminal_status"], ensure_ascii=False)] for c, v in phase["conditions"].items()])
    output += '<h3>逐合同 E1 交付结果</h3>' + table(["合同", "案例", "类别", "V0成功/计划", "V1成功/计划", "V2成功/计划", "V2−V1（百分点）"],
        [[item["contract_id"], ", ".join(item["case_ids"]), ", ".join(item["target_categories"])] +
         [rate(item["conditions"][c]) + f'，完成 {item["conditions"][c]["completed"]}' for c in CONDITIONS] +
         [numeric(item["v2_minus_v1"] * 100) if item["v2_minus_v1"] is not None else "整批终态后展示"] for item in phase["contract_e1_results"]])
    output += '<h3>全材料角色过程汇总</h3><p>调用列展示已记录总数及可用记录均值。证据列分母为该条件全部E1计划运行；段落重合需要结合原文做语义复核。</p>'
    process_rows = []
    for condition, values in phase["conditions"].items():
        for role in ("manager", "compliance"):
            metrics = values[role]
            counts = [f'{metrics[key]["known_sum"]} / {numeric(metrics[key]["mean_available"])}' for key in (
                "tool_calls", "search_calls", "page_calls", "open_calls", "failed_tool_calls")]
            process_rows.append([condition, role] + counts + [rate(metrics[key]) for key in (
                "gold_returned", "gold_opened", "gold_opened_and_adopted_proxy", "fact_recovered")])
    output += table(["条件", "角色", "工具 总/均", "搜索 总/均", "翻页 总/均", "打开 总/均", "失败 总/均",
                     "E1返回证据", "E1打开证据", "E1打开且采用代理", "E1事实恢复"], process_rows)
    output += strata_html(phase["conditions"], provisional=provisional)
    output += table(["条件", "E1角色恢复转移", "失败类别（启发式，需原日志复核）"],
        [[c, json.dumps(v["role_recovery_transition_e1"], ensure_ascii=False), json.dumps(v["failure_categories"], ensure_ascii=False)] for c, v in phase["conditions"].items()])
    output += '<h3>累计用量与时间</h3><p>token 是多次请求累计量，单次上下文长度需要逐请求记录另行核查。模型时间为请求往返时间，工具时间为本地工具执行时间；这些时间包含系统与传输开销。small 与 medium 的传输方式不同（medium 使用 SSH 压缩），时间分别呈现。已知部分为下界。</p>'
    output += table(["条件", "已知累计token", "token完整运行/计划", "模型调用", "已知模型往返秒数", "缺失时间调用", "工具执行秒数", "缺失工具时间调用"],
        [[condition, numeric(value["known_total_tokens"]["known_sum"]), f'{value["total_tokens"]["available"]}/{value["planned"]}',
          value["model_calls"], numeric(value["known_model_round_trip_seconds"]["known_sum"]), value["missing_model_latency_calls"],
          numeric(sum(value[role]["known_tool_latency_seconds"]["known_sum"] for role in ("manager", "compliance"))),
          sum(value[role]["tool_latency_missing_calls"]["known_sum"] for role in ("manager", "compliance"))]
         for condition, value in phase["conditions"].items()])
    output += table(["条件", "已知prompt token", "已知completion token", "已知reasoning token", "reasoning计数来源", "usage缺失调用", "无模型记录运行", "坏模型/工具日志行"],
        [[c, numeric(v["known_prompt_tokens"]["known_sum"]), numeric(v["known_completion_tokens"]["known_sum"]), numeric(v["known_reasoning_tokens"]["known_sum"]),
          json.dumps(v["reasoning_token_sources"], ensure_ascii=False), v["usage_missing_calls"], v["runs_without_model_calls"],
          f'{v["malformed_model_log_lines"]}/{v["malformed_tool_log_lines"]}'] for c, v in phase["conditions"].items()])
    output += '<p>上表 reasoning token 为服务端 usage 字段；下表 reasoning observation 按日志记载的来源汇总，可包含本地 tokenizer 重编码。两种统计分别呈现，不叠加计入 total token。</p>'
    output += table(["条件", "观测 reasoning 已知token", "观测 reasoning 完整运行/计划", "已记录观测中的缺失调用", "无观测字段的已记录模型调用", "观测计数来源"],
        [[c, numeric(v["known_observed_reasoning_tokens"]["known_sum"]), f'{v["observed_reasoning_tokens"]["available"]}/{v["planned"]}',
          v["observed_reasoning_tokens_missing_calls"], v["model_calls_without_reasoning_observation"], json.dumps(v["reasoning_token_sources"], ensure_ascii=False)] for c, v in phase["conditions"].items()])
    output += '<p>' + escape(phase["log_coverage"]["note"]) + ' 本报告读取的行级快照未直接测量格式修复、上下文压缩次数及端到端墙钟时长；这些项目保留为未测量，不填写零次。</p>'
    if "sensitivity" in phase:
        sensitivity = phase["sensitivity"]
        output += '<h3>固定负例排除敏感性分析</h3><p>' + escape(sensitivity["note"]) + \
            f' 排除 {sensitivity["excluded_runs"]} 次运行，保留 {sensitivity["retained_runs"]} 次。</p>'
        output += result_tables(sensitivity["conditions"], provisional=provisional) + effect_html(sensitivity["effects"])
    output += '<details><summary>待复核记录</summary>' + table(["运行", "定位标记"],
        [[item["run_id"], ", ".join(item["flags"])] for item in phase["review_candidates"]]) + '</details>'
    review = phase["semantic_review"]
    output += '<h3>语义复核附件</h3><p>' + (escape(review["path"]) + '；SHA256 ' + review["sha256"] if review["status"] == "attached" else '待附 AI / 人工原文复核记录。当前主表不将未审查记录升级为语义支持恢复。') + '</p>'
    output += '<p>复核范围固定为全部 E1 最终恢复、V2/V1不一致配对、负例误报及无交付；待复核标记用于定位，未标记也需按上述范围检查。原评分保持不变。</p>'
    return output + '<p class="muted">冻结计划指纹 ' + escape(phase["plan_fingerprint"]) + '</p></section>'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--small", type=Path, required=True)
    parser.add_argument("--medium", type=Path, required=True)
    parser.add_argument("--small-plan", type=Path, required=True)
    parser.add_argument("--medium-plan", type=Path, required=True)
    parser.add_argument("--small-review", type=Path)
    parser.add_argument("--medium-review", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {"report_version": "recovery-study-report-v1",
              "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
              "small": load_phase(args.small, "小规模流程试验", args.small_plan, purpose="workflow_pilot"),
              "medium": load_phase(args.medium, "中等批量探索性实验", args.medium_plan, sensitivity=True)}
    for phase, path in (("small", args.small_review), ("medium", args.medium_review)):
        if path:
            report[phase]["semantic_review"] = {"status": "attached", "path": str(path.resolve()), "sha256": sha256(path)}
    css = 'body{font:15px/1.6 system-ui,sans-serif;color:#182735;background:#f3f6f8;margin:0}main{max-width:1500px;margin:30px auto;padding:0 24px}section{background:white;border:1px solid #dce4eb;border-radius:12px;padding:24px;margin:24px 0}h1,h2,h3{line-height:1.3}.badge{font-size:14px;background:#fff1bd;padding:5px 10px;border-radius:6px}.notice{padding:16px;background:#fff1bd;border-left:4px solid #a56c00}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;margin:16px 0;font-variant-numeric:tabular-nums}th,td{border:1px solid #dde4ea;padding:9px;text-align:left;white-space:nowrap}th{background:#edf3f8}tr:nth-child(even){background:#fafcfd}summary{cursor:pointer;font-weight:600}.muted{color:#617081;overflow-wrap:anywhere}'
    html = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>恢复目标提示词实验报告</title><style>' + css + '</style><main><h1>恢复目标提示词实验报告</h1><p>small 与 medium 分别报告。结果基于冻结计划的离线分析快照，失败记录保留在主分母中。</p><p>生成时间 ' + escape(report["generated_at"]) + '</p>' + phase_html(report["small"]) + phase_html(report["medium"]) + '</main></html>'
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "recovery_study_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output / "recovery_study_report.html").write_text(html, encoding="utf-8")
    print(json.dumps({"small_status": report["small"]["status"], "medium_status": report["medium"]["status"], "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
