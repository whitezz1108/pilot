"""Use the pre-imported production transport alias with a fake HTTP response."""
import ast
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import urllib.request
from pilot01.model.openai_compat import _urlopen_transport as pooled_transport_alias

root=Path(__file__).resolve().parent
script=root/'run_case0067.py'
wire=root/'capture_selftest'
wire.mkdir(exist_ok=False)
namespace={'Path':Path,'json':json,'datetime':datetime,'timezone':timezone,'wire':wire}
tree=ast.parse(script.read_text())
selected=[node for node in tree.body if isinstance(node,(ast.FunctionDef,ast.ClassDef)) and node.name in ('save','now','Tee','observed_urlopen')]
exec(compile(ast.Module(body=selected,type_ignores=[]),str(script),'exec'),namespace)
class Response(io.BytesIO):
    status=200
    headers={'Content-Type':'text/event-stream'}
payload={'stream':True,'messages':[{'role':'user','content':'synthetic capture test'}]}
chunks=[{'choices':[{'index':0,'delta':{'content':'{}'},'finish_reason':'stop'}]},
        {'choices':[],'sglext':{'output_ids':[[1,2,3]]}}]
body=''.join('data: '+json.dumps(x)+'\n\n' for x in chunks)+'data: [DONE]\n\n'
def fake_urlopen(req,*args,**kwargs):
    got=json.loads(req.data)
    assert got=={**payload,'return_output_ids_in_sglext':True}
    return Response(body.encode())
namespace['original_urlopen']=fake_urlopen
request=urllib.request.Request('http://127.0.0.1:1/not-sent',data=json.dumps(payload).encode(),headers={'Authorization':'Bearer synthetic-test'})
request.pilot01_model_call_id='capture_regression'
original=urllib.request.urlopen
urllib.request.urlopen=namespace['observed_urlopen']
try:
    result=pooled_transport_alias(request,1)
finally:
    urllib.request.urlopen=original
record=wire/'capture_regression'
assert (record/'response.sse').read_bytes()==body.encode()
assert json.loads((record/'completion.json').read_text())['stream_complete']
assert json.loads((record/'request_before_observation.json').read_text())==payload
assert json.loads((record/'request_metadata.json').read_text())['headers']['Authorization']=='[REDACTED]'
print(json.dumps({'passed':True,'network_requests':0,'preimported_transport_alias':True,
                  'raw_sse_bytes_identical':True,'output_ids_preserved':True,'request_and_headers_preserved_with_redaction':True}))
