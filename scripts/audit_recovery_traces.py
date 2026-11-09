"""Read-only completed-run trace inspection, separate from the frozen runtime."""
import argparse
import datetime
import json
import re
from collections import Counter
from pathlib import Path

from pilot01.experiment.cases import CaseRegistry


HIDDEN_MARKERS = re.compile(
    r"(?<![A-Za-z0-9])(?:A[01]V[012]D[012]|E[01]|condition_id|error_condition|"
    r"is_negative_sentinel|omission_target_claim_id|gold_target_clause_status|"
    r"gold_evidence_offsets|recovery_required|verification_required)(?![A-Za-z0-9])")


def input_marker_matches(text, run_id):
    """Scan declared labels and the exact artifact identity independently.

    Python word boundaries include underscores, whereas compound run IDs use
    underscores as separators. An exact identity check also covers IDs whose
    condition names are outside the current marker vocabulary.
    """
    matches = [(match.start(), match.end(), match.group(), "hidden_marker")
               for match in HIDDEN_MARKERS.finditer(text)]
    if run_id:
        matches.extend((match.start(), match.end(), match.group(), "exact_artifact_run_id")
                       for match in re.finditer(re.escape(run_id), text))
    return [{"marker": marker, "match_kind": kind, "start_char": start, "end_char": end,
             "context": text[max(0, start - 90):end + 90]}
            for start, end, marker, kind in sorted(matches)]


