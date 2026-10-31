"""Plan-denominator descriptive metrics. No semantic judge and no gold in tools."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from .artifacts import read_raw_run
from ..schemas import expected_decision


def account_usage(row, calls, pricing=None):
    from .costs import total_cost
    row["official_reference_cost"] = total_cost(calls)
    row["model_calls"] = len(calls)
    row["usage_missing_calls"] = sum(c.get("usage") is None for c in calls)
    row["reasoning_usage_missing_calls"] = sum((c.get("usage") or {}).get("reasoning_tokens") is None for c in calls)
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"):
        values = [(c.get("usage") or {}).get(key) for c in calls]
        row[key] = sum(values) if calls and all(v is not None for v in values) else None
        row["known_" + key] = sum(v for v in values if v is not None)
    row["known_model_latency_seconds"] = sum(c.get("latency_seconds") or 0 for c in calls)
    if any(c.get("reasoning_observation") is not None for c in calls):
        observations = [c.get("reasoning_observation") or {} for c in calls]
        row["reasoning_observation"] = {
            "calls": len(calls),
            "content_recorded_calls": sum(c.get("reasoning_content") is not None for c in calls),
            "token_sources": dict(Counter(o.get("token_source") or "missing" for o in observations)),
            "timing_sources": dict(Counter(o.get("timing_source") or "missing" for o in observations)),
        }
        for key in ("tokens", "reasoning_phase_seconds"):
            values = [o.get(key) for o in observations]
            row["reasoning_observation"][key] = sum(values) if all(v is not None for v in values) else None
            row["reasoning_observation"]["known_" + key] = sum(v for v in values if v is not None)
            row["reasoning_observation"][key + "_missing_calls"] = sum(v is None for v in values)
    costs = [pricing.estimate(model_id=c.get("response_model_id") or c["params"]["model_id"],
                             prompt_tokens=(c.get("usage") or {}).get("prompt_tokens"),
                             completion_tokens=(c.get("usage") or {}).get("completion_tokens")) for c in calls] if pricing else []
    row["estimated_flat_rate_cost"] = sum(costs) if costs and all(c is not None for c in costs) else None


def failure_category(raw):
    if not raw.artifact.failed:
        return None
    error = raw.artifact.failure.error if raw.artifact.failure else ""
    error = (error or "").lower()
    if any(c.finish_reason == "length" for c in raw.model_calls):
        return "output_truncated"
    if "requested more than" in error:
        return "tool_budget_exceeded"
    if any("timed out" in (c.error or "").lower() or "timeout" in (c.error or "").lower() for c in raw.model_calls):
        return "transport_timeout"
    if any(c.error for c in raw.model_calls):
        return "transport_error"
    if "did not meet the verification requirement" in error:
        return "validator_rejected"
    if raw.artifact.failed:
        return "schema_or_protocol_invalid"
    return None


def analyze_plan(plan, registry, raw_root: Path, pricing=None):
    rows = []
    expected = set(plan.run_ids)
    extras = sorted(p.parent.name for p in raw_root.glob("*/run.json") if p.parent.name not in expected)
    if extras:
        raise ValueError("raw tree contains runs outside the frozen plan")
    for job in plan.jobs:
        directory = raw_root / job.run_id
        case = registry.get(job.case_id)
        row = {"run_id": job.run_id, "case_id": job.case_id, "contract_id": job.contract_id,
               "condition_id": job.condition_id, "error_condition": job.error_condition.value,
               "repeat_index": job.repeat_index, "execution_status": "not_started",
               "final_action": None, "final_action_correct": None, "correct_action_delivered": False,
               "final_fact_recovered": None, "final_recovered_delivered": False, "failure_category": None}
        row.update(gold_action=case.gold_action.value, final_false_accept=False,
                   final_false_escalate=False, final_review=False, final_policy_consistent=None)
        if not (directory / "run.json").exists():
            if any(p.is_file() and p.stat().st_size for p in directory.glob("*.jsonl")):
                row["execution_status"] = "interrupted"
                calls = []
                malformed = 0
                path = directory / "model_calls.jsonl"
                if path.exists():
                    with path.open(encoding="utf-8") as f:
                        for line in f:
                            if line.strip():
                                try:
                                    calls.append(json.loads(line))
                                except json.JSONDecodeError:
                                    malformed += 1
                account_usage(row, calls, pricing)
                row["partial_log_malformed_lines"] = malformed
            rows.append(row)
            continue
        raw = read_raw_run(directory)
        if raw.artifact.plan_fingerprint != plan.fingerprint():
            raise ValueError("raw run belongs to a different plan")
        completed = not raw.artifact.failed and raw.artifact.execution.status.value == "completed"
        row["execution_status"] = "completed" if completed else "failed"
        row["failure_category"] = failure_category(raw)
        doc = registry.document(job.case_id)
        gold_pids = {p.paragraph_id for p in doc.paragraphs if any(a < p.end_char and p.start_char < b for a, b in case.gold_spans())}
        recovered = {}
        for node in ("manager", "compliance"):
            verdicts = raw.verifications_of(node)
            row[node + "_validation_pass"] = verdicts[-1].validation_pass if verdicts else None
            row[node + "_audit_failures"] = [f.value for f in verdicts[-1].audit_failures] if verdicts else []
            output = getattr(raw.artifact.execution, node + "_output")
            status = output.target_clause_status.get(case.target_category) if output else None
            row[node + "_target_status"] = status.value if status else None
            row[node + "_target_unknown"] = status.value == "unknown" if status else None
            row[node + "_incorrect_absent"] = (status.value == "absent" and case.gold_status_for(case.target_category).value == "present") if status else None
            recovered[node] = status.value == "present" if status and not case.is_negative_sentinel else None
            row[node + "_fact_recovered"] = recovered[node]
            calls = raw.tool_calls_of(node)
            opened = {pid for c in calls if c.ok and c.tool == "open_source_span" for pid in c.result_ids}
            returned = {pid for c in calls if c.ok and c.tool == "search_contract" for pid in c.result_ids}
            relied = set(output.evidence_provenance.relied_paragraph_ids or ()) if output else set()
            row[node + "_opened_gold_overlap_proxy"] = bool(opened & gold_pids & relied) if output and not case.is_negative_sentinel else None
            row[node + "_recovered_with_open_overlap_proxy"] = bool(recovered[node] and row[node + "_opened_gold_overlap_proxy"]) if recovered[node] is not None else None
            row[node + "_inherited_ids"] = list(output.evidence_provenance.inherited_source_ids) if output else []
            row[node + "_self_opened_ids"] = sorted(opened)
            row[node + "_tool_requests"] = len(calls)
            row[node + "_diagnostic"] = (None if case.is_negative_sentinel else
                "opened_but_unrecovered" if opened & gold_pids and not recovered[node] else
                "recovered_with_open_overlap_proxy" if row[node + "_recovered_with_open_overlap_proxy"] else
                "returned_not_opened" if returned & gold_pids and not opened & gold_pids else
                "target_not_returned_or_opened" if not (returned | opened) & gold_pids else "other")
            candidates = [c.parsed_output for c in raw.model_calls_of(node) if c.parsed_output]
            row[node + "_last_candidate"] = candidates[-1] if candidates else None
            row[node + "_last_candidate_action_correct"] = candidates[-1].get("decision") == case.gold_action.value if candidates else None
        final = raw.artifact.execution.compliance_output if completed else None
        if final:
            row["final_action"] = final.decision.value
            row["final_action_correct"] = final.decision == case.gold_action
            row["correct_action_delivered"] = row["final_action_correct"]
            row["final_fact_recovered"] = recovered["compliance"]
            row["final_recovered_delivered"] = recovered["compliance"] is True
            row["final_false_accept"] = final.decision.value == "ACCEPT" and case.gold_action.value != "ACCEPT"
            row["final_false_escalate"] = final.decision.value == "ESCALATE" and case.gold_action.value == "ACCEPT"
            row["final_review"] = final.decision.value == "REVIEW"
            row["final_policy_consistent"] = final.decision == expected_decision(final.target_clause_status, case.policy)
        row["compliance_new_recovery"] = recovered.get("compliance") is True and recovered.get("manager") is not True
        row["any_node_recovered"] = any(v is True for v in recovered.values()) if not case.is_negative_sentinel else None
        account_usage(row, [c.model_dump(mode="json") for c in raw.model_calls], pricing)
        rows.append(row)
    def aggregate(group):
        n, done = len(group), sum(r["execution_status"] == "completed" for r in group)
        correct = sum(r["correct_action_delivered"] for r in group)
        e1 = [r for r in group if r["error_condition"] == "E1"]
        e1_done = [r for r in e1 if r["execution_status"] == "completed"]
        facts = sum(r["final_recovered_delivered"] for r in e1)
        return {"planned": n, "execution_statuses": dict(Counter(r["execution_status"] for r in group)),
                "correct_actions_delivered": correct, "correct_delivery_per_planned": correct / n if n else None,
                "action_accuracy_among_completed": correct / done if done else None,
                "final_actions": dict(Counter(r["final_action"] for r in group if r["final_action"])),
                "false_accept_delivered": sum(r["final_false_accept"] for r in group),
                "false_escalate_delivered": sum(r["final_false_escalate"] for r in group),
                "reviews_delivered": sum(r["final_review"] for r in group),
                "e1_final_unknown": sum(r.get("compliance_target_unknown") is True for r in e1_done),
                "e1_final_incorrect_absent": sum(r.get("compliance_incorrect_absent") is True for r in e1_done),
                "e1_planned": len(e1), "e1_completed": len(e1_done), "e1_final_fact_delivered": facts,
                "e1_fact_delivery_per_planned": facts / len(e1) if e1 else None,
                "e1_fact_recovery_among_completed": facts / len(e1_done) if e1_done else None,
                "failures": dict(Counter(r["failure_category"] for r in group if r["failure_category"])),
                "validation_by_node": {node: {
                    "evaluated": sum(r.get(node + "_validation_pass") is not None for r in group),
                    "passed": sum(r.get(node + "_validation_pass") is True for r in group),
                    "failed": sum(r.get(node + "_validation_pass") is False for r in group),
                } for node in ("manager", "compliance")},
                "known_total_tokens": sum(r.get("known_total_tokens", 0) for r in group),
                "runs_with_complete_token_usage": sum(r.get("total_tokens") is not None for r in group),
                "known_partial_cost_sum": sum(r["estimated_flat_rate_cost"] for r in group if r.get("estimated_flat_rate_cost") is not None),
                "runs_with_cost_estimate": sum(r.get("estimated_flat_rate_cost") is not None for r in group),
                "mean_total_tokens_when_reported": sum(r["total_tokens"] for r in group if r.get("total_tokens") is not None) / sum(r.get("total_tokens") is not None for r in group) if any(r.get("total_tokens") is not None for r in group) else None}
    summary = {"scorer_version": "2", "plan_fingerprint": plan.fingerprint(), "overall": aggregate(rows),
               "batch_complete": all(r["execution_status"] in ("completed", "failed") for r in rows),
               "note": "Partial batches are progress snapshots. Evidence overlap is a procedural proxy, not semantic support. Descriptive statistics only; runs are not independent contracts.",
               "by_condition": {c: aggregate([r for r in rows if r["condition_id"] == c]) for c in plan.condition_ids},
               "by_contract_condition": {f"{contract}/{c}": aggregate([r for r in rows if r["contract_id"] == contract and r["condition_id"] == c])
                                         for contract in sorted({r["contract_id"] for r in rows}) for c in plan.condition_ids}}
    return rows, summary


def write_report(plan, registry, raw_root, out, pricing=None):
    rows, summary = analyze_plan(plan, registry, Path(raw_root), pricing)
    summary["pricing"] = pricing.model_dump(mode="json") if pricing else None
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "run_scores_v2.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    (out / "experiment_summary_v2.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary
