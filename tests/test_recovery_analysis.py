import json

import pytest

from pilot01.experiment.recovery_analysis import (
    analyze_recovery, paired_effect, process_metrics, write_analysis,
)
from test_validator_disclosure import execute


def call(tool, ids=(), *, ok=True, **arguments):
    return {"tool": tool, "arguments": arguments, "ok": ok,
            "result_ids": list(ids), "latency_seconds": 0.2}


def test_pagination_and_failed_repeated_queries_are_counted_separately():
    calls = [
        call("search_contract", ("background",), query="Control", target_category="COC"),
        call("search_contract", ok=False, query=" control ", target_category="COC"),
        call("next_search_page", ("gold",), cursor="opaque"),
        call("open_source_span", ("gold",), paragraph_id="gold"),
        call("search_contract", query="ownership", target_category="COC"),
    ]
    result = process_metrics(calls, gold_ids={"gold"}, relied_ids=("gold", "invented"), has_output=True)
    assert result["search_calls"] == 3
    assert result["page_calls"] == 1
    assert result["failed_search_calls"] == 1
    assert result["repeated_search_calls"] == 1
    assert result["additional_distinct_queries_within_target"] == 1
    assert result["gold_returned"] is True
    assert result["gold_opened_and_adopted_proxy"] is True
    assert result["reported_relied_without_open"] == 1
    assert result["first_gold_open_call"] == 4
    assert result["calls_after_first_gold_open"] == 1


def test_missing_output_or_latency_is_not_zero_or_false():
    result = process_metrics([{"tool": "open_source_span", "ok": True, "result_ids": ["gold"]}],
                             gold_ids={"gold"})
    assert result["gold_opened"] is True
    assert result["gold_opened_and_adopted_proxy"] is None
    assert result["distinct_reported_relied_paragraphs"] is None
    assert result["tool_latency_seconds"] is None
    assert result["tool_latency_missing_calls"] == 1
    sentinel = process_metrics([], gold_ids=set(), applicable=False)
    assert sentinel["gold_opened"] is None


def paired_rows():
    rows = []
    for case, contract, treatment, control in (
        ("A", "C1", True, False), ("B", "C1", True, False),
        ("C", "C2", False, True),
    ):
        for condition, result in (("A1V2D0", treatment), ("A1V1D0", control)):
            rows.append({"case_id": case, "contract_id": contract, "repeat_index": 0,
                         "error_condition": "E1", "condition_id": condition,
                         "execution_status": "completed", "final_recovered_delivered": result})
    return rows


def test_bootstrap_resamples_contracts_together_and_preserves_pairing():
    rows = paired_rows()
    result = paired_effect(rows, "A1V2D0", "A1V1D0", bootstrap_samples=400)
    assert result["contracts"] == 2
    assert result["pairs"] == 3
    assert result["risk_difference"] == pytest.approx(1 / 3)
    assert result["gains"] == 2
    assert result["losses"] == 1
    assert result["contract_bootstrap_95_interval"] == [-1, 1]
    assert paired_effect(list(reversed(rows)), "A1V2D0", "A1V1D0", bootstrap_samples=400) == result


def test_missing_or_duplicate_pair_refused_and_failed_run_stays_in_primary():
    rows = paired_rows()
    with pytest.raises(ValueError, match="missing counterpart"):
        paired_effect(rows[:-1], "A1V2D0", "A1V1D0")
    with pytest.raises(ValueError, match="duplicate"):
        paired_effect(rows + [rows[0]], "A1V2D0", "A1V1D0")
    rows[0]["execution_status"] = "failed"
    rows[0]["final_recovered_delivered"] = False
    primary = paired_effect(rows, "A1V2D0", "A1V1D0")
    completed = paired_effect(rows, "A1V2D0", "A1V1D0", completed_only=True)
    assert primary["pairs"] == 3
    assert primary["risk_difference"] == 0
    assert completed["pairs"] == 2
    assert completed["excluded_incomplete_pairs"] == 1