def read_jsonl(path):
    if not path.is_file():
        return []
    records = []
    # U+2028/U+2029 can occur inside an unescaped JSON string. They are not
    # JSONL delimiters, even though str.splitlines() treats them as new lines.
    for number, line in enumerate(path.read_text(encoding="utf-8").split('\n'), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as error:
            raise ValueError(f"{path.name}:{number}: malformed JSON record") from error
        if not isinstance(record, dict):
            raise ValueError(f"{path.name}:{number}: expected a JSON object")
        records.append(record)
    return records


def rendered_input(call):
    value = call.get("rendered_input")
    return value if isinstance(value, str) else ""


def string_list(value):
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def schema_issues(models, tools, verifications):
    """Report missing/changed audit fields without guessing their values."""
    issues = []
    fields = (("model_calls", models, {"role": str, "invocation": int,
               "call_index": int, "rendered_input": str}),
              ("tool_calls", tools, {"node": str, "invocation": int,
               "call_index": int, "sequence": int, "tool": str,
               "arguments": dict, "ok": bool, "result_ids": list}),
              ("verification", verifications, {"node": str, "invocation": int}))
    for name, records, expected in fields:
        for line, record in enumerate(records, 1):
            for field, kind in expected.items():
                if type(record.get(field)) is not kind:
                    issues.append({"log": name, "record": line, "field": field,
                                   "expected_type": kind.__name__,
                                   "observed_type": type(record.get(field)).__name__})
    return issues


def model_key(record, role_field):
    return json.dumps([record.get(role_field), record.get("invocation"),
                       record.get("call_index")], sort_keys=True)


def ordered_indices(values):
    valid = all(type(value) is int and value >= 0 for value in values)
    return {"count": len(values), "valid_integer_indices": valid,
            "unique": len(set(values)) == len(values) if valid else None,
            "strictly_increasing": all(a < b for a, b in zip(values, values[1:])) if valid else None}


def actual_tool_requests(call):
    """Prefer native provider payload so rejected multi-tool responses stay visible."""
    provider = call.get("provider_response")
    if isinstance(provider, dict) and isinstance(provider.get("choices"), list) and provider["choices"]:
        if len(provider["choices"]) != 1:
            return None, "unsupported_multiple_provider_choices"
        choice = provider["choices"][0]
        message = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(message, dict):
            if message.get("function_call") is not None:
                return None, "unsupported_legacy_function_call_format"
            native = message.get("tool_calls")
            if native is None:
                native = []
            if not isinstance(native, list):
                return None, "unknown_native_tool_calls_format"
            if not native and isinstance(call.get("tool_request"), dict):
                request = call["tool_request"]
                if isinstance(request.get("tool"), str):
                    return [{"id": request.get("call_id"), "tool": request["tool"],
                             "arguments": request.get("arguments")}], "parsed_legacy_request"
            if not native and (call.get("parse_error") or call.get("error")
                               or call.get("is_tool_turn") is True):
                return None, "empty_native_calls_with_unresolved_response"
            requests = []
            for item in native:
                if not isinstance(item, dict) or not isinstance(item.get("function"), dict):
                    return None, "unknown_native_tool_call_format"
                function = item["function"]
                if not isinstance(function.get("name"), str) or not function["name"]:
                    return None, "unknown_native_tool_name_format"
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        arguments = {"unparsed_json_arguments": arguments}
                requests.append({"id": item.get("id"), "tool": function.get("name"), "arguments": arguments})
            return requests, "native_provider_payload"
    # Old scripted/legacy records can provide a parsed request without a provider payload.
    request = call.get("tool_request")
    if isinstance(request, dict) and isinstance(request.get("tool"), str):
        return [{"id": request.get("call_id"), "tool": request["tool"],
                 "arguments": request.get("arguments")}], "parsed_request_only"
    if call.get("parsed_output") is not None and call.get("is_tool_turn") is False:
        return [], "parsed_final_output_only"
    return None, "unknown_response_format_or_missing_response"


def log_integrity(directory, artifact, models, tools, verifications):
    file_presence = {name: (directory / name).is_file() for name in
                     ("model_calls.jsonl", "tool_calls.jsonl", "verification.jsonl")}
    requests = []
    unknown_calls = []
    for model in models:
        emitted, source = actual_tool_requests(model)
        location = {key: model.get(key) for key in ("role", "invocation", "call_index", "model_call_id")}
        if emitted is None:
            unknown_calls.append(dict(location, reason=source, error=model.get("error"),
                                      parse_error=model.get("parse_error")))
            continue
        for request in emitted:
            candidates = [tool for tool in tools if tool.get("node") == model.get("role")
                          and tool.get("invocation") == model.get("invocation")
                          and tool.get("call_index") == model.get("call_index")]
            if request["id"] is not None:
                candidates = [tool for tool in candidates if tool.get("tool_call_id") == request["id"]]
            match = len(candidates) == 1 and candidates[0].get("tool") == request["tool"]
            if match:
                match = candidates[0].get("arguments") == request["arguments"]
            if match and candidates[0].get("parent_model_call_id") is not None:
                match = candidates[0]["parent_model_call_id"] == model.get("model_call_id")
            requests.append(dict(location, request=request, request_source=source,
                                 matching_execution_count=len(candidates), execution_matches=match,
                                 matching_execution_sequences=[tool.get("sequence") for tool in candidates],
                                 model_parse_error=model.get("parse_error"), model_error=model.get("error")))
    model_keys = {model_key(m, "role") for m in models}
    orphan_tools = [tool.get("sequence") for tool in tools
                    if model_key(tool, "node") not in model_keys]
    groups = sorted({(str(m.get("role")), str(m.get("invocation"))) for m in models})
    observed_counts = {"model_calls": len(models), "tool_calls": len(tools), "verifications": len(verifications)}
    issues = schema_issues(models, tools, verifications)
    counts_match = {name: artifact.get(name) == count for name, count in observed_counts.items()}
    model_count_complete = file_presence["model_calls.jsonl"] and counts_match["model_calls"]
    return {"log_files_present": file_presence,
            "audit_field_schema_issues": issues,
            "model_indices_by_role": {role: ordered_indices([
                m.get("call_index") for m in models if str(m.get("role")) == role])
                for role in sorted({str(m.get("role")) for m in models})},
            "model_indices_by_role_invocation": {f"{role}/{invocation}": ordered_indices([
                m.get("call_index") for m in models if str(m.get("role")) == role and str(m.get("invocation")) == invocation])
                for role, invocation in groups},
            "tool_sequence": ordered_indices([t.get("sequence") for t in tools]),
            "artifact_logged_counts": {"model_calls": artifact.get("model_calls"),
                                        "tool_calls": artifact.get("tool_calls"),
                                        "verifications": artifact.get("verifications")},
            "observed_log_counts": observed_counts,
            "artifact_counts_match_logs": counts_match,
            "actual_requested_tool_calls": len(requests) if not unknown_calls and model_count_complete else None,
            "known_requested_tool_calls": len(requests),
            "request_execution_counts_equal": len(requests) == len(tools) if not unknown_calls and not issues and all(file_presence.values()) and all(counts_match.values()) else None,
            "unknown_model_response_formats": unknown_calls,
            "requested_tool_call_correspondence": requests,
            "tool_execution_without_model_record": orphan_tools,
            "tool_sequences_without_confirmed_matching_request": [
                tool.get("sequence") for tool in tools if not any(
                    request["execution_matches"] and tool.get("sequence") in request["matching_execution_sequences"]
                    for request in requests)],
            "interpretation": "Unexecuted provider calls may reflect a deliberate protocol rejection. Missing or unknown logs prevent a complete request-count claim."}


def model_activity(models):
    counts = Counter()
    for call in models:
        if call.get("is_repair") is True:
            purpose = "format_repair"
        else:
            requests, _ = actual_tool_requests(call)
            if requests:
                purpose = "tool"
            elif isinstance(call.get("parsed_output"), dict) and call.get("is_tool_turn") is False:
                purpose = "final_candidate"
            else:
                purpose = "unknown"
        counts[purpose] += 1
    return {"parse_error_calls": sum(bool(call.get("parse_error")) for call in models),
            "error_calls": sum(bool(call.get("error")) for call in models),
            "finish_reason_length_calls": sum(call.get("finish_reason") == "length" for call in models),
            "call_purpose_counts": {purpose: counts[purpose] for purpose in
                                    ("format_repair", "tool", "final_candidate", "unknown")},
            "purpose_definition": "Format repair has precedence; tool uses known emitted request payloads; final_candidate requires parsed output and is_tool_turn=false. Candidate does not imply delivered. Unknown remains separate."}


def workflow_timing(execution):
    start, end = execution.get("started_at"), execution.get("completed_at")
    seconds, reason = None, None
    try:
        if not isinstance(start, str) or not isinstance(end, str):
            raise ValueError("missing start/end")
        start_time, end_time = (datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
                                for value in (start, end))
        if start_time.tzinfo is None or end_time.tzinfo is None:
            raise ValueError("timestamp has no time zone")
        seconds = (end_time - start_time).total_seconds()
        if seconds < 0:
            raise ValueError("end precedes start")
    except (ValueError, TypeError) as error:
        seconds, reason = None, str(error)
    return {"started_at": start, "completed_at": end,
            "workflow_wall_seconds": seconds, "unavailable_reason": reason}


def first_visibility(models, paragraph_id):
    shorthand = paragraph_id.split(":")[-1]
    return {kind: next((call.get("call_index") for call in models
                        if needle in rendered_input(call)), None)
            for kind, needle in (("full_id", paragraph_id), ("shorthand", shorthand))}


def inspect_run(directory, registry):
    artifact = json.loads((directory / "run.json").read_text(encoding="utf-8"))
    if artifact["run_id"] != directory.name:
        raise ValueError("run identity differs from directory")
    if artifact.get("execution", {}).get("status") not in ("completed", "failed"):
        raise ValueError("trace inspection requires a terminal artifact")
    models = read_jsonl(directory / "model_calls.jsonl")
    tools = read_jsonl(directory / "tool_calls.jsonl")
    verifications = read_jsonl(directory / "verification.jsonl")
    for record in models + tools + verifications:
        if record.get("run_id") not in (None, artifact["run_id"]):
            raise ValueError("cross-run trace record")
    case = registry.get(artifact["case_id"])
    document = registry.document(case.case_id)
    paragraph_ids = {p.paragraph_id for p in document.paragraphs}
    gold_ids = {p.paragraph_id for p in document.paragraphs
                if any(start < p.end_char and p.start_char < end for start, end in case.gold_spans())}
    result = {"run_id": artifact["run_id"], "status": artifact["execution"]["status"],
              "completed_at": artifact["execution"].get("completed_at"),
              "workflow_timing": workflow_timing(artifact["execution"]),
              "model_activity": model_activity(models),
              "log_integrity": log_integrity(directory, artifact, models, tools, verifications),
              "model_inputs_scanned": sum(isinstance(call.get("rendered_input"), str) for call in models),
              "uninspectable_model_inputs": sum(not isinstance(call.get("rendered_input"), str) for call in models),
              "model_input_marker_matches": [], "roles": {}}
    for line, call in enumerate(models, 1):
        for match in input_marker_matches(rendered_input(call), artifact["run_id"]):
            result["model_input_marker_matches"].append({
                "model_jsonl_line": line, "role": call.get("role"), "call_index": call.get("call_index"),
                **match})
    for role in ("manager", "compliance"):
        role_models = [call for call in models if call.get("role") == role]
        role_tools = [call for call in tools if call.get("node") == role]
        output = artifact["execution"].get(role + "_output")
        verification = next((v for v in reversed(verifications) if v.get("node") == role), {})
        returned = {pid for call in role_tools if call.get("ok") is True
                    and call.get("tool") in ("search_contract", "next_search_page") for pid in string_list(call.get("result_ids"))}
        opened = {pid for call in role_tools if call.get("ok") is True
                  and call.get("tool") == "open_source_span" for pid in string_list(call.get("result_ids"))}
        provenance = output.get("evidence_provenance", {}) if output else {}
        relied = set(string_list(provenance.get("relied_paragraph_ids")))
        unobserved = [entry for entry in (verification.get("citation_audit") or [])
                      if isinstance(entry, dict) and entry.get("observed") is False
                      and isinstance(entry.get("source_id"), str)]
        result["roles"][role] = {
            "model_calls": len(role_models), "tool_calls": len(role_tools),
            "model_activity": model_activity(role_models),
            "tool_type_counts": dict(Counter(str(call.get("tool")) for call in role_tools)),
            "tool_failures": [{k: c.get(k) for k in ("sequence", "call_index", "tool", "arguments", "error")}
                              for c in role_tools if c.get("ok") is False],
            "target_statuses": output.get("target_clause_status") if output else None,
            "audit_failures": verification.get("audit_failures"),
            "ledger_unobserved_citations": [dict(entry, first_input_visibility=first_visibility(role_models, entry["source_id"]))
                                             for entry in unobserved],
            "reported_opened_but_not_self_opened": sorted(set(string_list(provenance.get("opened_paragraph_ids"))) - opened) if output else [],
            "relied_but_not_self_opened": sorted(relied - opened),
            "returned_ids": sorted(returned), "opened_ids": sorted(opened),
            "not_returned_ids": sorted(paragraph_ids - returned),
            "gold_returned": sorted(gold_ids & returned), "gold_opened": sorted(gold_ids & opened),
            "gold_opened_and_relied_proxy": sorted(gold_ids & opened & relied),
            "retrieval_ledger_input_calls": [call.get("call_index") for call in role_models
                                              if "RUNTIME RETRIEVAL LEDGER" in rendered_input(call)],
            "search_stop_input_calls": [call.get("call_index") for call in role_models
                                         if "RUNTIME SEARCH STOP" in rendered_input(call)],
            "budget_limit": artifact.get("runtime_config", {}).get("max_tool_requests_per_node"),
            "reason_summary": output.get("reason_summary") if output else None,
            "uncertainties": output.get("uncertainties") if output else None,
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", required=True, type=Path)
    parser.add_argument("--registry", required=True, type=Path)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--run-id", action="append")
    selection.add_argument("--all-terminal", action="store_true",
                           help="Inspect finalized completed/failed artifacts and list unfinished directories separately.")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    registry = CaseRegistry.load_json(args.registry)
    runs, unfinished = [], []
    run_ids = args.run_id or []
    if args.all_terminal:
        for directory in sorted(args.raw.iterdir()):
            if not directory.is_dir():
                continue
            path = directory / "run.json"
            if not path.exists():
                unfinished.append({"run_id": directory.name, "reason": "no final artifact at snapshot"})
                continue
            try:
                artifact = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                unfinished.append({"run_id": directory.name, "reason": "artifact not readable as complete JSON at snapshot"})
                continue
            if artifact["execution"]["status"] not in ("completed", "failed"):
                unfinished.append({"run_id": directory.name, "reason": "artifact status is not terminal"})
                continue
            run_ids.append(directory.name)
    for run_id in run_ids:
        directory = (args.raw / run_id).resolve()
        if directory.parent != args.raw.resolve():
            raise ValueError("run-id must name a direct raw subdirectory")
        runs.append(inspect_run(directory, registry))
    report = {"scope": "Offline inspection of finalized artifacts and logged actual model inputs.",
              "snapshot_observed_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "unfinalized_directories": unfinished,
              "limitations": ["Marker matches require human or AI contextual review; zero matches do not prove semantic nonleakage.",
                              "Structured observation ledger and textual visibility are distinct.",
                              "Gold paragraph overlap is a process proxy, not semantic support.",
                              "Temporal ledger presence alone does not establish a cause of model behavior."],
              "runs": runs}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Inspected {len(runs)} finalized artifact(s). Marker matches "
          f"{sum(len(run['model_input_marker_matches']) for run in runs)}. "
          f"Unfinished directories excluded {len(unfinished)}.")


if __name__ == "__main__":
    main()
