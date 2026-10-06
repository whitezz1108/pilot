"""Finish a frozen validator preflight: score, replay, QC, and denominator-aware report."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pilot01.experiment.artifacts import iter_raw_runs
from pilot01.experiment.cases import CaseRegistry
from pilot01.experiment.layout import ExperimentPaths
from pilot01.experiment.runspec import RunPlan
from pilot01.experiment.scoring import score_experiment
from pilot01.experiment.summary import Rate, summarise
from validator_trace_audit import audit_runs


def ratio(success: int, total: int) -> dict:
    return {"numerator": success, "denominator": total,
            "value": None if not total else round(success / total, 6)}


def classify_failure(row, artifact):
    if not row.protocol_failure:
        return None
    error = artifact.failure.error or ""
    if re.search(r"requested more than \d+ tool\(s\)", error):
        return "tool_budget_exhausted"
    if "finish_reason='length'" in error:
        return "output_token_limit"
    if row.verification_failure:
        return "validator_rejected"
    if row.model_output_failure:
        return "model_or_output_failure"
    return "other_protocol_failure"


def analyze(root: Path, experiment_id: str, registry_path: Path) -> dict:
    paths = ExperimentPaths(root=root, experiment_id=experiment_id)
    registry = CaseRegistry.load_json(registry_path)
    plan = RunPlan.load_json(paths.run_plan_json)
    manifest = json.loads((root / "validator_manifest.json").read_text(encoding="utf-8"))
    # Older frozen manifests used four requests and have no explicit budget field.
    tool_budget = manifest.get("max_tool_rounds", 4)
    unchanged = all(Path(path).is_file() and hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
                    for path, digest in manifest["sha256"].items())
    if not unchanged:
        raise ValueError("Frozen implementation/configuration/input hashes changed during preflight")
    raw = list(iter_raw_runs(paths.raw_dir))
    expected_ids, actual_ids = {job.run_id for job in plan.jobs}, {run.run_id for run in raw}
    if expected_ids != actual_ids or len(actual_ids) != len(raw):
        raise ValueError(f"Incomplete raw tree: missing={sorted(expected_ids - actual_ids)}, "
                         f"extra={sorted(actual_ids - expected_ids)}")
    replay = audit_runs(registry, paths.raw_dir)
    paths.processed_dir.mkdir(parents=True, exist_ok=True)
    (paths.processed_dir / "trace_audit.json").write_text(
        json.dumps(replay, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    scores = score_experiment(paths.raw_dir, registry=registry)
    raw_by_id = {run.run_id: run for run in raw}
    def failure_kind(row):
        return classify_failure(row, raw_by_id[row.run_id].artifact)
    scores.write_jsonl(paths.run_scores_jsonl)
    scores.write_csv(paths.run_scores_csv)
    summary = summarise(scores, plan=plan, registry=registry)
    summary.write_json(paths.summary_json)
    conditions = []
    for condition in plan.condition_ids:
        rows = [row for row in scores.rows if row.condition_id == condition]
        e1 = [row for row in rows if row.error_condition.value == "E1" and not row.is_negative_sentinel]
        sentinels = [row for row in rows if row.is_negative_sentinel]
        conditions.append({
            "condition_id": condition, "planned": len(rows),
            "completed": sum(row.completed for row in rows),
            "protocol_failed": sum(row.protocol_failure for row in rows),
            "runtime_failure_counts": dict(Counter(failure_kind(row) for row in rows if row.protocol_failure)),
            "action_correct_among_answers": Rate.of(row.final_action_correct for row in rows).model_dump(),
            "correct_action_delivered_over_planned": ratio(
                sum(row.completed and row.final_action_correct is True for row in rows), len(rows)),
            "e1_planned": len(e1), "e1_completed": sum(row.completed for row in e1),
            "e1_recovery_among_completed": Rate.of(
                row.omission_recovered_with_evidence for row in e1).model_dump(),
            "e1_recovery_delivered_over_planned": ratio(
                sum(row.omission_recovered_with_evidence is True for row in e1), len(e1)),
            "sentinel_false_escalation_among_answers": Rate.of(row.false_escalation for row in sentinels).model_dump(),
            "manager_full_validation": Rate.of(row.manager_validation_pass for row in rows).model_dump(),
            "compliance_full_validation": Rate.of(row.compliance_validation_pass for row in rows).model_dump(),
            "gate_failure_reasons": dict(Counter(f for row in rows for f in row.verification_failures)),
            "audit_issue_reasons": dict(Counter(f for row in rows for f in row.verification_audit_failures)),
        })
    a0_success = sum(call.ok for run in raw if not run.artifact.source_access for call in run.tool_calls)
    rejected_applied = [row for row in replay["nodes"]
                        if row["condition_id"] == "A1V1"
                        and row["recorded_validation_pass"] is False and row["candidate_applied"]]
    tool_counts = Counter((run.run_id, call.node, call.invocation)
                          for run in raw for call in run.tool_calls if call.ok)
    request_counts = Counter((run.run_id, call.node, call.invocation)
                             for run in raw for call in run.tool_calls)
    qc = {
        "plan_matches_raw": True, "frozen_hashes_unchanged": unchanged,
        "a0_successful_source_calls": a0_success,
        "runtime_replay_agreement": all(row["current_replay_matches_runtime"] is True for row in replay["nodes"]),
        "rejected_a1v1_candidates_applied": len(rejected_applied),
        "max_successful_tools_per_node_invocation": max(tool_counts.values(), default=0),
        "max_dispatched_tools_per_node_invocation": max(request_counts.values(), default=0),
        "max_tool_rounds": tool_budget,
        "all_run_versions_match": all(run.artifact.validator_version == manifest["validator_version"] for run in raw),
        "planned": len(plan.jobs), "attempted": len(raw),
        "completed": sum(run.artifact.completed for run in raw),
        "failed": sum(not run.artifact.completed for run in raw),
        "model_calls": sum(len(run.model_calls) for run in raw),
        "tool_calls": sum(len(run.tool_calls) for run in raw),
        "failed_runs_retried": False,
    }
    qc["protocol_invariants_pass"] = (
        a0_success == 0 and not rejected_applied and qc["runtime_replay_agreement"]
        and qc["max_dispatched_tools_per_node_invocation"] <= tool_budget and qc["all_run_versions_match"]
    )
    payload = {"experiment_id": experiment_id, "validator_version": manifest["validator_version"],
               "implementation_commit": manifest["implementation_commit"],
               "max_tool_rounds": tool_budget,
               "qc": qc, "conditions": conditions,
               "node_diagnosis_counts": replay["counts"]["diagnoses"],
               "boundary": "Purposive six-case preflight, one repeat per cell; no population or causal effect claim. "
                           "Failed runs stay in planned delivery denominators; gold overlap is an offline proxy."}
    (paths.processed_dir / "preflight_metrics.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raw_hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in sorted(paths.raw_dir.rglob("*")) if path.is_file()}
    (root / "raw_sha256.json").write_text(json.dumps(raw_hashes, indent=2) + "\n", encoding="utf-8")
    def fmt(rate):
        return f"{rate['numerator']}/{rate['denominator']}" if rate["denominator"] else "不适用 / 未到达"
    lines = [f"# Validator v2.1 预实验结果（{manifest.get('client_date', '2026-10-02')}）", "",
             f"实验 ID：`{experiment_id}`；每节点工具预算：{tool_budget} 次（搜索和打开共用）。",
             f"计划 {qc['planned']}，尝试 {qc['attempted']}，完成 {qc['completed']}，失败 {qc['failed']}。",
             f"模型调用 {qc['model_calls']}，工具调用 {qc['tool_calls']}。程序不变量检查：{qc['protocol_invariants_pass']}。", "",
             "| 条件 | 完成 / 计划 | 最终动作正确 / 已答 | 正确动作交付 / 计划 | E1 证据恢复 / 完成 | E1 证据恢复交付 / 计划 | Manager 完整核验 | Compliance 完整核验 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in conditions:
        lines.append(f"| {row['condition_id']} | {row['completed']}/{row['planned']} | "
                     f"{fmt(row['action_correct_among_answers'])} | {fmt(row['correct_action_delivered_over_planned'])} | "
                     f"{fmt(row['e1_recovery_among_completed'])} | {fmt(row['e1_recovery_delivered_over_planned'])} | "
                     f"{fmt(row['manager_full_validation'])} | {fmt(row['compliance_full_validation'])} |")
    lines += ["", "## 拦截与失败", ""]
    for row in conditions:
        lines.append(f"- {row['condition_id']} gate failure：`{json.dumps(row['gate_failure_reasons'], ensure_ascii=False)}`；"
                     f"audit 缺项：`{json.dumps(row['audit_issue_reasons'], ensure_ascii=False)}`；"
                     f"运行失败：`{json.dumps(row['runtime_failure_counts'], ensure_ascii=False)}`。")
    lines += ["", "## 每个计划运行", "",
              "| 案例 | Memo | 条件 | 完成 | 最终动作 | 最终目标状态 | 失败类型 / 核验缺项 |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in scores.rows:
        status = (row.compliance_target_clause_status or {}).get(row.target_category)
        lines.append(f"| {row.case_id} | {row.error_condition.value} | {row.condition_id} | {row.completed} | "
                     f"{row.final_decision.value if row.final_decision else '未交付'} | "
                     f"{status.value if status else '未交付'} | "
                     f"{failure_kind(row) or '无运行失败'}; {', '.join(row.verification_failures) or '无核验拦截'} |")
    lines += ["", "## 解释边界", "",
              "完整核验只证明字段协议、成功检索和打开记录符合程序规则，不证明语义结论正确。",
              "失败不是已答错误：已答正确率保留可测量分母，同时用所有计划运行报告成功交付。",
              "E1 恢复统计沿用冻结的 evidence-supported recovery 指标；段落重叠不是人工语义蕴含。",
              "这六个案例按类型与长度目的性选取，每格一次，不能据此宣称稳定的因果增益。",
              "未自动重跑失败结果；本批次运行期间配置保持冻结，预算以本轮 manifest 为准。", "",
              f"冻结实现：`{manifest['implementation_commit']}`。原始输出根目录：`{root}`。"]
    (root / "preflight_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--experiment-id", required=True)
    args = parser.parse_args()
    result = analyze(Path(args.out), args.experiment_id, Path(args.registry))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
