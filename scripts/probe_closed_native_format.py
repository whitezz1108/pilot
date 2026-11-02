"""CPU-only feasibility probe; does not change any live request or service."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import xgrammar as xgr
from sglang.srt.entrypoints.openai.protocol import Tool
from sglang.srt.function_call.function_call_parser import FunctionCallParser


def main():
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--tokenizer", help="Optional local Hugging Face tokenizer; never load model weights")
    arguments.add_argument("--runtime-module", type=Path, help="Compile the actual deployed constraint builder")
    args = arguments.parse_args()
    schemas = json.loads(Path("/tmp/pilot96_output_schemas.json").read_text())
    definitions = [
        Tool(type="function", function={"name": "search_contract", "description": "probe", "strict": True,
             "parameters": {"type": "object", "properties": {
                 "query": {"type": "string", "minLength": 1},
                 "target_category": {"type": "string", "enum": ["Change Of Control", "Termination For Convenience"]}},
                 "required": ["query", "target_category"], "additionalProperties": False}}),
        Tool(type="function", function={"name": "open_source_span", "description": "probe", "strict": True,
             "parameters": {"type": "object", "properties": {
                 "paragraph_id": {"type": "string", "enum": ["only-authorized"]}},
                 "required": ["paragraph_id"], "additionalProperties": False}}),
    ]
    if args.tokenizer:
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, local_files_only=True)
        tokenizer_info = xgr.TokenizerInfo.from_huggingface(tokenizer)
        scope = "CPU matching compiled with the actual local model tokenizer and role schemas; no inference or live changes"
    else:
        tokenizer_info = xgr.TokenizerInfo([bytes([i]) for i in range(256)], stop_token_ids=[0])
        scope = "CPU byte-vocabulary matching with actual role schemas; no model calls"
    compiler = xgr.GrammarCompiler(tokenizer_info, max_threads=1)
    runtime = None
    if args.runtime_module:
        spec = importlib.util.spec_from_file_location("native_constraints_under_test", args.runtime_module)
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
    tool = "<tool_call>\n<function=open_source_span>\n<parameter=paragraph_id>\nonly-authorized\n</parameter>\n</function>\n</tool_call>"
    results = []
    formats = {}
    for role, final_schema in schemas.items():
        branches = [{"type": "json_schema", "json_schema": final_schema, "any_order": True}]
        for definition in definitions:
            branches.append({"type": "tag", "begin": f"<tool_call>\n<function={definition.function.name}>\n",
                             "content": {"type": "json_schema", "json_schema": definition.function.parameters,
                                         "style": "qwen_xml", "any_order": True}, "end": "\n</function>\n</tool_call>"})
        shape = {"type": "structural_tag", "format": {"type": "sequence", "elements": [
            {"type": "regex", "pattern": "[ \\t\\r\\n]*"}, {"type": "or", "elements": branches},
            {"type": "regex", "pattern": "[ \\t\\r\\n]*"}]}}
        if runtime:
            shape = runtime.closed_native_response_format(final_schema, [
                SimpleNamespace(name=d.function.name, input_schema=d.function.parameters) for d in definitions])
        formats[role] = shape
        compiled = compiler.compile_structural_tag(shape)
        final = {
            "role": role,
            "target_clause_status": {"Change Of Control": "unknown", "Termination For Convenience": "unknown"},
            "decision": "REVIEW", "rule_id": "POLICY-01",
            "evidence_provenance": {"upstream_claim_ids": [], "inherited_source_ids": [],
                "opened_paragraph_ids": [], "relied_paragraph_ids": [], "candidate_source_ids": [],
                "verification_basis": "unavailable"},
            "confidence": 0.5, "reason_summary": "Protocol probe; no task was performed.", "uncertainties": [],
        }
        examples = [
            ("one_offered_call", tool, True),
            ("whitespace_then_one_call", "\n\n" + tool + "\n", True),
            ("two_calls", tool + "\n" + tool, False),
            ("illegal_paragraph", tool.replace("only-authorized", "fabricated"), False),
            ("unoffered_function", tool.replace("open_source_span", "unoffered_function"), False),
            ("bare_function_terminator", "</function>", False),
            ("bare_terminators", "</function>\n</tool_call>", False),
            ("empty_function_terminators", "<tool_call>\n</function>\n</tool_call>", False),
            ("final_and_tool", json.dumps(final) + "\n" + tool, False),
            ("natural_language_before_tool", "I will search now.\n" + tool, False),
            ("missing_final_fields", '{"decision":"REVIEW"}', False),
        ]
        for decision, statuses in (("REVIEW", ("unknown", "unknown")), ("ACCEPT", ("absent", "absent")),
                                   ("ESCALATE", ("present", "unknown"))):
            body = {**final, "decision": decision,
                    "target_clause_status": dict(zip(final["target_clause_status"], statuses))}
            examples.append(("valid_final_" + decision, json.dumps(body), True))
        for name, text, expected in examples:
            accepted = xgr.GrammarMatcher(compiled).accept_string(text)
            parser = FunctionCallParser(definitions, "qwen3_coder")
            normal, calls = parser.parse_stream_chunk(text)
            results.append({"role": role, "case": name, "expected": expected, "accepted": accepted,
                            "parser_tool_indices": [call.tool_index for call in calls],
                            "parser_names": [call.name for call in calls]})
        if runtime:
            final_only = runtime.closed_native_response_format(final_schema, [])
            formats[role + "_no_tools"] = final_only
            compiled_final = compiler.compile_structural_tag(final_only)
            for name, text, expected in examples:
                expected = expected and name.startswith("valid_final_")
                results.append({"role": role, "case": "no_tools_" + name, "expected": expected,
                                "accepted": xgr.GrammarMatcher(compiled_final).accept_string(text)})
    matched = all(row["accepted"] == row["expected"] for row in results)
    print(json.dumps({"scope": scope, "tokenizer_path": args.tokenizer,
                      "runtime_source_sha256": hashlib.sha256(args.runtime_module.read_bytes()).hexdigest() if runtime else None,
                      "all_expected": matched, "results": results, "prototype_formats": formats}, ensure_ascii=False, indent=2))
    if not matched:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
