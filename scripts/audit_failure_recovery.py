"""Audit original selected identities against complete, linked recovery runs."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from pilot01.schemas import ComplianceOutputV2, ManagerOutputV2


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def file_hash(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_lines(path):
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


IDENTITY = ("case_id", "contract_id", "condition_id", "error_condition", "repeat_index",
            "source_access", "verification_required")


def authorized_format_repair(record, following, execution, runtime):
    """Recognize the existing one-shot format repair, without hiding its first error."""
    if (record.get("error") or not record.get("parse_error") or record.get("is_tool_turn")
            or record.get("is_repair") or record.get("attempt") != 0
            or str(record["parse_error"]).startswith("tool_protocol_violation:")
            or record.get("finish_reason") == "length"):
        return False
    if (not following or not following.get("is_repair") or following.get("attempt") != 1
            or following.get("role") != record.get("role")
            or following.get("invocation") != record.get("invocation")
            or following.get("repair_reason") != record.get("parse_error")
            or following.get("error") or following.get("parse_error")
            or following.get("finish_reason") == "length"):
        return False
    limit = runtime.get("repair_max_output_tokens")
    if limit is not None and following.get("params", {}).get("max_output_tokens", limit + 1) > limit:
        return False
    schema = {"manager": ManagerOutputV2, "compliance": ComplianceOutputV2}.get(record.get("role"))
    if schema is None:
        return False
    try:
        repaired = schema.model_validate(following.get("parsed_output"))
        final = schema.model_validate(execution.get(record["role"] + "_output"))
    except Exception:
        return False
    return repaired.model_dump(mode="json") == final.model_dump(mode="json")


def audit_delivery(directory, expected):
    issues = []
    if not (directory / "run.json").is_file():
        return {"valid": False, "issues": ["missing_terminal_run_json"]}
    artifact = read(directory / "run.json")
    execution = artifact.get("execution") or {}
    if artifact.get("run_id") != directory.name or artifact.get("experiment_id") != directory.parent.name:
        issues.append("artifact_path_identity_mismatch")
    for key in IDENTITY:
        if artifact.get(key) != expected.get(key):
            issues.append("identity_mismatch:" + key)
    if execution.get("status") != "completed":
        issues.append("status:" + str(execution.get("status")))
    if execution.get("final_node") != "end":
        issues.append("workflow_did_not_reach_end")
    for role, schema in (("manager", ManagerOutputV2), ("compliance", ComplianceOutputV2)):
        try:
            schema.model_validate(execution.get(role + "_output"))
        except Exception as exc:
            issues.append(f"invalid_or_missing_{role}_output:{type(exc).__name__}")
    logs = {}
    for name in ("model_calls", "tool_calls", "verification", "events"):
        path = directory / (name + ".jsonl")
        if not path.is_file():
            issues.append("missing_log:" + name)
        else:
            logs[name] = load_lines(path)
    calls, tools = logs.get("model_calls", []), logs.get("tool_calls", [])
    if len(calls) != artifact.get("model_calls"):
        issues.append("model_call_count_mismatch")
    if len(tools) != artifact.get("tool_calls"):
        issues.append("tool_call_count_mismatch")
    budget = (artifact.get("runtime_config") or {}).get("max_tool_requests_per_node", 16)
    for role, count in Counter(t.get("node") for t in tools).items():
        if count > budget:
            issues.append(f"tool_budget_exceeded:{role}:{count}")
    if not expected["source_access"] and any(t.get("ok") or t.get("result_ids") for t in tools):
        issues.append("successful_source_access_in_A0")
    allowed_repairs = []
    for index, record in enumerate(calls):
        choice = (((record.get("provider_response") or {}).get("choices")) or [{}])[0]
        native_calls = (choice.get("message") or {}).get("tool_calls") or []
        if len(native_calls) > 1:
            issues.append("multiple_native_calls:" + str(record.get("model_call_id")))
        if (record.get("finish_reason") or choice.get("finish_reason")) == "length":
            issues.append("truncated_model_response:" + str(record.get("model_call_id")))
        following = calls[index + 1] if index + 1 < len(calls) else None
        repaired = authorized_format_repair(record, following, execution, artifact.get("runtime_config") or {})
        if repaired:
            allowed_repairs.append({"initial_model_call_id": record.get("model_call_id"),
                                    "repair_model_call_id": following.get("model_call_id"),
                                    "role": record.get("role"), "invocation": record.get("invocation")})
        if record.get("error") or (record.get("parse_error") and not repaired):
            issues.append("model_call_error:" + str(record.get("model_call_id")))
    for identity, count in Counter((r["role"], r["invocation"]) for r in allowed_repairs).items():
        if count > 1:
            issues.append("more_than_one_format_repair:" + str(identity))
    for role in ("manager", "compliance"):
        if not any(e.get("node") == role and e.get("event_type") == "node_completed"
                   for e in logs.get("events", [])):
            issues.append("missing_node_completion_event:" + role)
    if not any(e.get("event_type") == "run_completed" for e in logs.get("events", [])):
        issues.append("missing_run_completion_event")
    return {"valid": not issues, "issues": issues, "artifact_sha256": file_hash(directory / "run.json"),
            "model_calls": len(calls), "tool_calls": len(tools),
            "authorized_format_repairs": allowed_repairs,
            "denied_or_failed_tools": sum(not t.get("ok") for t in tools)}


def audit(base, watch, batch, expected=96, recoveries=None):
    manifest = base / "manifests" / batch
    selection = read(manifest / "launch_selection.json")["run_ids"]
    plan = read(manifest / "run_plan.json")
    jobs = {job["run_id"]: job for job in plan["jobs"]}
    if len(selection) != expected or len(set(selection)) != expected:
        raise ValueError("selected identities do not match the expected count")
    if set(selection) - jobs.keys():
        raise ValueError("selected identity missing from original frozen plan")
    recoveries = recoveries or {}
    rows, terminal, original_failed = [], 0, 0
    for run_id in selection:
        original = base / "raw" / batch / run_id
        marker = original / "run.json"
        artifact = read(marker) if marker.is_file() else {}
        status = (artifact.get("execution") or {}).get("status")
        terminal += status in ("completed", "failed")
        original_failed += status == "failed"
        link = recoveries.get(run_id)
        current = original
        if link:
            current = base / "raw" / link["experiment_id"] / link["run_id"]
        result = audit_delivery(current, jobs[run_id])
        if status == "failed":
            archive = watch / batch / "failures" / run_id
            evidence_path = archive / "evidence_manifest.json"
            if not evidence_path.is_file():
                result["issues"].append("original_failure_evidence_not_archived")
            else:
                evidence = read(evidence_path)
                for relative, proof in evidence["files"].items():
                    copied = archive / relative
                    if not copied.is_file() or file_hash(copied) != proof["sha256"]:
                        result["issues"].append("failure_evidence_checksum_mismatch:" + relative)
                if evidence["run_json_source_sha256"] != file_hash(marker):
                    result["issues"].append("original_failure_was_modified")
            if not link:
                result["issues"].append("original_failure_has_no_linked_recovery")
        result["valid"] = not result["issues"]
        rows.append({"original_run_id": run_id, "original_status": status,
                     "recovery": link, "validated_directory": str(current), **result})
    covered = sum(row["valid"] for row in rows)
    return {"audited_at": datetime.now(timezone.utc).isoformat(), "original_experiment_id": batch,
            "expected": expected, "original_terminal": terminal, "original_failed": original_failed,
            "valid_completed_coverage": covered, "outstanding": expected - covered,
            "goal_completion_proven": terminal == expected and covered == expected,
            "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--expected", type=int, default=96)
    parser.add_argument("--recoveries", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.root / "outputs/experiment_console", args.root / "outputs/experiment_console_watch",
                   args.batch, args.expected, read(args.recoveries) if args.recoveries else None)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_suffix(args.out.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.out)
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
