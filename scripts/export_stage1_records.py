"""Package all existing Stage 1 records. Offline only; preserves source bytes."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'outputs/stage1_all_records_20261001'
BATCH = 'pilot01_main_v1_20260924'
MAIN = ROOT / 'outputs/pilot01_main_v1'
RAW_NAMES = {'run.json', 'events.jsonl', 'model_calls.jsonl', 'tool_calls.jsonl', 'verification.jsonl'}
REPORTS = (
    'phase1_architecture_audit.md', 'phase1_gap_analysis.md', 'phase1_minimal_fix_plan.md',
    'phase1_requirement_matrix.md', 'pilot_execution_report.md', 'pilot_quality_check.md',
    'failure_analysis.md', 'research_report.md', 'presentation_summary.md',
    'pilot01_phase2_phase3_full_outcomes.md', 'sentinel_case_review_material.md',
    'sentinel_full_contract_texts.md',
)


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def lines(path):
    # U+2028 may occur within a JSON string: only LF separates JSONL records.
    return [json.loads(s) for s in path.read_text(encoding='utf-8-sig').split('\n') if s.strip()]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding='utf-8')


def write_json(path, value):
    write(path, dump(value) + '\n')


def block(value, language='json'):
    content = value if isinstance(value, str) else dump(value)
    n = max([len(m.group()) for m in re.finditer(r'`+', content)] + [3]) + 1
    fence = '`' * n
    return f'{fence}{language}\n{content}\n{fence}\n'


def flag(value):
    return 'N/A' if value is None else ('是' if value else '否')


def copy(source, relative=None):
    destination = OUT / (relative or source.relative_to(ROOT))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and sha(source) == sha(destination):
        return destination
    shutil.copy2(source, destination)
    assert sha(source) == sha(destination), source
    return destination


def copy_tree(source):
    for path in sorted(source.rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts:
            copy(path)


def runtime_cause(run):
    failure = run['failure']
    error = failure.get('error') or ''
    if not failure['protocol_failure']:
        return ''
    if failure['failure_type'] == 'ToolLoopError':
        return '工具调用超限'
    if 'evidence_not_opened' in error:
        return '核验未通过：未自行打开证据'
    if 'output limit' in error or "finish_reason='length'" in error:
        return '输出截断'
    if '10054' in error:
        return '模型连接重置'
    return failure.get('failure_type') or '未分类运行失败'


def describe(score, run):
    if not score['completed']:
        return f'运行失败：{runtime_cause(run)}；中断节点 {score["failure_stage"]}，无有效最终动作。'
    answer = '最终动作正确' if score['final_action_correct'] else '最终动作与冻结 gold 不符'
    if score['error_condition'] == 'E1' and not score['is_negative_sentinel']:
        evidence = bool(score['manager_corrected_with_evidence'] or score['compliance_corrected_with_evidence'])
        recovery = '至少一个节点恢复目标事实并有与 gold 重叠的自开证据' if evidence else '未达到有证据遗漏恢复标准'
        return f'{answer}：{score["final_decision"]}（gold={score["gold_action"]}）；{recovery}。'
    return f'{answer}：{score["final_decision"]}（gold={score["gold_action"]}）；本轮为 E0，有证据遗漏恢复指标不适用。'


def render_run(score, run, case, logs, number):
    run_id = score['run_id']
    execution = run['execution']
    events = logs['events.jsonl']
    memo = next(e['payload']['output'] for e in events if e['node'] == 'analyst' and e['event_type'] == 'agent_output_received')
    inputs = {e['node']: e['payload']['input'] for e in events if e['event_type'] == 'agent_input_created'}
    info = {k: v for k, v in run.items() if k not in ('execution', 'model_calls', 'tool_calls', 'verifications')}
    info['execution_metadata'] = {k: v for k, v in execution.items() if k not in ('manager_output', 'compliance_output', 'events')}
    doc = [f'# 第 {number:03d} 轮｜{score["case_id"]}｜{score["error_condition"]} / {score["condition_id"]} / r{score["repeat_index"]:03d}\n',
           f'> {describe(score, run)}\n',
           f'run_id：`{run_id}`。本文件由已有记录离线整理，原始日志保留在包内 `outputs/pilot01_main_v1/raw/{BATCH}/{run_id}/`。',
           '\n一轮对应一个唯一 run_id。下列中文说明为导读；模型输入与输出保留原文。失败轮只记录已发生的阶段；缺失字段不补写。\n',
           '## 1. 条件、运行信息与结果\n',
           f'- 主目标：{score["target_category"]}；阴性哨兵：{flag(score["is_negative_sentinel"])}。',
           f'- 运行状态：{score["run_status"]}；最终动作：{score["final_decision"] or "无有效最终动作"}；gold：{score["gold_action"]}。',
           f'- 模型调用：{score["model_calls"]}；工具调用：{score["tool_calls"]}；记录的总 tokens：{score["total_tokens"]}。',
           '\n### 原始运行元数据\n', block(info),
           '## 2. 冻结 gold 与目标证据\n',
           '本节供事后复核，不代表模型运行时获得了 gold 或合同全文。正确性按冻结 gold 计分；最终动作正确与有证据遗漏恢复是不同指标。\n',
           block({k: case[k] for k in ('target_category', 'gold_target_clause_status', 'gold_evidence_offsets', 'is_negative_sentinel', 'policy', 'omission_target_claim_id')}),
           '### gold 原文片段\n']
    spans = case['gold_evidence_offsets']
    if not spans:
        doc.append('此案例没有正例 gold 证据片段；不能把无片段解释成模型已证明不存在。\n')
    for start, end in zip(spans[::2], spans[1::2]):
        doc += [f'字符偏移 [{start}, {end})：\n', block(case['contract_text'][start:end], 'text')]
    doc += ['## 3. 本轮实际 Analyst 备忘录\n',
            'Analyst 为离线冻结备忘录加载。E0 使用完整 memo，E1 删除指定目标 claim 及其附带证据引用。\n', block(memo)]
    if score['error_condition'] == 'E1':
        doc += ['### 被删除的 claim（供读者对照）\n',
                block([c for c in case['memo']['claims'] if c['claim_id'] == case['omission_target_claim_id']])]
    for n, role in ((4, 'manager'), (5, 'compliance')):
        doc += [f'## {n}. {role.title()} 完整受限输入与有效输出\n', '### 输入视图\n',
                block(inputs[role]) if role in inputs else '此节点未启动。\n', '### 工作流接受的输出\n',
                block(execution[role + '_output']) if execution.get(role + '_output') else
                '未产生被工作流接受的输出。候选模型响应（若有）仍完整保留在第 6 节。\n']
    doc += ['## 6. 全部模型调用：完整提示词与原始响应\n',
            '按原始调用顺序排列，包含工具请求、格式修复和错误。rendered_input 包括当次已记录的上下文；raw_response 是实际保存的响应，不包含服务端未返回的内部推理。\n']
    for n, call in enumerate(logs['model_calls.jsonl'], 1):
        doc += [f'### 调用 {n}｜{call["role"]}｜call_index={call["call_index"]}\n',
                '#### 参数、时间、用量、工具请求与解析结果\n',
                block({k: v for k, v in call.items() if k not in ('rendered_input', 'raw_response')}),
                '#### 完整 rendered_input\n', block(call['rendered_input'], 'text'),
                '#### 完整 raw_response\n', block(call['raw_response'] if call['raw_response'] is not None else 'null', 'text')]
    doc += ['## 7. 全部工具调用\n', block(logs['tool_calls.jsonl']),
            '工具日志保留参数、结果段落 ID 与状态；模型实际看到的工具返回上下文保留在后续调用的 rendered_input。\n',
            '## 8. 全部核验记录\n', block(logs['verification.jsonl']),
            '## 9. 失败信息与完整事件流\n', block(run['failure']), block(events),
            '## 10. 完整逐轮评分\n', block(score),
            '## 11. 来源与附件\n',
            f'- [原始 run.json](../../outputs/pilot01_main_v1/raw/{BATCH}/{run_id}/run.json)',
            f'- [案例库条目](../../cases/{score["case_id"]}/case_registry_entry.json)',
            f'- [合同全文](../../cases/{score["case_id"]}/contract_full.txt)',
            f'- [原始评分表](../../outputs/pilot01_main_v1/processed/{BATCH}/run_scores.jsonl)',
            '\n## 12. 合同全文\n',
            '全文用于读者复核。A0V0 的模型没有原文访问权；A1 条件通过工具取得局部原文，不等于阅读过合同全文。\n',
            block(case['contract_text'], 'text')]
    relative = Path('readable_runs') / score['case_id'] / (run_id + '.md')
    write(OUT / relative, '\n'.join(doc) + '\n')
    return relative.as_posix(), memo


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    scores = lines(MAIN / 'processed' / BATCH / 'run_scores.jsonl')
    plan = read(ROOT / 'data/pilot01/main_v1/run_plan_v1.json')
    registry = read(ROOT / 'data/pilot01/main_v1/case_registry_v1.json')
    freeze = read(ROOT / 'data/pilot01/main_v1/experiment_manifest.yaml')
    cases = {c['case_id']: c for c in registry['cases']}
    ids = {r['run_id'] for r in scores}
    planned = {j['run_id'] for j in plan['jobs']}
    actual = {p.name for p in (MAIN / 'raw' / BATCH).iterdir() if p.is_dir()}
    assert len(scores) == len(ids) == len(plan['jobs']) == 648
    assert ids == planned == actual
    raw_inventory = list(csv.DictReader((MAIN / 'raw_artifact_sha256_v1.csv').open(encoding='utf-8-sig')))
    assert len(raw_inventory) == 3240
    for entry in raw_inventory:
        source = ROOT / entry['path']
        assert source.is_file() and sha(source) == entry['sha256'], entry['path']
        assert source.stat().st_size == int(entry['bytes'])
    copy_tree(MAIN)
    copy_tree(ROOT / 'outputs/pilot01_preflight_v1')
    copy_tree(ROOT / 'data/pilot01/main_v1')
    freeze_checks = []
    for source_name, frozen in freeze['files'].items():
        source = ROOT / source_name
        assert source.is_file(), source_name
        relative = Path('docs') / source.name if Path(source_name).is_absolute() else Path(source_name)
        digest = sha(source)
        freeze_checks.append({'source': source_name, 'packaged_path': relative.as_posix(),
                              'frozen_sha256': frozen['sha256'], 'current_sha256': digest,
                              'matches_frozen': digest == frozen['sha256']})
        copy(source, relative)
    mismatches = [r['packaged_path'] for r in freeze_checks if not r['matches_frozen']]
    assert mismatches == ['src/pilot01/experiment/artifacts.py'], mismatches
    for folder in ('src', 'prompts'):
        copy_tree(ROOT / folder)
    for name in REPORTS:
        copy(ROOT / 'outputs/reports' / name)
    for name in ('analyze_formal_pilot.py', 'write_formal_pilot_reports.py', 'export_stage1_records.py'):
        copy(ROOT / 'scripts' / name)
    copy(ROOT / 'data/manifest.md')
    for name in ('pilot01_main_v1_20260924_frozen.manifest.json', 'pilot01_main_v1_20260924_frozen.zip.sha256'):
        copy(ROOT / 'outputs/archives' / name, Path('provenance') / name)
    for case_id, case in cases.items():
        write_json(OUT / 'cases' / case_id / 'case_registry_entry.json', case)
        write(OUT / 'cases' / case_id / 'contract_full.txt', case['contract_text'])
        write_json(OUT / 'cases' / case_id / 'memo_E0.json', case['memo'])
        if 'E1' in case['error_conditions']:
            memo = {**case['memo'], 'claims': [c for c in case['memo']['claims'] if c['claim_id'] != case['omission_target_claim_id']]}
            write_json(OUT / 'cases' / case_id / 'memo_E1.json', memo)
    print('Source files copied; frozen raw hashes verified.', flush=True)
    index_rows, run_index = [], []
    failures, totals = Counter(), Counter()
    safe_patterns = [re.compile(r'\bsk-[A-Za-z0-9_-]{16,}'), re.compile(r'\bghp_[A-Za-z0-9]{20,}'), re.compile(r'\bAKIA[0-9A-Z]{16}\b')]
    for number, score in enumerate(sorted(scores, key=lambda r: r['run_id']), 1):
        directory = MAIN / 'raw' / BATCH / score['run_id']
        assert {p.name for p in directory.iterdir()} == RAW_NAMES
        run = read(directory / 'run.json')
        assert run['run_id'] == score['run_id']
        execution = run['execution']
        case = cases[score['case_id']]
        assert 'sha256:' + hashlib.sha256(case['contract_text'].encode('utf-8')).hexdigest() == execution['contract_text_hash']
        logs = {name: lines(directory / name) for name in RAW_NAMES if name.endswith('.jsonl')}
        assert logs['events.jsonl'] == execution['events']
        assert len(logs['model_calls.jsonl']) == score['model_calls']
        assert len(logs['tool_calls.jsonl']) == score['tool_calls']
        for call in logs['model_calls.jsonl']:
            serialized = json.dumps(call)
            assert not any(p.search(serialized) for p in safe_patterns), f'Credential-shaped text: {score["run_id"]}'
        totals['model_calls'] += len(logs['model_calls.jsonl'])
        totals['tool_calls'] += len(logs['tool_calls.jsonl'])
        relative, memo = render_run(score, run, case, logs, number)
        expected = case['memo']['claims']
        if score['error_condition'] == 'E1':
            expected = [c for c in expected if c['claim_id'] != case['omission_target_claim_id']]
        assert memo['claims'] == expected
        cause = runtime_cause(run)
        if cause:
            failures[cause] += 1
        row = {k: score[k] for k in ('run_id', 'case_id', 'contract_id', 'target_category', 'is_negative_sentinel',
                                    'error_condition', 'condition_id', 'repeat_index', 'run_status', 'final_decision',
                                    'gold_action', 'final_action_correct', 'correction_stage', 'failure_type', 'failure_stage')}
        row.update({'number': number, 'readable_record': relative, 'failure_cause': cause,
                    'raw_directory': f'outputs/pilot01_main_v1/raw/{BATCH}/{score["run_id"]}'})
        run_index.append(row)
        category = 'COC' if score['target_category'] == 'Change Of Control' else 'TFC'
        status = '运行失败：' + cause if cause else ('动作正确' if score['final_action_correct'] else '动作不符')
        index_rows.append(f'| [{number:03d}]({relative}) | {score["case_id"]} | {category} | {score["error_condition"]} | {score["condition_id"]} | r{score["repeat_index"]:03d} | {status} | {score["final_decision"] or "N/A"} |')
        if number % 100 == 0:
            print(f'Rendered and checked {number}/648 rounds.', flush=True)
    assert totals == {'model_calls': 4424, 'tool_calls': 3155}
    assert sum(failures.values()) == 58
    write_json(OUT / 'run_index.json', run_index)
    write(OUT / '01_全量记录索引.md', '\n'.join([
        '# Stage 1 全量逐轮记录索引\n',
        '共 648 轮，按 run_id 排序。一轮一份 Markdown；点击编号打开。COC 为控制权变更，TFC 为任意终止。SENT 为阴性哨兵，POS 为正例。\n',
        '“动作正确”与“有证据遗漏恢复”分别计量；运行失败的最终动作记为 N/A。更多说明见 [交付说明](00_交付说明.md)。\n',
        '| 编号 | 案例 | 主目标 | E | 条件 | 重复 | 结果 | 最终动作 |\n|---|---|---|---|---|---|---|---|', *index_rows]) + '\n')
    preflight = lines(ROOT / 'outputs/pilot01_preflight_v1/processed/pilot01_preflight_v1_20260924/run_scores.jsonl')
    assert len(preflight) == 6
    validation = {'main_batch': BATCH, 'planned_runs': len(planned), 'raw_runs': len(actual), 'scored_runs': len(scores),
                  'readable_runs': len(run_index), 'cases': len(cases), 'positive_cases': sum(not c['is_negative_sentinel'] for c in cases.values()),
                  'sentinel_cases': sum(c['is_negative_sentinel'] for c in cases.values()),
                  'completed': sum(s['completed'] for s in scores), 'failed': sum(not s['completed'] for s in scores),
                  'main_raw_files_verified_against_frozen_sha256': len(raw_inventory),
                  'model_calls': totals['model_calls'], 'tool_calls': totals['tool_calls'], 'failure_causes': dict(failures),
                  'conditions': {c: {'planned': sum(s['condition_id'] == c for s in scores),
                                      'completed': sum(s['condition_id'] == c and s['completed'] for s in scores),
                                      'failed': sum(s['condition_id'] == c and not s['completed'] for s in scores)}
                                 for c in ('A0V0', 'A1V0', 'A1V1')},
                  'preflight_runs_separate': len(preflight), 'preflight_completed': sum(s['completed'] for s in preflight),
                  'all_run_ids_match_plan_and_scores': True, 'all_event_logs_match_run_json': True,
                  'all_contract_hashes_match': True, 'all_actual_memos_match_frozen_treatments': True,
                  'all_call_counts_match_scores': True, 'frozen_baseline_checks': freeze_checks,
                  'documented_post_run_source_mismatch': mismatches,
                  'scope': 'Formal Stage 1 batch plus separately labelled final live preflight; excludes mock/development batches and later Stage 2 design.'}
    write_json(OUT / 'validation.json', validation)
    condition_rows = [f'| {c} | {v["planned"]} | {v["completed"]} | {v["failed"]} |' for c, v in validation['conditions'].items()]
    failure_rows = [f'| {k} | {v} |' for k, v in failures.items()]
    write(OUT / '00_交付说明.md', '\n'.join([
        '# Stage 1 全量实验记录交付说明\n',
        '整理日期：2026-10-01。正式实验批次：`pilot01_main_v1_20260924`。\n',
        '**本包覆盖正式 Stage 1 的全部 648 轮记录，包含 590 轮完成和 58 轮运行失败。** 每轮有一份完整 Markdown，并保留五种原始日志。另附正式运行前的 6 轮 live preflight（5 完成、1 失败），独立存放，不计入 648 轮。\n',
        '## 1. 从哪里开始\n',
        '1. [全量记录索引](01_全量记录索引.md)：点击编号查看任意一轮。',
        '2. [完整 outcome 报告](outputs/reports/pilot01_phase2_phase3_full_outcomes.md)：查看整体结果、分层与比较。',
        '3. [运行报告](outputs/reports/pilot_execution_report.md)、[质量检查](outputs/reports/pilot_quality_check.md)、[失败分析](outputs/reports/failure_analysis.md)：核对完成情况和异常。',
        '4. 程序读取可使用 `run_index.json` 和 `outputs/pilot01_main_v1/processed/`。\n',
        '## 2. 样本与轮数\n',
        '- 共 40 份合同：32 正例（控制权变更与任意终止各 16）和 8 个阴性哨兵。',
        '- 正例：32 × E0/E1 × 3 种治理条件 × 3 次重复 = 576 轮。',
        '- 哨兵：8 × E0 × 3 种治理条件 × 3 次重复 = 72 轮。',
        '- 合计：648 轮。重复编号 r000/r001/r002 为计划内重复，不是失败后的重试。\n',
        '| 条件 | 计划 | 完成 | 运行失败 |\n|---|---:|---:|---:|', *condition_rows,
        '| 合计 | 648 | 590 | 58 |\n',
        'A0V0：无原文访问、不要求核验；A1V0：可用原文工具、核验可选；A1V1：可用原文工具、要求核验。E0 为完整冻结备忘录，E1 仅删除指定目标 claim 及其引用。\n',
        '## 3. 包内材料\n',
        '| 路径 | 内容 |\n|---|---|',
        '| `readable_runs/` | 648 份逐轮 Markdown，按案例分文件夹；含完整输入、输出、调用、核验、事件、评分、gold 依据及合同全文。 |',
        '| `outputs/pilot01_main_v1/raw/` | 648 轮 × 5 个文件 = 3,240 个原始日志。 |',
        '| `outputs/pilot01_main_v1/processed/` | 全量逐轮评分 JSONL/CSV 与实验汇总。 |',
        '| `outputs/pilot01_main_v1/analysis/` | 合同条件指标、总体分析及扩展配对比较。 |',
        '| `outputs/pilot01_main_v1/manifests/` | 运行计划与批次索引。 |',
        '| `data/pilot01/main_v1/` | 冻结案例库、样本选择、memo 对审计、实验/提示词清单、人工复核声明及计划。 |',
        '| `cases/` | 40 份合同全文与案例信息；40 份 E0 memo、32 份 E1 memo。 |',
        '| `outputs/reports/` | 12 份审计、执行、质量、失败、结果及哨兵复核材料。 |',
        '| `outputs/pilot01_preflight_v1/` | 单独保留 6 轮最终配置 live preflight 的原始日志、评分与计划。 |',
        '| `config/`、`prompts/`、`src/`、`scripts/`、`pyproject.toml` | 模型/条件/流程/政策配置、提示词、运行与评分代码、分析和整理脚本。正式使用 v3 提示词；较早提示词仅随源代码保留。 |',
        '| `docs/`、根目录 Phase 提示文件及进展文件 | 实施说明与冻结清单引用的工作基线。 |',
        '| `provenance/` | 原有归档清单及该旧 ZIP 的哈希文件，用于来源追溯；不是本新包的哈希。 |',
        '| `validation.json` | 本次覆盖、哈希、输入和调用数核验结果；逐项冻结版本比较。 |',
        '| `package_file_manifest.json`、`verify_package.py` | 本包各文件 SHA-256 清单及可离线运行的完整性检查脚本。 |\n',
        '原始每轮五种日志：`run.json`、`events.jsonl`、`model_calls.jsonl`、`tool_calls.jsonl`、`verification.jsonl`。JSONL 应以 LF（`\\n`）分行；不要把 JSON 字符串内的 U+2028 当作行边界。\n',
        '## 4. 失败记录与指标口径\n',
        '| 运行失败原因 | 轮数 |\n|---|---:|', *failure_rows,
        '\n- 失败轮完整保留到中断点；未执行的节点无输出，候选模型响应不等于被接受的最终答案。',
        '- “completed”表示工作流完成，不表示答案正确。动作正确、有证据遗漏恢复、完成率分别计量。',
        '- 有证据遗漏恢复仅适用于完成的正例 E1；E0、哨兵与运行失败为 N/A。',
        '- 记录了 4,424 次模型调用和 3,155 次原文工具调用。2 次模型调用缺少 usage，涉及轮次的 token 总量可能为 null。',
        '- gold 与合同全文供事后复核，不表示模型获得了这些隐藏信息。当前记录仅包含系统实际保存的输入、输出与事件，服务端未返回的内部推理不在日志中。\n',
        '## 5. 完整性与版本说明\n',
        '- 648 个计划 run_id、648 个原始目录、648 条评分和648 份可读记录一一对应，无缺失、额外或重复 ID。',
        '- 3,240 个主批次原始文件均通过原批次冻结 SHA-256 核对；复制件逐字节一致。',
        '- 逐轮核对了合同文本哈希、E0/E1 实际 memo、事件流及模型/工具调用数。',
        '- 冻结清单引用的文件中，当前仅 `src/pilot01/experiment/artifacts.py` 与运行前哈希不同。这是运行结束后的 JSONL 读取修正，详见 [修正说明](outputs/pilot01_main_v1/post_run_reader_fix_note.md)。本包保留冻结清单与当前修正代码，未将其冒充为逐字节原始执行代码。',
        '- 本包为正式 Stage 1 及其直接支撑材料；早期开发、mock、Gate 测试与后续 Stage 2 设计不在本包。API 本地凭据、虚拟环境及 Git 目录未打包。',
        '- 本次仅离线整理已有实验产物，没有重新运行模型、重试失败轮或修改原始结果。\n',
        '## 6. 离线使用\n',
        '解压整个目录后打开本说明或总索引，内部相对链接即可使用。校验包内文件可运行：\n',
        block('python verify_package.py', 'text'),
        '该检查使用 Python 标准库，不调用模型。若要重新评分或分析，需根据 `pyproject.toml` 安装项目依赖；本包没有附带 Python 虚拟环境。\n',
    ]) + '\n')
    write(OUT / 'verify_package.py', '''"""Verify this delivery with Python standard library only; no network calls."""
import hashlib
import json
from pathlib import Path
root = Path(__file__).resolve().parent
manifest = json.loads((root / 'package_file_manifest.json').read_text(encoding='utf-8'))
expected = {r['path'] for r in manifest['files']}
actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.name != 'package_file_manifest.json'}
errors = []
for item in manifest['files']:
    p = root / item['path']
    if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != item['sha256']:
        errors.append(item['path'])
assert not errors, f'Missing or changed files: {errors}'
assert expected == actual, f'Unexpected files: {sorted(actual - expected)}'
index = json.loads((root / 'run_index.json').read_text(encoding='utf-8'))
assert len(index) == len({r['run_id'] for r in index}) == 648
assert all((root / r['readable_record']).is_file() for r in index)
assert all(len(list((root / r['raw_directory']).iterdir())) == 5 for r in index)
print(f'PASS: {len(expected)} file hashes; 648 rounds; 3240 main raw files.')
''')
    # Scan packaged text without ever reading the local credential file.
    for p in OUT.rglob('*'):
        if p.is_file():
            assert p.name != 'api.local.env' and p.suffix != '.env'
            content = p.read_text(encoding='utf-8-sig')
            assert not any(pattern.search(content) for pattern in safe_patterns), f'Credential-shaped content in {p.relative_to(OUT)}'
    payload = [{'path': p.relative_to(OUT).as_posix(), 'bytes': p.stat().st_size, 'sha256': sha(p)}
               for p in sorted(OUT.rglob('*')) if p.is_file() and p.name != 'package_file_manifest.json']
    write_json(OUT / 'package_file_manifest.json', {'schema_version': 1, 'packaged_date': '2026-10-01',
               'file_count_including_this_manifest': len(payload) + 1,
               'scope': 'Every package file except this manifest itself', 'files': payload})
    archive = OUT.with_suffix('.zip')
    print(f'Compressing {len(payload) + 1} files...', flush=True)
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(OUT.rglob('*')):
            if p.is_file():
                z.write(p, p.relative_to(OUT.parent).as_posix())
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        for item in payload:
            assert hashlib.sha256(z.read(OUT.name + '/' + item['path'])).hexdigest() == item['sha256']
        assert len(z.namelist()) == len(payload) + 1
    write(archive.with_suffix('.zip.sha256'), sha(archive) + '  ' + archive.name + '\n')
    print(dump({'zip': str(archive), 'files': len(payload) + 1, 'zip_bytes': archive.stat().st_size,
                'uncompressed_bytes': sum(p.stat().st_size for p in OUT.rglob('*') if p.is_file()),
                'main_runs': 648, 'preflight_runs': 6, 'raw_files': 3240, 'all_zip_payload_hashes_verified': True}))


if __name__ == '__main__':
    main()
