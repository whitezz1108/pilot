"""Seal the single actual attempt and its before/after evidence; never rerun."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

root=Path(__file__).resolve().parent
attempt=root/'attempt2'
def read(p): return json.loads(p.read_text())
def rows(p): return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,data): p.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
mission=read(attempt/'run_mission.json')
assert mission['stage']=='terminal'
state=read(attempt/'final_state.json')
assert not state['busy'] and all(x['active']==0 for x in state['api_limits'])
runtime=attempt/'runtime'
raw=runtime/'outputs/experiment_console/raw'/mission['experiment_id']/mission['run_id']
after=read(raw/'run.json')
before=read(attempt/'before_case0067/raw/run.json')
assert after['execution']['status'] in ('completed','failed')
same_keys=('case_id','contract_id','condition_id','error_condition','source_access','verification_required',
           'repeat_index','model_fingerprint','runtime_config','profile_fingerprint')
for k in same_keys:
    assert after[k]==before[k],k
assert after['execution']['contract_text_hash']==before['execution']['contract_text_hash']
baseline=read(attempt/'application_freeze.json')
for rel,value in baseline['files_sha256'].items():
    assert sha(runtime/rel)==value,rel
    assert sha(Path(baseline['runtime_root'])/rel)==value,rel
for rel,value in read(attempt/'before_case0067/sha256.json').items():
    assert sha(attempt/'before_case0067'/rel)==value,rel
calls=rows(raw/'model_calls.jsonl')
tools=rows(raw/'tool_calls.jsonl')
oldcalls=rows(attempt/'before_case0067/raw/model_calls.jsonl')
audit=read(attempt/'wire_boundary_audit.json')
assert len(audit['calls'])==len(calls), 'Every model response needs a raw-token audit'
assert {x['model_call_id'] for x in audit['calls']}=={x['model_call_id'] for x in calls}
def count_native(call):
    resp=call.get('provider_response') or {}
    message=((resp.get('choices') or [{}])[0].get('message') or {})
    return len(message.get('tool_calls') or [])
role_counts={}
for role in ('manager','compliance'):
    rc=[c for c in calls if c['role']==role]
    rt=[t for t in tools if t.get('role')==role or t.get('node')==role]
    role_counts[role]={'model_calls':len(rc),'native_requests':sum(count_native(c) for c in rc),
                      'tool_log_rows':len(rt),'tool_successes':sum(bool(t.get('ok')) for t in rt),
                      'tool_refusals':sum(not bool(t.get('ok')) for t in rt),
                      'has_final_output':after['execution'].get(role+'_output') is not None,
                      'format_repairs':sum(bool(c.get('is_repair')) for c in rc),
                      'finish_reasons':dict(Counter(c.get('finish_reason') for c in rc))}
names=['pilot-native-gpu0-v1','pilot-native-gpu1-v1','pilot-native-seven-layer-v1']
containers=[]
for name in names:
    container=json.loads(subprocess.check_output(['docker','inspect',name],text=True))[0]
    containers.append(container)
    if name!=names[2]:
        logs=subprocess.run(['docker','logs',name],capture_output=True,text=True,check=True)
        (root/(name+'_after.stdout.log')).write_text(logs.stdout)
        (root/(name+'_after.stderr.log')).write_text(logs.stderr)
save(root/'containers_final.json',containers)
started=read(root/'containers_after.json')
for a,b in zip(containers,started):
    assert a['Id']==b['Id'] and a['Image']==b['Image'] and a['Config']['Cmd']==b['Config']['Cmd']
outcome={'status':after['execution']['status'], 'case':'CASE-0067/E1/A1V0D0/repeat0',
         'experiment_id':mission['experiment_id'],
         'earlier_validation_attempt':read(root/'attempt1_collector_failure.json'),
         'validation_attempts_count':2,'collector_failure_attempts':1,
         'completed_validation_workflows':int(after['execution']['status']=='completed'),
         'before_status':before['execution']['status'],'before_multi_call_responses':sum(count_native(c)>1 for c in oldcalls),
         'after_multi_call_responses':sum(count_native(c)>1 for c in calls),
         'tool_refusals':[{'node':t.get('node'),'tool':t.get('tool'),'error':t.get('error')} for t in tools if not t.get('ok')],
         'roles':role_counts,'total_model_calls':len(calls),'total_tool_log_rows':len(tools),
         'before_after_application_and_model_settings_equal':True,
         'frozen_files_unchanged':len(baseline['files_sha256']),
         'actual_token_mask_replay_passed':audit['passed'],
         'requested_concurrency_limit':4,'actual_selected_workflows':1,
         'started_at':mission['session']['created_at'],'finished_at':mission['session']['finished_at'],
         'duration_seconds':(datetime.fromisoformat(mission['session']['finished_at'])-datetime.fromisoformat(mission['session']['created_at'])).total_seconds(),
         'execution_completed_is_not_judgment_accuracy':True,
         'serving_version':'explicit-think-end-v1',
         'historical_cause_limit':'Historical failed response lacks raw generation tokens; CPU mismatch reproduction is not proof of its exact token path.',
         'live_audit_limit':'Actual output tokens replayed offline; no per-step internal scheduler masks were instrumented.',
         'sample_limit':'One targeted workflow, not a 4-workflow concurrency benchmark or reliability estimate.'}
save(root/'result_summary.json',outcome)
(root/'README.txt').write_text(
    'CASE-0067 边界修复与完整流程验证\n\n'
    '修复：Qwen3 仅以显式 </think> 结束思考；解码器在思考阶段屏蔽工具控制符和 EOS；'
    '正文继续沿用一份最终 JSON 或一次当前授权工具调用的语法。工具解析只接收思考结束后的正文。\n'
    '只改两个服务端源文件；before/、after/、*.patch 与 patch_manifest.json 保存完整差异。'
    '旧容器保留，新镜像部署到 GPU0/1，GPU2 未参与验证。\n'
    '实验的 194 个冻结文件、模型采样参数、提示词、工具/输出预算、数据指纹与 GPU 分配均已核对。'
    '并发上限仍为 4，本次只选一轮，不能当作四轮同时运行的并发测试。\n\n'
    '验证记录有两次：第一次由于新采集器接入错误，在第一轮 SSE 读取前失败；原始运行及错误已保留，'
    '详见 attempt1_collector_failure.json。修复采集器并通过无网络独立回归检查后，启动独立第二批次。\n'
    f'完整验证批次：{mission["experiment_id"]}\n'
    f'状态：{outcome["status"]}；耗时：{outcome["duration_seconds"]:.3f} 秒。\n'
    f'Manager：{role_counts["manager"]["model_calls"]} 次模型响应，{role_counts["manager"]["native_requests"]} 次原生工具请求。\n'
    f'Compliance：{role_counts["compliance"]["model_calls"]} 次模型响应，{role_counts["compliance"]["native_requests"]} 次原生工具请求。\n'
    f'实际生成 token 的离线语法回放通过：{audit["passed"]}。\n\n'
    '第二次完整验证的逐调用请求、原始 HTTP/SSE、生成 token ID、解析前生成文本在 attempt2/wire/；'
    '工作流、工具、核验、事件及最终输出在 attempt2/runtime/outputs/experiment_console/。'
    '修改前 CASE-0067 的已记录完整资料在 before_case0067/。\n'
    '历史失败没有保存生成 token，不能据此声称已证明其唯一生成路径；本次 token 回放也不是内部调度器逐步日志。'
    '单例完整流程完成与业务判断正确性不同，也不构成所有实验永不失败的保证。\n'
    'evidence_manifest.json 为逐文件 SHA256；压缩包旁的 evidence_bundle.sha256 校验整个包。\n',encoding='utf-8')
files={}
excluded={'evidence_bundle.zip','evidence_bundle.sha256','evidence_manifest.json'}
for p in sorted(root.rglob('*')):
    if p.is_file() and not p.is_symlink() and p.name not in excluded and '__pycache__' not in p.parts:
        files[str(p.relative_to(root))]={'sha256':sha(p),'bytes':p.stat().st_size}
save(root/'evidence_manifest.json',{'sealed_at':datetime.now(timezone.utc).isoformat(),'files':files})
with zipfile.ZipFile(root/'evidence_bundle.zip','w',zipfile.ZIP_DEFLATED) as z:
    for rel in files:
        z.write(root/rel,rel)
    z.write(root/'evidence_manifest.json','evidence_manifest.json')
(root/'evidence_bundle.sha256').write_text(sha(root/'evidence_bundle.zip')+'  evidence_bundle.zip\n')
print(json.dumps(outcome,ensure_ascii=False,indent=2))
