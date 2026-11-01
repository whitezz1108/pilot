import json
from test_validator_v3 import setup_tools,TARGETS
from pilot01.workflow.nodes.tool_loop import render_tool_result
from pilot01.model.client import ModelMessage
from pilot01.model.openai_compat import OpenAICompatibleClient
from test_tool_loop import build_request

def rendered(outcome):
    text=render_tool_result(outcome)
    # Exercise the message/JSON transport boundary rather than just the backend.
    client=OpenAICompatibleClient(base_url='http://offline.invalid/v1',api_key='offline-placeholder',transport=lambda *args: (_ for _ in ()).throw(AssertionError('network forbidden')))
    request=build_request((ModelMessage(role='user',content=text),))
    wire=json.loads(client.build_request(request).data)
    return text,json.loads(wire['messages'][0]['content'].split('\n',3)[3])

def test_cursor_survives_message_and_opens_next_page():
    _,tools=setup_tools()
    first=tools.search_contract('administrative',target_category=TARGETS[0])
    _,p=rendered(first)
    assert p==first.payload and p['has_more'] and p['next_cursor']
    second=tools.search_contract(p['query'],target_category=p['target_category'],cursor=p['next_cursor'])
    assert second.ok and set(first.result_ids).isdisjoint(second.result_ids)
    while second.payload['has_more']:
        _,p=rendered(second)
        second=tools.search_contract(p['query'],target_category=p['target_category'],cursor=p['next_cursor'])
    _,p=rendered(second)
    assert p['has_more'] is False and p['next_cursor'] is None

def test_repeat_page_is_not_no_match():
    _,tools=setup_tools()
    first=tools.search_contract('administrative',target_category=TARGETS[0])
    repeat=tools.search_contract('administrative',target_category=TARGETS[0])
    text,p=rendered(repeat)
    assert p['repeat_page'] and p['notice'] and p['results']==[]
    assert p['next_cursor']==first.payload['next_cursor']
    assert 'no paragraph of this contract matched' not in text

def test_true_no_match_retains_metadata():
    _,tools=setup_tools()
    _,p=rendered(tools.search_contract('xyznevermatchabc',target_category=TARGETS[0]))
    assert p['total_matching']==0 and not p['has_more'] and not p['repeat_page']
