"""Opt-in generation constraints. Never rewrite a model response or tool arguments."""
import atexit,json,os,shlex,subprocess,threading
from pilot01.schemas import ManagerOutputV2,ComplianceOutputV2
_worker=None
_lock=threading.Lock()
_cache={}
TARGETS=['Change Of Control','Termination For Convenience']
def object_schema(properties,required=None):
    return {'type':'object','properties':properties,'required':list(properties) if required is None else required,'additionalProperties':False}
def build_schema(messages,role,final_only=False):
    if role not in ['manager','compliance']:raise ValueError('unsupported protocol role')
    final=(ManagerOutputV2 if role=='manager' else ComplianceOutputV2).model_json_schema()
    final['properties']['target_clause_status']=object_schema({k:{'type':'string','enum':['present','absent','unknown']} for k in TARGETS})
    definitions=final.pop('$defs',{})
    if final_only:return dict(final,**{'$defs':definitions})
    def tool(name,args):return object_schema({'tool_request':object_schema({'tool':{'const':name},'arguments':args})})
    bindings={}
    available=any('You may use these source tools:' in m['content'] for m in messages)
    branches=[final]
    if available:
        branches.extend([tool('search_contract',object_schema({'query':{'type':'string','minLength':1},'target_category':{'enum':TARGETS}})),tool('open_source_span',object_schema({'paragraph_id':{'type':'string','minLength':1}}))])
        for m in messages:
            if m['role']!='user' or not m['content'].startswith('TOOL RESULT\ntool: search_contract\nstatus: ok\n'):continue
            page=json.JSONDecoder().raw_decode(m['content'].split('\n',3)[3])[0]
            cursor=page.get('next_cursor')
            if page.get('has_more') and cursor:
                pair=(page['query'],page['target_category'])
                if cursor in bindings and bindings[cursor]!=pair:raise ValueError('conflicting cursor binding')
                bindings[cursor]=pair
        for cursor,(query,target) in sorted(bindings.items()):
            branches.append(tool('search_contract',object_schema({'query':{'const':query},'target_category':{'const':target},'cursor':{'const':cursor}})))
    return {'$defs':definitions,'anyOf':branches}
def compile_grammar(schema):
    global _worker
    key=json.dumps(schema)
    with _lock:
        if key in _cache:return _cache[key]
        if _worker is None:
            _worker=subprocess.Popen(shlex.split(os.environ['PILOT01_PROTOCOL_COMPILER']),stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,bufsize=1)
            atexit.register(_close)
        _worker.stdin.write(key+'\n');_worker.stdin.flush()
        result=json.loads(_worker.stdout.readline())
        if 'error' in result:raise RuntimeError(result['error'])
        _cache[key]=result['grammar'];return result['grammar']
def _close():
    if _worker is not None:
        _worker.stdin.close()
        try:_worker.wait(timeout=20)
        except subprocess.TimeoutExpired:_worker.terminate()
