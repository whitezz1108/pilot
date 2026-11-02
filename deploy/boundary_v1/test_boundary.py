"""CPU regression against the installed engine, real tokenizer, and body grammar."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import torch
from transformers import AutoTokenizer
from sglang.srt.constrained.reasoner_grammar_backend import ReasonerGrammarBackend
from sglang.srt.constrained.xgrammar_backend import XGrammarGrammarBackend
from sglang.srt.parser.reasoning_parser import ReasoningParser
from sglang.srt.function_call.function_call_parser import FunctionCallParser
from sglang.srt.entrypoints.openai.protocol import Tool

tokenizer = AutoTokenizer.from_pretrained('/models/Qwen3.8-27B', local_files_only=True)
spec = importlib.util.spec_from_file_location('constraints', '/evidence/native_constraints.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
schema = {'type': 'object', 'properties': {'decision': {'type': 'string', 'enum': ['REVIEW']}},
          'required': ['decision'], 'additionalProperties': False}
tool_schema = {'type': 'object', 'properties': {'paragraph_id': {'type': 'string', 'enum': ['authorized']}},
               'required': ['paragraph_id'], 'additionalProperties': False}
definition = SimpleNamespace(name='open_source_span', input_schema=tool_schema)
tools = [Tool(type='function', function={'name': definition.name, 'parameters': tool_schema})]
call = '<tool_call>\n<function=open_source_span>\n<parameter=paragraph_id>\nauthorized\n</parameter>\n</function>\n</tool_call>'
rp = ReasoningParser(model_type='qwen3', stream_reasoning=True, force_reasoning=True)
xb = XGrammarGrammarBackend(tokenizer, len(tokenizer))
backend = ReasonerGrammarBackend(xb, rp, tokenizer, enable_strict_thinking=False)
shape = mod.closed_native_response_format(schema, [definition])
key = ('structural_tag', json.dumps(shape))

def grammar(thinking=True, no_tools=False):
    k = ('structural_tag', json.dumps(mod.closed_native_response_format(schema, []))) if no_tools else key
    return backend._init_value_dispatch(k, thinking)

def allowed(g, token):
    mask = torch.full((1, (len(tokenizer)+31)//32), -1, dtype=torch.int32)
    g.fill_vocab_mask(mask, 0)
    return bool((int(mask[0, token//32]) >> (token%32)) & 1)

def feed(g, text):
    for token in tokenizer.encode(text, add_special_tokens=False):
        if not allowed(g, token):
            return False
        g.accept_token(token)
    return True

results = []
def check(name, value):
    results.append({'name': name, 'passed': bool(value)})
    assert value, name

strict = getattr(rp.detector, 'requires_explicit_reasoning_end', False)
check('strict_contract_enabled', strict and backend.enable_token_filter and backend.enable_strict_thinking)
check('no_added_thinking_budget', backend.max_think_tokens == -1)
g = grammar()
excluded = rp.detector.think_excluded_tokens
check('all_control_tokens_singleton', all(len(tokenizer.encode(t, add_special_tokens=False)) == 1 for t in excluded))
check('thought_blocks_tool_and_eos', all(not allowed(g, tokenizer.encode(t, add_special_tokens=False)[0]) for t in excluded))
check('explicit_end_allowed', allowed(g, tokenizer.encode('</think>', add_special_tokens=False)[0]))
for name, text, expected in [
    ('one_tool', 'reasoning</think>'+call, True),
    ('one_final', 'reasoning</think>{"decision":"REVIEW"}', True),
    ('missing_end', 'reasoning'+call, False),
    ('two_calls', 'reasoning</think>'+call+'\n'+call, False),
    ('final_then_tool', 'reasoning</think>{"decision":"REVIEW"}'+call, False),
    ('illegal_id', 'reasoning</think>'+call.replace('authorized','invented'), False),
]:
    check(name, feed(grammar(), text) == expected)
check('A0_final', feed(grammar(no_tools=True), 'reasoning</think>{"decision":"REVIEW"}'))
check('A0_tool_blocked', not feed(grammar(no_tools=True), 'reasoning</think>'+call))
check('thinking_disabled_body', feed(grammar(thinking=False), call))

# Exercise rollback through the boundary, including the speculative draft width 4.
prefix = tokenizer.encode('reasoning', add_special_tokens=False)
suffix = tokenizer.encode('</think>'+call, add_special_tokens=False)
for depth in (1, 2, 3, 4, len(suffix)):
    g = grammar()
    for t in prefix + suffix:
        assert allowed(g, t)
        g.accept_token(t)
    g.rollback(depth)
    for t in suffix[-depth:]:
        assert allowed(g, t)
        g.accept_token(t)
    check('rollback_'+str(depth), g._is_generation() and not allowed(g, tokenizer.encode('<tool_call>', add_special_tokens=False)[0]))
g = grammar()
assert feed(g, 'reasoning</think>')
clone = g.copy()
check('copy_after_boundary', feed(clone, call))
check('copy_independent_original', feed(g, '{"decision":"REVIEW"}'))

# Parser gating is tested at every two-chunk split as well as one-character chunks.
def parse(chunks):
    r = ReasoningParser(model_type='qwen3', stream_reasoning=True, force_reasoning=True)
    f = FunctionCallParser(tools, 'qwen3_coder')
    names = []
    for c in chunks:
        thought, normal = r.parse_stream_chunk(c)
        if normal:
            _, calls = f.parse_stream_chunk(normal)
            names.extend(x.name for x in calls if x.name)
    return names

for name, text, expected in [('implicit_end_never_dispatches', 'reasoning'+call+call, []),
                              ('explicit_end_dispatches_once', 'reasoning</think>'+call, ['open_source_span'])]:
    check(name+'_all_splits', all(parse([text[:i],text[i:]]) == expected for i in range(len(text)+1)))
    check(name+'_char_stream', parse(list(text)) == expected)
    rr = ReasoningParser(model_type='qwen3', stream_reasoning=True, force_reasoning=True)
    thought, normal = rr.parse_non_stream(text)
    ff = FunctionCallParser(tools, 'qwen3_coder')
    _, calls = ff.parse_non_stream(normal)
    check(name+'_nonstream', [x.name for x in calls] == expected)
print(json.dumps({'passed': True, 'scope': 'CPU real tokenizer, real grammar masks, rollback and stream parsing; no inference', 'checks': results}, indent=2))