def test_end_to_end_analysis_uses_real_artifacts_and_exports(tmp_path):
    plan, registry, paths, batch = execute(tmp_path)
    output = tmp_path / "analysis"
    summary = write_analysis(plan, registry, paths.raw_dir, output, bootstrap_samples=30)
    assert summary["batch_complete"] is True
    assert summary["by_condition"]["D0"]["e1_recovery_per_planned"] == {
        "numerator": 1, "denominator": 1, "rate": 1.0}
    rows = [json.loads(line) for line in (output / "recovery_runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == len(plan.jobs)
    assert all(r["manager_search_calls"] == 2 for r in rows)
    assert all(r["manager_open_calls"] == 1 for r in rows)
    assert all(r["manager_gold_opened_and_adopted_proxy"] is True for r in rows)
    assert (output / "recovery_runs.csv").is_file()
    assert (output / "recovery_analysis.md").is_file()


def test_interrupted_run_keeps_observed_calls_and_missing_final_outcome(tmp_path):
    plan, registry, paths, batch = execute(tmp_path)
    job = next(j for j in plan.jobs if j.error_condition.value == "E1")
    directory = paths.run_dir(job.run_id)
    (directory / "run.json").unlink()
    with (directory / "tool_calls.jsonl").open("a", encoding="utf-8") as stream:
        stream.write('{"incomplete":\n')
    rows, summary = analyze_recovery(plan, registry, paths.raw_dir, bootstrap_samples=0)
    row = next(r for r in rows if r["run_id"] == job.run_id)
    assert row["execution_status"] == "interrupted"
    assert row["manager_search_calls"] == 2
    assert row["malformed_tool_log_lines"] == 1
    assert row["manager_gold_opened_and_adopted_proxy"] is None
    assert row["final_recovered_delivered"] is False
    assert summary["batch_complete"] is False
    assert summary["by_condition"][job.condition_id]["e1_recovery_per_planned"]["denominator"] == 1


def test_analysis_refuses_registry_identity_drift(tmp_path):
    plan, registry, paths, batch = execute(tmp_path)
    changed_plan = plan.model_copy(update={"registry_fingerprint": "sha256:" + "0" * 64})
    with pytest.raises(ValueError, match="registry differs"):
        analyze_recovery(changed_plan, registry, paths.raw_dir)


def test_unfinished_batch_has_no_inferential_interval():
    rows = paired_rows()
    rows[0]["execution_status"] = "interrupted"
    rows[0]["final_recovered_delivered"] = False
    result = paired_effect(rows, "A1V2D0", "A1V1D0")
    assert result["pairs"] == 3
    assert result["all_selected_runs_terminal"] is False
    assert result["contract_bootstrap_95_interval"] is None


@pytest.mark.parametrize("field,value,execution_field", [
    ("condition_id", "WRONG-CONDITION", "condition_id"),
    ("error_condition", "E1", "error_condition"),
    ("repeat_index", 99, "repetition_id"),
    ("contract_id", "WRONG-CONTRACT", None),
    ("source_access", False, "source_access"),
    ("verification_required", False, "verification_required"),
    ("recovery_required", True, "recovery_required"),
    ("models_version", "WRONG-MODEL-VERSION", None),
])
def test_self_consistent_artifact_with_wrong_planned_identity_is_refused(
        tmp_path, field, value, execution_field):
    plan, registry, paths, batch = execute(tmp_path)
    job = next(j for j in plan.jobs if j.error_condition.value == "E0")
    path = paths.run_dir(job.run_id) / "run.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = value
    if execution_field is not None:
        payload["execution"][execution_field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match=field + " differs from the planned job"):
        analyze_recovery(plan, registry, paths.raw_dir)


@pytest.mark.parametrize("log_name", ["tool_calls.jsonl", "model_calls.jsonl"])
def test_interrupted_log_from_another_run_is_refused(tmp_path, log_name):
    plan, registry, paths, batch = execute(tmp_path)
    job = plan.jobs[0]
    directory = paths.run_dir(job.run_id)
    (directory / "run.json").unlink()
    path = directory / log_name
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    records[0]["run_id"] = "OTHER-RUN"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    with pytest.raises(ValueError, match="log record run_id"):
        analyze_recovery(plan, registry, paths.raw_dir)
