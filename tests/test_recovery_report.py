"""Guard reporting estimands and incomplete-batch presentation, offline only."""
from collections import Counter
import csv
import importlib.util
import json
from pathlib import Path
import sys

import pytest


spec = importlib.util.spec_from_file_location("report_recovery_study", Path(__file__).parents[1] / "scripts/report_recovery_study.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def study_rows():
    rows = []
    for case in ("CASE-0004", "CASE-0044", "CASE-0052", "CASE-0071", "CASE-0012"):
        negative = case != "CASE-0004"
        for repeat in range(2):
            for error in (("E0",) if negative else ("E0", "E1")):
                for condition in report.CONDITIONS:
                    recovered = not negative and (error == "E0" or condition == "A1V2D0" or repeat == 1)
                    row = {"experiment_id": "synthetic", "case_id": case, "contract_id": "contract-" + case, "repeat_index": repeat,
                           "error_condition": error, "condition_id": condition, "is_negative_sentinel": negative,
                           "run_id": f"{case}-{repeat}-{error}-{condition}", "execution_status": "completed",
                           "target_category": "COC", "final_recovered_delivered": recovered,
                           "final_recovery_with_open_overlap_proxy": recovered, "correct_action_delivered": True,
                           "final_any_false_positive_delivered": negative and case in report.SENSITIVITY_CASES and condition == "A1V2D0",
                           "total_tokens": 120 if condition == "A1V2D0" else 100, "known_total_tokens": 120 if condition == "A1V2D0" else 100,
                           "model_latency_seconds": 4 if condition == "A1V2D0" else 2,
                           "known_model_latency_seconds": 4 if condition == "A1V2D0" else 2, "model_calls": 3,
                           "compliance_target_status": "present" if recovered else "absent"}
                    for role in ("manager", "compliance"):
                        row.update({role + "_" + name: 1 for name in report.ROLE_FIELDS})
                        row.update({role + "_" + name: recovered for name in (
                            "gold_returned", "gold_opened", "gold_opened_and_adopted_proxy", "fact_recovered")})
                    rows.append(row)
    return rows


def frozen_plan(rows):
    return {"plan_version": "1", "experiment_version": "4", "experiment_id": "synthetic", "seed": 1109,
            "repeat_count": 2, "condition_ids": list(report.CONDITIONS), "case_ids": sorted({r["case_id"] for r in rows}),
            "models_version": "offline", "model_fingerprint": "offline-test", "jobs": rows}


def snapshot(tmp_path, rows, *, batch_complete=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    summary = {"plan_fingerprint": report.plan_fingerprint(frozen_plan(rows)), "batch_complete": all(r["execution_status"] in report.TERMINAL for r in rows)
               if batch_complete is None else batch_complete,
               "by_condition": {c: {"planned": sum(r["condition_id"] == c for r in rows),
                    "execution_statuses": dict(Counter(r["execution_status"] for r in rows if r["condition_id"] == c))}
                    for c in report.CONDITIONS}, "paired_comparisons": []}
    (tmp_path / "recovery_runs.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (tmp_path / "recovery_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return tmp_path


def test_entire_batch_must_finish_even_when_all_e1_pairs_are_complete(tmp_path):
    rows = study_rows()
    pending = next(r for r in rows if r["error_condition"] == "E0" and r["is_negative_sentinel"])
    pending.update(execution_status="not_started", correct_action_delivered=False, final_any_false_positive_delivered=False)
    phase = report.load_phase(snapshot(tmp_path, rows), "medium", sensitivity=True)
    assert phase["ready"] is False
    for effect in (phase["effects"], phase["sensitivity"]["effects"]):
        assert effect["primary"] is None
        assert effect["completed_pairs_secondary"] is None
        assert effect["process_pairs"] == []
    assert all(r["v2_minus_v1"] is None for r in phase["contract_e1_results"])
    html = report.phase_html(phase)
    assert "整批终态后展示" in html
    assert "已观察" in html and "未终态/部分日志" in html
    assert "（100.0%）" not in html and "（50.0%）" not in html


def test_failed_delivery_stays_in_primary_but_reduces_shared_completed_pair_set(tmp_path):
    rows = study_rows()
    failed = next(r for r in rows if r["condition_id"] == "A1V2D0" and r["error_condition"] == "E1" and r["repeat_index"] == 1)
    failed.update(execution_status="failed", final_recovered_delivered=False,
                  final_recovery_with_open_overlap_proxy=False, correct_action_delivered=False)
    phase = report.load_phase(snapshot(tmp_path, rows), "medium")
    primary = phase["effects"]["primary"]
    selected = phase["effects"]["completed_pairs_secondary"]
    assert primary["pairs"] == 2 and primary["risk_difference"] == 0
    assert primary["gains"] == primary["losses"] == 1
    assert selected["pairs"] == 1 and selected["planned_pairs"] == 2
    assert selected["excluded_incomplete_pairs"] == 1 and selected["risk_difference"] == 1
    assert phase["conditions"]["A1V1D0"]["e1_recovery_among_completed"] == {"numerator": 1, "denominator": 2}
    assert phase["conditions"]["A1V2D0"]["e1_recovery_among_completed"] == {"numerator": 1, "denominator": 1}


def test_numeric_pairing_uses_values_and_reports_missing_not_zero(tmp_path):
    rows = study_rows()
    missing = next(r for r in rows if r["condition_id"] == "A1V2D0" and r["error_condition"] == "E1" and r["repeat_index"] == 1)
    missing["total_tokens"] = None
    missing["model_latency_seconds"] = None
    phase = report.load_phase(snapshot(tmp_path, rows), "medium")
    results = {r["field"]: r for r in phase["effects"]["process_pairs"]
               if r["treatment"] == "A1V2D0" and r["control"] == "A1V1D0" and r["error_condition"] == "E1"}
    assert results["total_tokens"]["mean_paired_difference"] == 20
    assert results["model_latency_seconds"]["mean_paired_difference"] == 2
    assert results["total_tokens"]["available_pairs"] == results["total_tokens"]["missing_pairs"] == 1
    assert results["manager_tool_calls"]["mean_paired_difference"] == 0


def test_fixed_negative_sensitivity_keeps_e1_and_removes_all_contract_cells(tmp_path):
    rows = study_rows()
    # A second case for the same negative contract must also be removed.
    alias = []
    for row in rows:
        if row["case_id"] == "CASE-0044":
            alias.append(dict(row, case_id="CASE-ALIAS", run_id=row["run_id"] + "-alias"))
    rows.extend(alias)
    phase = report.load_phase(snapshot(tmp_path, rows), "medium", sensitivity=True)
    before, after = phase["conditions"], phase["sensitivity"]["conditions"]
    assert phase["sensitivity"]["excluded_runs"] == 18
    assert phase["planned"] == 42 and phase["sensitivity"]["retained_runs"] == 24
    for c in report.CONDITIONS:
        assert before[c]["e1_recovery_per_planned"] == after[c]["e1_recovery_per_planned"]
        assert before[c]["negative_false_positive"]["denominator"] == 10
        assert after[c]["negative_false_positive"]["denominator"] == 4
    assert before["A1V2D0"]["negative_false_positive"]["numerator"] == 6
    assert after["A1V2D0"]["negative_false_positive"]["numerator"] == 0
    assert phase["effects"]["primary"] == phase["sensitivity"]["effects"]["primary"]


def test_frozen_plan_prevents_silently_dropping_a_whole_pair(tmp_path):
    rows = study_rows()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(frozen_plan(rows)), encoding="utf-8")
    subset = [r for r in rows if not (r["error_condition"] == "E1" and r["repeat_index"] == 1)]
    with pytest.raises(ValueError, match="cover the frozen plan"):
        report.load_phase(snapshot(tmp_path / "analysis", subset), "medium", plan)


def test_jsonl_false_must_be_a_boolean_and_csv_false_is_preserved(tmp_path):
    rows = study_rows()
    rows[0]["final_recovered_delivered"] = "False"
    with pytest.raises(ValueError, match="must be boolean"):
        report.load_phase(snapshot(tmp_path, rows), "medium")
    (tmp_path / "recovery_runs.jsonl").unlink()
    with (tmp_path / "recovery_runs.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=["outcome", "tokens", "missing"])
        writer.writeheader()
        writer.writerow({"outcome": False, "tokens": 12, "missing": ""})
    loaded, _ = report.load_rows(tmp_path)
    assert loaded == [{"outcome": False, "tokens": 12, "missing": None}]


def test_jsonl_keeps_unicode_line_separators_inside_valid_json_strings(tmp_path):
    (tmp_path / "recovery_runs.jsonl").write_text(json.dumps({"reason": "first\u2028second\u2029third"}, ensure_ascii=False) + "\n", encoding="utf-8")
    rows, _ = report.load_rows(tmp_path)
    assert rows == [{"reason": "first\u2028second\u2029third"}]


def test_local_reasoning_observation_is_separate_from_missing_server_usage(tmp_path):
    rows = study_rows()
    row = rows[0]
    row.update(reasoning_tokens=None, known_reasoning_tokens=0,
               reasoning_observation={"tokens": 25, "known_tokens": 25, "tokens_missing_calls": 0,
                                      "token_sources": {"local_tokenizer": 3}})
    phase = report.load_phase(snapshot(tmp_path, rows), "medium")
    condition = phase["conditions"][row["condition_id"]]
    assert condition["reasoning_tokens"]["available"] == 0
    assert condition["known_observed_reasoning_tokens"]["known_sum"] == 25
    assert condition["observed_reasoning_tokens"]["available"] == 1
    assert condition["reasoning_token_sources"] == {"local_tokenizer": 3}
    assert condition["model_calls_without_reasoning_observation"] == 33


def test_summary_status_must_match_rows_and_missing_snapshot_is_not_complete(tmp_path):
    rows = study_rows()
    directory = snapshot(tmp_path, rows)
    summary = report.read_json(directory / "recovery_summary.json")
    summary["by_condition"]["A1V2D0"]["execution_statuses"] = {"not_started": 12}
    (directory / "recovery_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(ValueError, match="execution statuses differ"):
        report.load_phase(directory, "medium")
    unavailable = report.load_phase(tmp_path / "missing", "medium")
    assert unavailable["ready"] is False and unavailable["effects"]["primary"] is None


def test_cli_exports_complete_and_pending_phases_separately(tmp_path, monkeypatch):
    rows = study_rows()
    small = snapshot(tmp_path / "small", rows)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(frozen_plan(rows)), encoding="utf-8")
    output = tmp_path / "report"
    monkeypatch.setattr(sys, "argv", ["report_recovery_study.py", "--small", str(small),
        "--medium", str(tmp_path / "pending"), "--small-plan", str(plan), "--medium-plan", str(plan), "--output", str(output)])
    report.main()
    exported = report.read_json(output / "recovery_study_report.json")
    assert exported["small"]["effects"]["primary"]["risk_difference"] == .5
    assert exported["medium"]["effects"]["primary"] is None
    html = (output / "recovery_study_report.html").read_text(encoding="utf-8")
    assert "50.00 个百分点" in html and "尚无离线分析快照" in html
    assert "语义复核附件" in html and "两臂均完成" in html
    assert "E1 流程描述" in html and "主要效果估计使用 medium" in html


@pytest.mark.parametrize("change", ["order", "runtime"])
def test_same_job_set_with_different_frozen_plan_identity_is_refused(tmp_path, change):
    rows = study_rows()
    plan = frozen_plan(rows)
    if change == "order":
        plan["jobs"] = list(reversed(rows))
    else:
        plan["runtime_config"] = {"max_tool_requests_per_node": 99}
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint differs"):
        report.load_phase(snapshot(tmp_path / "analysis", rows), "medium", plan_path)


def test_stdlib_fingerprint_matches_owning_frozen_plan_serialization():
    from pilot01.experiment.runspec import RunPlan

    path = Path(__file__).parents[1] / "outputs/recovery_v4/manifests/r4m2-1109/run_plan.json"
    if not path.exists():
        pytest.skip("local frozen study manifest is not distributed with source-only checkouts")
    assert report.plan_fingerprint(report.read_json(path)) == RunPlan.load_json(path).fingerprint()
