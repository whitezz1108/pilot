"""Replay saved generation tokens through the installed grammar; never call a model."""
import json
from pathlib import Path
import torch
from transformers import AutoTokenizer
from sglang.srt.constrained.reasoner_grammar_backend import ReasonerGrammarBackend
from sglang.srt.constrained.xgrammar_backend import XGrammarGrammarBackend
from sglang.srt.parser.reasoning_parser import ReasoningParser

root=Path('/evidence')
tokenizer=AutoTokenizer.from_pretrained('/models/Qwen3.8-27B',local_files_only=True)
config=json.loads(Path('/models/Qwen3.8-27B/config.json').read_text())
text_config=config.get('text_config',config)
vocab_size=text_config['vocab_size']
eos=text_config['eos_token_id']
eos=eos if isinstance(eos,list) else [eos]
generation=json.loads(Path('/models/Qwen3.8-27B/generation_config.json').read_text())
generation_eos=generation.get('eos_token_id',[])
generation_eos=generation_eos if isinstance(generation_eos,list) else [generation_eos]
eos=sorted(set(eos+generation_eos))
backend=ReasonerGrammarBackend(XGrammarGrammarBackend(tokenizer,vocab_size,model_eos_token_ids=eos),
    ReasoningParser(model_type='qwen3',stream_reasoning=True,force_reasoning=True),tokenizer)
end=tokenizer.encode('</think>',add_special_tokens=False)
assert len(end)==1
checks=[]
for directory in sorted((root/'wire').iterdir()):
    request=json.loads((directory/'request.json').read_text())
    events=[]
    for line in (directory/'response.sse').read_text().splitlines():
        if line.startswith('data:') and line[5:].strip()!='[DONE]':
            events.append(json.loads(line[5:]))
    ids=None
    for event in events:
        extension=event.get('sglext') or {}
        if extension.get('output_ids') is not None:
            ids=extension['output_ids'][0]
    if ids is None:
        checks.append({'model_call_id':directory.name,'passed':False,'error':'No raw output IDs; cannot verify boundary'})
        continue
    grammar=backend._init_value_dispatch(('structural_tag',json.dumps(request['response_format'])),True)
    rejected=[]
    body_tokens=0
    for index,token in enumerate(ids):
        mask=torch.full((1,(vocab_size+31)//32),-1,dtype=torch.int32)
        grammar.fill_vocab_mask(mask,0)
        if not ((int(mask[0,token//32])>>(token%32))&1):
            rejected.append({'index':index,'token':token,'text':tokenizer.decode([token]),'thinking':grammar._is_thinking()})
            break
        if grammar._is_generation():
            body_tokens+=1
        grammar.accept_token(token)
    raw=tokenizer.decode(ids,skip_special_tokens=False)
    (directory/'generated_text_before_parsers.txt').write_text(raw)
    (directory/'output_token_ids.json').write_text(json.dumps(ids))
    native_indices=set()
    finish=[]
    for event in events:
        for choice in event.get('choices') or []:
            for tc in (choice.get('delta') or {}).get('tool_calls') or []:
                native_indices.add(tc.get('index'))
            if choice.get('finish_reason'):
                finish.append(choice['finish_reason'])
    passed=not rejected and end[0] in ids and body_tokens>0 and len(native_indices)<=1
    checks.append({'model_call_id':directory.name,'passed':passed,'raw_token_count':len(ids),
        'explicit_think_end_count':ids.count(end[0]),'first_think_end_index':ids.index(end[0]) if end[0] in ids else None,
        'tokens_sent_to_body_grammar':body_tokens,'native_tool_indices':sorted(native_indices),
        'finish_reason':finish,'mask_rejections':rejected})
result={'scope':'Offline replay of actual emitted token IDs through patched reasoning wrapper and exact per-request body grammar; not internal scheduler instrumentation',
        'vocab_size':vocab_size,'model_eos_token_ids':eos,
        'passed':bool(checks) and all(x['passed'] for x in checks),'calls':checks}
(root/'wire_boundary_audit.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
assert result['passed']
