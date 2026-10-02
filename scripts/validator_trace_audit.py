"""Offline trace replay and retrieval diagnosis; never changes raw runs or gold."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pilot01.experiment.artifacts import iter_raw_runs
from pilot01.experiment.cases import CaseRegistry
from pilot01.schemas import ComplianceOutput, ManagerOutput
from pilot01.source import VALIDATOR_VERSION, evaluate_verification, replay_ledger


def audit_runs(registry: CaseRegistry, raw_root: Path) -> dict:
    """Gold overlap is an offline diagnostic proxy, never an agent/gate input."""
    rows = []
    runs = []
    for raw in iter_raw_runs(raw_root):
        case = registry.get(raw.artifact.case_id)
        document = registry.document(case.case_id)
        offsets = case.gold_evidence_offsets
        gold_paragraphs = {
            paragraph.paragraph_id for paragraph in document.paragraphs
            if any(paragraph.start_char < end and paragraph.end_char > start
                   for start, end in zip(offsets[::2], offsets[1::2]))
        }
        runs.append({
            "run_id": raw.run_id, "case_id": case.case_id,
            "condition_id": raw.artifact.condition_id,
            "error_condition": raw.artifact.error_condition.value,
            "completed": raw.artifact.completed,
            "validator_version": raw.artifact.validator_version,
            "audited_nodes": len(raw.verifications),
        })
        for recorded in raw.verifications:
            candidates = [call for call in raw.model_calls
                          if call.role == recorded.node and call.invocation == recorded.invocation
                          and call.parsed_output and "target_clause_status" in call.parsed_output]
            if not candidates:
                raise ValueError(f"Audit without parsed candidate: {raw.run_id}/{recorded.node}")
            candidate = candidates[-1]
            output_type = ManagerOutput if recorded.node == "manager" else ComplianceOutput
            output = output_type.model_validate(candidate.parsed_output)
            if recorded.node == "manager":
                upstream = tuple(source for claim in registry.memo(
                    case.case_id, raw.artifact.error_condition
                ).claims for source in claim.source_ids)
            else:
                manager = raw.artifact.execution.manager_output
                upstream = () if manager is None else manager.evidence_provenance.inherited_source_ids
            calls = tuple(call for call in raw.tool_calls
                          if call.call_index < candidate.call_index)
            ledger = replay_ledger(calls, node=recorded.node,
                                   invocation=recorded.invocation, upstream=upstream)
            replayed = evaluate_verification(
                output=output, policy=case.policy, ledger=ledger,
                document=document if raw.artifact.source_access else None,
                verification_required=raw.artifact.verification_required,
                source_tools_available=raw.artifact.source_access,
                node=recorded.node, invocation=recorded.invocation,
            )
            target_queries = next((set(item.matched_queries) for item in replayed.target_search_audit
                                   if item.category == case.target_category), set())
            search_calls = [call for call in calls if call.node == recorded.node
                            and call.invocation == recorded.invocation
                            and call.ok and call.tool == "search_contract"]
            returned = {pid for call in search_calls for pid in call.result_ids}
            target_returned = {pid for call in search_calls
                               if call.arguments.get("query") in target_queries
                               for pid in call.result_ids}
            opened_gold = bool(set(ledger.opened_ids) & gold_paragraphs)
            returned_gold = bool(returned & gold_paragraphs)
            status = output.target_clause_status.get(case.target_category)
            if case.is_negative_sentinel:
                diagnosis = "negative_sentinel_no_positive_gold_span"
            elif not raw.artifact.source_access:
                diagnosis = "source_unavailable"
            elif not returned_gold:
                diagnosis = "gold_overlap_not_returned"
            elif not opened_gold:
                diagnosis = "gold_overlap_returned_but_not_opened"
            elif status is not None and status.value != "present":
                diagnosis = "gold_overlap_opened_but_candidate_not_present"
            else:
                diagnosis = "candidate_present_with_gold_overlap"
            current_record = recorded.validator_version == VALIDATOR_VERSION
            if current_record and recorded.model_dump() != replayed.model_dump():
                raise ValueError(f"Runtime/replay disagreement: {raw.run_id}/{recorded.node}")
            rows.append({
                "run_id": raw.run_id, "case_id": case.case_id,
                "condition_id": raw.artifact.condition_id,
                "error_condition": raw.artifact.error_condition.value,
                "node": recorded.node, "invocation": recorded.invocation,
                "candidate_call_index": candidate.call_index,
                "candidate_target_status": None if status is None else status.value,
                "candidate_applied": getattr(raw.artifact.execution,
                                             f"{recorded.node}_output") is not None,
                "recorded_validator_version": recorded.validator_version,
                "recorded_validation_pass": recorded.validation_pass,
                "current_replay_matches_runtime": True if current_record else None,
                "replayed_outcome": replayed.model_dump(mode="json"),
                "gold_paragraph_ids_offline_only": sorted(gold_paragraphs),
                "gold_hit_in_any_search": returned_gold if gold_paragraphs else None,
                "gold_hit_in_target_named_search": bool(target_returned & gold_paragraphs)
                                                  if gold_paragraphs else None,
                "opened_gold_overlap": opened_gold if gold_paragraphs else None,
                "diagnosis": diagnosis,
            })
    return {
        "validator_version": VALIDATOR_VERSION,
        "scope": "Offline replay only; historical outcomes are not overwritten. "
                 "Gold span overlap is a retrieval proxy, not semantic entailment; "
                 "no downstream counterfactual is inferred.",
        "runs": runs, "nodes": rows,
        "counts": {"runs": len(runs), "completed": sum(run["completed"] for run in runs),
                   "evaluated_nodes": len(rows),
                   "diagnoses": dict(Counter(row["diagnosis"] for row in rows))},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    raw_root, out = Path(args.raw).resolve(), Path(args.out).resolve()
    if out == raw_root or raw_root in out.parents:
        raise SystemExit("Sidecar output must be outside the raw tree")
    payload = audit_runs(CaseRegistry.load_json(args.registry), raw_root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
