"""One complete frozen CASE-0067 workflow; only extra output-token/wire observation."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = Path(__file__).resolve().parent
FROZEN = Path('/home/evan/ai/frozen/pilot01-v4-full96-20261102T045241')
OLD = 'console-factorial-20261102T050600-20a1e40c'
OLD_RID = OLD+'__CASE-0067__E1__A1V0D0__r000'
ROOT = HERE/'runtime'

def save(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')

def now():
    return datetime.now(timezone.utc).isoformat()

def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

assert json.loads((HERE/'deployment.json').read_text())['stage']=='ready'
assert not (HERE/'run_mission.json').exists(), 'Never launch a duplicate attempt'
source_proof={}
for gpu in (0,1):
    hashes={}
    for name,entry in json.loads((HERE/'patch_manifest.json').read_text()).items():
        path='/sgl-workspace/sglang/python/sglang/srt/'+entry['destination']
        value=subprocess.check_output(['docker','exec',f'pilot-native-gpu{gpu}-v1','sha256sum',path],text=True).split()[0]
        assert value==entry['after_sha256'],(gpu,name)
        hashes[name]=value
    source_proof[str(gpu)]=hashes
save(HERE/'active_engine_source_proof.json',source_proof)
with urllib.request.urlopen('http://127.0.0.1:8766/api/state',timeout=5) as r:
    state = json.load(r)
assert not state['busy'] and all(x['active']==0 for x in state['api_limits'])
baseline = json.loads((FROZEN/'outputs/experiment_console_watch'/OLD/'freeze_manifest.json').read_text())
ROOT.mkdir(exist_ok=False)
for rel, sha in baseline['files_sha256'].items():
    src = FROZEN/rel
    assert digest(src)==sha, rel
    dst = ROOT/rel
    dst.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(src,dst)
for name in ('data','.venv'):
    (ROOT/name).symlink_to((FROZEN/name).resolve(),target_is_directory=True)
save(HERE/'application_freeze.json',baseline)
oldraw=FROZEN/'outputs/experiment_console/raw'/OLD/OLD_RID
shutil.copytree(oldraw,HERE/'before_case0067'/'raw')
shutil.copytree(FROZEN/'outputs/experiment_console/manifests'/OLD,HERE/'before_case0067'/'manifests')
save(HERE/'before_case0067'/'sha256.json',{
    str(p.relative_to(HERE/'before_case0067')):digest(p)
    for p in (HERE/'before_case0067').rglob('*') if p.is_file()})
sys.path.insert(0,str(ROOT/'src'))
os.environ['PYTHONDONTWRITEBYTECODE']='1'
from pilot01.experiment.console import Console
from pilot01.model import openai_compat as oc

# Record the actual request and byte-exact HTTP/SSE response before parsing.
# This is observation only; no retries, repairs or changes to model messages.
wire = HERE/'wire'
wire.mkdir()
original_urlopen = urllib.request.urlopen

class Tee:
    def __init__(self, handle, path, request):
        self.handle,self.path=handle,path
        self.request=request
        self.file=(path/'response.sse').open('wb')
        save(path/'response_headers.json', {'status':handle.status,'headers':dict(handle.headers)})
    def __enter__(self):
        return self
    def __exit__(self,*args):
        self.file.close()
        self.handle.close()
        cap=getattr(self.request,'pilot01_stream_capture',None)
        save(self.path/'completion.json',{'finished_at':now(),'stream_complete':bool(cap and cap.done),
                                        'sse_events':len(cap.events) if cap else 0})
    def __iter__(self):
        for line in self.handle:
            self.file.write(line)
            self.file.flush()
            yield line
    def read(self,*args):
        data=self.handle.read(*args)
        self.file.write(data)
        self.file.flush()
        return data
    def __getattr__(self,name):
        return getattr(self.handle,name)

def observed_urlopen(req,*args,**kwargs):
    mid=getattr(req,'pilot01_model_call_id',None)
    if mid is None:
        return original_urlopen(req,*args,**kwargs)
    path=wire/mid
    path.mkdir(exist_ok=False)
    payload=json.loads(req.data)
    save(path/'request_before_observation.json',payload)
    assert 'return_output_ids_in_sglext' not in payload
    payload['return_output_ids_in_sglext']=True
    req.data=json.dumps(payload).encode()
    save(path/'request.json',payload)
    save(path/'request_metadata.json',{'url':req.full_url,'model_call_id':mid,'started_at':now(),
         'timeout':kwargs.get('timeout'),'only_added_parameter':'return_output_ids_in_sglext',
         'headers':{k:('[REDACTED]' if k.lower()=='authorization' else v) for k,v in req.header_items()}})
    response=original_urlopen(req,*args,**kwargs)
    return Tee(response,path,req)

urllib.request.urlopen=observed_urlopen
app=Console(root=ROOT)
assert app.profile.fingerprint()==baseline['profile_fingerprint']
selection={'a':['A1'],'v':['V0'],'d':['D0'],'e':['E1'],'cohort':'new40','repeat':1,'case_ids':['CASE-0067']}
draft=app.prepare(selection)
assert draft['total']==1
assert app.draft.registry_fingerprint==baseline['registry_fingerprint']
job=draft['jobs'][0]
assert job['case_id']=='CASE-0067' and job['condition']=='A1V0D0' and job['memo']=='E1' and job['repeat']==1
mission={'stage':'prepared','old_batch':OLD,'old_run':OLD_RID,'experiment_id':draft['draft_id'],
         'run_id':job['run_id'],'runtime_root':str(ROOT),'created_at':now(),
         'application_profile_unchanged':baseline['profile_fingerprint'],
         'serving_patch':'explicit-think-end-v1','concurrency_limit':4,
         'selected_runs':1,'manager_gpu':0,'compliance_gpu':1,'pid':os.getpid()}
save(HERE/'run_mission.json',mission)
started=app.start({'draft_id':draft['draft_id'],'run_ids':[job['run_id']],
                 'concurrency':4,'manager_gpus':[0],'compliance_gpus':[1]})
manifest_dir=ROOT/'outputs/experiment_console/manifests'/draft['draft_id']
save(manifest_dir/'serving_boundary_patch.json',{
    'version':'explicit-think-end-v1',
    'deployment':json.loads((HERE/'deployment.json').read_text()),
    'source_patch':json.loads((HERE/'patch_manifest.json').read_text()),
    'application_profile_note':'Application source/config unchanged; serving-engine revision is identified separately here.',
    'observation_only_parameter':{'return_output_ids_in_sglext':True},
    'before_run':OLD_RID})
mission.update(stage='running',started=started)
save(HERE/'run_mission.json',mission)
print(json.dumps(started),flush=True)
while app.busy():
    save(HERE/'latest_state.json',app.state())
    with (HERE/'observations.jsonl').open('a') as f:
        f.write(json.dumps({'at':now(),'session':app.session,'activity':app.activity})+'\n')
    # Read-only GPU telemetry is independent of all model inputs.
    gpu=subprocess.run(['nvidia-smi','--query-gpu=index,timestamp,power.draw,power.limit,memory.used,memory.total,temperature.gpu,utilization.gpu','--format=csv,noheader'],capture_output=True,text=True)
    with (HERE/'gpu_samples.csv').open('a') as f:
        f.write(gpu.stdout)
    app.thread.join(timeout=10)
save(HERE/'final_state.json',app.state())
mission.update(stage='terminal',finished_at=now(),session=app.session)
save(HERE/'run_mission.json',mission)
print(json.dumps(mission),flush=True)
