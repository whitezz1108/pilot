"""Preserve idle old containers, deploy only the tested boundary patch on GPU0/1."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
BASE = 'sha256:e3a4f06001a29542fc4a8638536d9ffc5eb9c9942b08603c09b19a56eae4a1ae'
TAG = 'pilot-sglang:qwen-boundary-v1-20261102'
NAMES = ['pilot-native-gpu0-v1', 'pilot-native-gpu1-v1', 'pilot-native-seven-layer-v1']

def run(args, timeout=120):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(str(args[:3])+': '+p.stderr[-2000:])
    return p.stdout.strip()

def save(name, value):
    (ROOT/name).write_text(json.dumps(value, indent=2)+'\n')

def get(url):
    with urllib.request.urlopen(url, timeout=5) as f:
        return json.load(f)

def idle():
    s = get('http://127.0.0.1:8766/api/state')
    assert not s['busy'] and all(x['active'] == 0 for x in s['api_limits'])
    return s

test_stdout = (ROOT/'cpu_test.json').read_text()
test_result = json.loads(test_stdout[test_stdout.index('{\n  "passed":'):])
assert test_result['passed'] and all(x['passed'] for x in test_result['checks'])
save('cpu_test_result.json', test_result)
assert not (ROOT/'deployment.json').exists(), 'Do not repeat a deployment'
save('console_before.json', idle())
old = [json.loads(run(['docker','inspect',n]))[0] for n in NAMES]
save('containers_before.json', old)
save('gpu_before.json', {'nvidia_smi':run(['nvidia-smi','--query-gpu=index,uuid,power.limit,memory.used,temperature.gpu','--format=csv'])})
for i in (0,1):
    assert old[i]['Image'] == BASE
    cmd = old[i]['Config']['Cmd']
    for option, expected in [('--context-length','49152'),('--max-running-requests','4'),('--max-total-tokens','65536'),('--dtype','bfloat16'),('--speculative-algorithm','NEXTN')]:
        assert cmd[cmd.index(option)+1] == expected
    logs = subprocess.run(['docker','logs',NAMES[i]], capture_output=True, text=True, check=True)
    (ROOT/f'gpu{i}_before.stdout.log').write_text(logs.stdout)
    (ROOT/f'gpu{i}_before.stderr.log').write_text(logs.stderr)
base_tag = 'pilot-sglang:before-boundary-20261102'
run(['docker','tag',BASE,base_tag])
dockerfile = f'FROM {base_tag}\n'
for name, entry in json.loads((ROOT/'patch_manifest.json').read_text()).items():
    dockerfile += f'COPY after/{name} /sgl-workspace/sglang/python/sglang/srt/{entry["destination"]}\n'
dockerfile += 'LABEL pilot01.boundary.version="explicit-think-end-v1"\n'
(ROOT/'Dockerfile').write_text(dockerfile)
(ROOT/'.dockerignore').write_text('*\n!Dockerfile\n!after/\n!after/*.py\n')
# Containerd-backed Docker builders attempt remote resolution even for a local
# base. Build this two-file layer entirely locally, without running the donor.
builder='pilot-boundary-image-builder-20261102'
base_config=json.loads(run(['docker','image','inspect',BASE]))[0]['Config']
builder_id=run(['docker','create','--name',builder,BASE,'true'])
for name, entry in json.loads((ROOT/'patch_manifest.json').read_text()).items():
    run(['docker','cp',str(ROOT/'after'/name),
         builder+':/sgl-workspace/sglang/python/sglang/srt/'+entry['destination']])
commit=['docker','commit','--change','LABEL pilot01.boundary.version=explicit-think-end-v1']
if base_config.get('Cmd') is not None:
    commit += ['--change','CMD '+json.dumps(base_config['Cmd'])]
commit += [builder,TAG]
save('build.json', {'method':'local stopped donor container with two copied source files',
                   'builder':builder_id,'image':run(commit,timeout=300)})
new_image = run(['docker','image','inspect','--format','{{.Id}}',TAG])
idle()
deployments = []
save('deployment.json', {'stage':'starting', 'new_image':new_image,'deployments':deployments})
for i in (0,1):
    o = old[i]
    backup = NAMES[i]+'-before-boundary-20261102'
    assert not subprocess.run(['docker','inspect',backup], capture_output=True).returncode == 0
    argv = ['docker','run','-d','--name',NAMES[i],'--gpus',f'device={i}','--ipc=host',
            '-p',f'127.0.0.1:{30000+i}:30000']
    for m in o['Mounts']:
        assert m['Type'] == 'bind' and not m['RW']
        argv += ['--mount',f'type=bind,source={m["Source"]},target={m["Destination"]},readonly']
    for env in o['Config']['Env']:
        argv += ['-e',env]
    restart = o['HostConfig']['RestartPolicy']['Name']
    if restart != 'no':
        argv += ['--restart',restart]
    argv += [new_image, *o['Config']['Cmd']]
    run(['docker','stop','--time','30',NAMES[i]])
    run(['docker','rename',NAMES[i],backup])
    new_id = run(argv)
    deployments.append({'gpu':i,'old_container_id':o['Id'],'backup_name':backup,'new_container_id':new_id,'command':o['Config']['Cmd']})
    save('deployment.json', {'stage':'warming','new_image':new_image,'deployments':deployments})
    print(json.dumps({'stage':'warming','gpu':i,'container_id':new_id}), flush=True)
deadline = time.monotonic()+1200
ready = set()
while time.monotonic()<deadline:
    for i in (0,1):
        if i in ready:
            continue
        try:
            info = get(f'http://127.0.0.1:{30000+i}/get_server_info')
        except OSError:
            continue
        assert info['context_length']==49152 and info['max_running_requests']==4
        caps = [x['max_total_num_tokens'] for x in [info,*info.get('internal_states',[])] if 'max_total_num_tokens' in x]
        assert caps and min(caps)==65536
        save(f'gpu{i}_server_info_after.json',info)
        ready.add(i)
        print(json.dumps({'stage':'ready','gpu':i}),flush=True)
    if ready=={0,1}:
        break
    time.sleep(5)
assert ready=={0,1}, 'startup timeout; retain evidence and do not run experiment'
after = [json.loads(run(['docker','inspect',n]))[0] for n in NAMES]
assert after[2]['Id']==old[2]['Id']
for i in (0,1):
    for k in ('Cmd','Entrypoint'):
        assert after[i]['Config'][k]==old[i]['Config'][k], k
    before_env=dict(x.split('=',1) for x in old[i]['Config']['Env'])
    after_env=dict(x.split('=',1) for x in after[i]['Config']['Env'])
    assert before_env==after_env
save('containers_after.json',after)
save('deployment.json', {'stage':'ready','new_image':new_image,'deployments':deployments,
    'commands_and_environment_unchanged':True,'gpu2_unchanged':True})
print('READY',flush=True)
