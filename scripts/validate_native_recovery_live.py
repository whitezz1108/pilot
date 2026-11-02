"""Isolated GPU2 wire validation, separate from all experiment/recovery runs."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from pilot01.model import ModelCallLog, ModelMessage, ModelRequest
from pilot01.model.execution import EndpointSettings
from pilot01.model.native_constraints import VERSION, schema_without_metadata
from pilot01.model.openai_compat import OpenAICompatibleClient, read_sglang_capacity
from pilot01.model.parsing import record_call
from pilot01.model.serving_capacity import validate_sglang_capacity
from pilot01.model.tool_protocol import ToolDefinition
from pilot01.runtime import ExperimentProfile
from pilot01.schemas import ComplianceOutputV2, ManagerOutputV2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--original", required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    watch = root / "outputs/experiment_console_watch" / args.original
    frozen = json.loads((watch / "original_terminal_summary.json").read_text())
    assert frozen["expected"] == len(frozen["runs"]) == 96 and frozen["no_active_requests_confirmed"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out = watch / "repair_cycle_01" / ("live_protocol_probe_" + stamp)
    out.mkdir(parents=True, exist_ok=False)
    profile = ExperimentProfile(root / "config/experiment_native_tools_closed_v4.yaml")
    tokenizer = "/home/evan/ai/models/Qwen3.8-27B/tokenizer.json"
    endpoint = EndpointSettings(slot=3, max_in_flight=4, reasoning_tokenizer_path=tokenizer,
        context_window_tokens=49152, context_safety_tokens=1024, capacity_preflight="sglang-v1")
    capacity = validate_sglang_capacity(read_sglang_capacity("http://127.0.0.1:30002/v1"), endpoint)
    client = OpenAICompatibleClient(base_url="http://127.0.0.1:30002/v1", api_key="local-only",
        reasoning_stream=True, reasoning_tokenizer_path=tokenizer, tool_transport="native",
        context_window_tokens=49152, context_safety_tokens=1024, minimum_output_tokens=2048)
    definitions = (ToolDefinition(name="search_contract", description="Search the current authorized contract.",
        input_schema={"type": "object", "properties": {"query": {"type": "string", "minLength": 1},
            "target_category": {"type": "string", "enum": ["Change Of Control", "Termination For Convenience"]}},
            "required": ["query", "target_category"], "additionalProperties": False}),)
    log = ModelCallLog(out / "model_calls.jsonl")
    observations, payloads = [], []
    checks = (("single_native_call", "manager", True), ("final_while_tools_available", "manager", True),
              ("A0_final_without_tools", "compliance", False))
    try:
        for index, (name, role, has_tools) in enumerate(checks):
            schema = ManagerOutputV2 if role == "manager" else ComplianceOutputV2
            final = {"role": role, "target_clause_status": {"Change Of Control": "unknown", "Termination For Convenience": "unknown"},
                "decision": "REVIEW", "rule_id": "POLICY-01", "evidence_provenance": {
                    "upstream_claim_ids": [], "inherited_source_ids": [], "opened_paragraph_ids": [],
                    "relied_paragraph_ids": [], "candidate_source_ids": [], "verification_basis": "unavailable"},
                "confidence": 0.5, "reason_summary": "Isolated protocol validation; no contract evaluated.", "uncertainties": []}
            user = ("Call search_contract exactly once with query 'change of control' and target_category 'Change Of Control'."
                    if name == "single_native_call" else "Return this final JSON without calling tools: " + json.dumps(final))
            request = ModelRequest(messages=(ModelMessage(role="system", content=
                "This is an isolated output-protocol check, not a contract experiment. Keep normal reasoning enabled. "
                "Follow the user request: one specified native tool call, or the specified final JSON. Never batch tools."),
                ModelMessage(role="user", content=user)), tools=definitions if has_tools else (),
                tool_choice="auto" if has_tools else None, parallel_tool_calls=False if has_tools else None,
                native_output_format=VERSION, final_output_schema=schema_without_metadata(schema.model_json_schema()),
                params=profile.models.for_role(role), role=role, prompt_version="isolated-wire-validation")
            request = client.prepare_request(request)
            payloads.append(json.loads(client.build_request(request).data))
            try:
                response = client.generate(request)
            except Exception as exc:
                log.append(record_call(index=index, request=request, prompt_version=request.prompt_version,
                    attempt=0, is_repair=False, response=getattr(exc, "partial_response", None), error=str(exc)))
                raise
            parsed = None if response.tool_calls else schema.model_validate_json(response.text)
            log.append(record_call(index=index, request=request, prompt_version=request.prompt_version,
                attempt=0, is_repair=False, response=response,
                parsed_output=parsed.model_dump(mode="json") if parsed is not None else None))
            assert response.finish_reason != "length"
            if name == "single_native_call":
                assert len(response.tool_calls) == 1 and response.tool_calls[0].name == "search_contract"
                assert response.tool_calls[0].arguments == {"query": "change of control", "target_category": "Change Of Control"}
            else:
                assert not response.tool_calls and parsed is not None
            observations.append({"check": name, "passed": True, "finish_reason": response.finish_reason,
                "reasoning_characters": len(response.reasoning_content or ""),
                "usage": response.usage.model_dump(mode="json") if response.usage else None})
            print(json.dumps(observations[-1]), flush=True)
        assert any(row["reasoning_characters"] > 0 for row in observations), "reasoning path was not observed"
        result = {"scope": "3 isolated GPU2 protocol requests; not complete experiment reruns", "passed": True,
                  "profile_fingerprint": profile.fingerprint(), "capacity": capacity, "checks": observations}
    except Exception as exc:
        result = {"passed": False, "error": f"{type(exc).__name__}: {exc}", "checks": observations}
        raise
    finally:
        (out / "wire_payloads.json").write_text(json.dumps(payloads, indent=2) + "\n")
        (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"evidence": str(out), "passed": result["passed"]}), flush=True)


if __name__ == "__main__":
    main()
