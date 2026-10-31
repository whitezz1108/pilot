"""Reasoning evidence survives parsing, streaming failure and lazy export."""
import io
import json
from unittest.mock import patch

import pytest

from pilot01.model.client import ModelClientError
from pilot01.model.openai_compat import OpenAICompatibleClient, _urlopen_transport
from pilot01.model.parsing import record_call, call_structured
from pilot01.model.log import ModelCallLog
from pilot01.model.reasoning import StreamCapture, observe_reasoning
from pilot01.experiment.research_records import split_record, join_record
from test_model_client import request_for, envelope, BASE_URL, DUMMY_KEY
from test_experiment_records import completed_app


def chunk(delta=None, finish=None, usage=None):
    return dict(id="test-stream", model="test-model", choices=[dict(index=0, delta=delta or {}, finish_reason=finish)], usage=usage)


def stream_bytes(events, done=True):
    return ("".join("data: " + json.dumps(e, ensure_ascii=False) + "\r\n\r\n" for e in events)
            + ("data: [DONE]\r\n\r\n" if done else "")).encode()


def client(transport, **kwargs):
    return OpenAICompatibleClient(base_url=BASE_URL, api_key=DUMMY_KEY, transport=transport, **kwargs)


def test_stream_timing_is_observed_interval_not_prefill_or_total_latency():
    clock = iter([0., 5., 7., 8., 9., 10.])
    c = StreamCapture(clock=lambda: next(clock))
    for e in [chunk({'role':'assistant'}), chunk({'reasoning_content':'思考'}),
              chunk({'reasoning_content':'继续'}), chunk({'content':'{}'}, 'stop')]:
        c.feed(json.dumps(e))
    c.feed('[DONE]')
    assert c.snapshot()['choices'][0]['message'] == {'content':'{}','reasoning_content':'思考继续'}
    t = c.timing()
    assert t['first_reasoning_seconds'] == 7
    assert t['reasoning_phase_seconds'] == 2
    assert t['reasoning_span_seconds'] == 1
    assert t['stream_complete']


def test_real_transport_parses_sse_and_preserves_usage_and_unicode(monkeypatch):
    events = [chunk({'reasoning_content':'逐步核对。'}), chunk({'content':'{"ok":true}'},'stop'),
              dict(choices=[],usage=dict(prompt_tokens=10,completion_tokens=20,total_tokens=30,
                   completion_tokens_details=dict(reasoning_tokens=15)))]
    monkeypatch.setattr('urllib.request.urlopen', lambda *a,**k: io.BytesIO(stream_bytes(events)))
    r = client(_urlopen_transport, reasoning_stream=True).generate(request_for())
    assert r.text == '{"ok":true}'
    assert r.reasoning_content == '逐步核对。'
    assert r.usage.reasoning_tokens == 15
    assert r.reasoning_observation['tokens'] == 15
    assert len(r.provider_response['stream_events']) == 3
    log = record_call(index=0,request=request_for(),prompt_version='test',attempt=0,is_repair=False,response=r)
    assert log.reasoning_content == r.reasoning_content
    assert 'reasoning_content' not in json.loads(client(lambda *_: b'', reasoning_stream=True).build_request(request_for()).data)['messages'][0]


@pytest.mark.parametrize('failure', ['eof','timeout','malformed'])
def test_failed_stream_keeps_partial_reasoning_without_usage_or_fake_duration(monkeypatch, failure):
    body=stream_bytes([chunk({'reasoning_content':'partial evidence'})],done=False)
    class Broken(io.BytesIO):
        def __next__(self):
            line=super().readline()
            if line:return line
            if failure=='timeout':raise TimeoutError('timeout')
            raise StopIteration
    if failure=='malformed':body+=b'data: not-json\n\n'
    monkeypatch.setattr('urllib.request.urlopen',lambda *a,**k: Broken(body))
    with pytest.raises(ModelClientError) as exc:
        client(_urlopen_transport,reasoning_stream=True).generate(request_for())
    r=exc.value.partial_response
    assert r.reasoning_content=='partial evidence'
    assert not r.reasoning_observation['stream_complete']
    assert r.reasoning_observation['reasoning_phase_seconds'] is None
    assert r.usage is None


