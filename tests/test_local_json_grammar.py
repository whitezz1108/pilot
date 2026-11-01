import json
import pytest
from pilot01.model.openai_compat import OpenAICompatibleClient
from pilot01.model.client import ModelClientError,ModelParams,ModelRequest,ModelMessage

def payload(**kwargs):
    client=OpenAICompatibleClient(base_url='http://offline.invalid/v1',api_key='offline-placeholder',transport=lambda *args: (_ for _ in ()).throw(AssertionError('network forbidden')),**kwargs)
    request=ModelRequest(messages=(ModelMessage(role='user',content='unchanged task'),),params=ModelParams(provider='openai-compatible',model_id='Qwen3.8-27B',temperature=0,max_output_tokens=32768,reasoning_effort='xhigh',timeout_seconds=360),role='manager',prompt_version='test',invocation=0,run_id='offline')
    return json.loads(client.build_request(request).data)

def test_explicit_grammar_changes_only_constraint(monkeypatch,tmp_path):
    monkeypatch.delenv('PILOT01_JSON_OBJECT_GRAMMAR_PATH',raising=False)
    before=payload();p=tmp_path/'grammar.ebnf';grammar='root ::= [ \\n\\r\\t]* "{}"';p.write_text(grammar)
    monkeypatch.setenv('PILOT01_JSON_OBJECT_GRAMMAR_PATH',str(p))
    after=payload();assert after.pop('ebnf')==grammar
    assert before.pop('response_format')=={'type':'json_object'}
    assert after==before and after['max_tokens']==32768 and after['reasoning_effort']=='xhigh'

def test_missing_grammar_fails_without_fallback(monkeypatch,tmp_path):
    monkeypatch.setenv('PILOT01_JSON_OBJECT_GRAMMAR_PATH',str(tmp_path/'missing'))
    with pytest.raises(ModelClientError,match='cannot read'):payload()

def test_empty_grammar_fails(monkeypatch,tmp_path):
    p=tmp_path/'empty';p.write_text(' ');monkeypatch.setenv('PILOT01_JSON_OBJECT_GRAMMAR_PATH',str(p))
    with pytest.raises(ModelClientError,match='empty'):payload()

def test_non_json_requests_unaffected(monkeypatch,tmp_path):
    monkeypatch.setenv('PILOT01_JSON_OBJECT_GRAMMAR_PATH',str(tmp_path/'missing'))
    p=payload(json_mode=False);assert 'ebnf' not in p and 'response_format' not in p
