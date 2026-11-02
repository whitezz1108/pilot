"""CPU-only probe of installed SGLang/XGrammar agreement; no model requests."""
import copy
import json
import xgrammar as xgr
from sglang.srt.entrypoints.openai.protocol import Tool
from sglang.srt.function_call.function_call_parser import FunctionCallParser


def main():
    tool = Tool(type="function", function={
        "name": "open_source_span", "description": "probe",
        "parameters": {"type": "object", "properties": {
            "paragraph_id": {"type": "string", "enum": ["e40e805afe1b:p0007"]}},
            "required": ["paragraph_id"], "additionalProperties": False}, "strict": True})
    make_parser = lambda: FunctionCallParser([tool], "qwen3_coder")
    constraint = make_parser().get_structure_constraint("auto", parallel_tool_calls=False, thinking_mode=False)[1]
    tokenizer = xgr.TokenizerInfo([bytes([i]) for i in range(256)], stop_token_ids=[0])
    compiler = xgr.GrammarCompiler(tokenizer, max_threads=1)
    hardened = copy.deepcopy(constraint.model_dump())
    hardened["format"]["triggers"] = ["<tool_call>"]
    hardened["format"]["excludes"] += ["<function="]
    template = "<tool_call>\n<function=open_source_span>\n<parameter=paragraph_id>\ne40e805afe1b:p0007\n</parameter>\n</function>\n</tool_call>"
    results = []
    for version, shape in (("installed_builtin", constraint), ("prototype_early_trigger", hardened)):
        compiled = compiler.compile_structural_tag(shape)
        for name, call in [
            ("canonical", template),
            ("no_newline_before_function", template.replace("<tool_call>\n", "<tool_call>")),
            ("double_newline_before_function", template.replace("<tool_call>\n", "<tool_call>\n\n")),
            ("space_before_function", template.replace("<tool_call>\n", "<tool_call> ")),
            ("bare_function", template.replace("<tool_call>\n", "").replace("\n</tool_call>", "")),
        ]:
            for variant, text in (("one_legal", call), ("two_legal", call + "\n" + call),
                                  ("illegal_id", call.replace("p0007", "p0384"))):
                matcher = xgr.GrammarMatcher(compiled)
                accepted = matcher.accept_string(text)
                try:
                    content, calls = make_parser().parse_non_stream(text)
                    parsed = [{"name": c.name, "parameters": c.parameters} for c in calls]
                except Exception as exc:
                    parsed = f"{type(exc).__name__}: {exc}"
                results.append({"grammar_version": version, "prefix": name, "variant": variant,
                                "grammar_accepts": accepted, "parser_calls": parsed})
    print(json.dumps({"scope": "CPU character-string acceptance, not GPU decoding or actual historical wire tokens",
                      "constraint": constraint.model_dump(), "prototype": hardened,
                      "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