def test_nonstream_unknown_is_not_zero_and_native_zero_is_retained(tmp_path):
    body=json.loads(envelope('{}'))
    body['choices'][0]['message']['reasoning_content']='reason text'
    r=client(lambda *_:json.dumps(body).encode()).generate(request_for())
    assert r.reasoning_observation['tokens'] is None
    assert r.reasoning_observation['reasoning_phase_seconds'] is None
    body['usage']['reasoning_tokens']=0
    r=client(lambda *_:json.dumps(body).encode()).generate(request_for())
    assert r.usage.reasoning_tokens==0
    assert r.reasoning_observation['tokens']==0


def test_reencode_count_is_separate_from_provider_usage(tmp_path):
    p=tmp_path/'tokenizer.json';p.write_text('{}')
    class Tokens:
        def encode(self, text, add_special_tokens):
            assert text=='reason' and add_special_tokens is False
            return type('Result',(),{'ids':[1,2,3]})()
    body=json.loads(envelope('{}'));body['choices'][0]['message']['reasoning_content']='reason'
    with patch('pilot01.model.reasoning._tokenizer',return_value=(Tokens(),'test-sha')):
        r=client(lambda *_:json.dumps(body).encode(),reasoning_tokenizer_path=str(p)).generate(request_for())
    assert r.reasoning_observation['tokens']==3
    assert r.reasoning_observation['token_source']=='local_tokenizer_reencode'
    assert r.usage.reasoning_tokens is None
    assert r.reasoning_observation['tokenizer_sha256']=='test-sha'


def test_reasoning_only_truncation_is_preserved():
    body=json.loads(envelope(''));body['choices'][0].update(finish_reason='length')
    body['choices'][0]['message']={'content':None,'reasoning_content':'still reasoning'}
    r=client(lambda *_:json.dumps(body).encode()).generate(request_for())
    assert r.text=='' and r.truncated and r.reasoning_content=='still reasoning'


def test_lazy_sections_and_roundtrip_preserve_new_fields(tmp_path):
    app=completed_app(tmp_path);ident=app.records.list()[0]['id']
    rid=app.records.batch(ident)['rows'][0]['run_id'];original=app.records.run(ident,rid)
    c=original['calls'][0]
    c.update(reasoning_content='long reasoning',reasoning_observation={'tokens':5},
             provider_response={'stream_events':[{'data':'raw evidence'}]})
    parts=split_record(original)
    assert 'reasoning_content' not in parts['model_calls'][0]
    assert 'provider_response' not in parts['model_calls'][0]
    assert parts['model_responses'][0]['reasoning_content']=='long reasoning'
    assert join_record(parts)==original


def test_partial_stream_is_persisted_by_workflow_error_path(monkeypatch):
    from pydantic import BaseModel
    class Answer(BaseModel):
        answer: int
    body=stream_bytes([chunk({'reasoning_content':'unfinished reasoning'})],done=False)
    monkeypatch.setattr('urllib.request.urlopen',lambda *a,**k:io.BytesIO(body))
    log=ModelCallLog()
    with pytest.raises(ModelClientError):
        call_structured(client(_urlopen_transport,reasoning_stream=True),request_for(),Answer,call_log=log)
    assert len(log.records)==1
    assert log.records[0].error
    assert log.records[0].reasoning_content=='unfinished reasoning'
    assert log.records[0].reasoning_observation['stream_complete'] is False


def test_summary_keeps_missing_counts_and_does_not_relabel_estimates_as_usage():
    from pilot01.experiment.scoring_v2 import account_usage
    calls=[dict(params={'model_id':'test'},reasoning_observation={'tokens':7,'token_source':'local_tokenizer_reencode',
        'reasoning_phase_seconds':1.5},reasoning_content='a'),dict(params={'model_id':'test'})]
    row={};account_usage(row,calls)
    assert row['reasoning_tokens'] is None
    assert row['reasoning_observation']['tokens'] is None
    assert row['reasoning_observation']['known_tokens']==7
    assert row['reasoning_observation']['tokens_missing_calls']==1
    assert row['reasoning_observation']['reasoning_phase_seconds'] is None


def test_top_level_native_count_fallback_when_nested_count_is_null():
    body=json.loads(envelope('{}'));body['usage'].update(reasoning_tokens=9,completion_tokens_details={'reasoning_tokens':None})
    r=client(lambda *_:json.dumps(body).encode()).generate(request_for())
    assert r.usage.reasoning_tokens==9
    assert r.reasoning_observation['tokens']==9
