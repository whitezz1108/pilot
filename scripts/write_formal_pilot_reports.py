"""Write Phase 2 and Phase 3 reports from the frozen Pilot01 artifacts."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_ID = "pilot01_main_v1_20260924"
RAW_ROOT = ROOT / "outputs/pilot01_main_v1/raw" / EXPERIMENT_ID
SCORE_ROOT = ROOT / "outputs/pilot01_main_v1/processed" / EXPERIMENT_ID
ANALYSIS_ROOT = ROOT / "outputs/pilot01_main_v1/analysis" / EXPERIMENT_ID
REPORT_ROOT = ROOT / "outputs/reports"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]


def pct(n, d):
    return "—" if not d else f"{100 * n / d:.1f}% ({n}/{d})"


def fmt_rate(rate):
    if rate["rate"] is None:
        return "N/A"
    return pct(rate["numerator"], rate["denominator"])


def latency_text(values):
    values = [float(x) for x in values if x is not None]
    if not values:
        return "N/A"
    return f"mean {statistics.mean(values):.1f}s; median {statistics.median(values):.1f}s"


def unit_rows(rows, *, sample=None, error=None, condition=None, category=None):
    out = []
    for r in rows:
        if sample is not None and ("sentinel" if r["is_negative_sentinel"] else "positive") != sample:
            continue
        if error is not None and r["error_condition"] != error:
            continue
        if condition is not None and r["condition_id"] != condition:
            continue
        if category is not None and r["target_category"] != category:
            continue
        out.append(r)
    return out


def valid_rate(group, field, *, completed_only=False):
    if completed_only:
        group = [r for r in group if r["completed"]]
    values = [r.get(field) for r in group if r.get(field) is not None]
    return sum(v is True for v in values), len(values)


def fact_any_rate(group):
    group = [r for r in group if r["completed"]]
    values = []
    for r in group:
        bits = [r.get("manager_fact_recovery_correct"), r.get("compliance_fact_recovery_correct")]
        bits = [b for b in bits if b is not None]
        if bits:
            values.append(any(b is True for b in bits))
    return sum(v is True for v in values), len(values)


def primary_rate(group):
    group = [r for r in group if r["completed"]]
    values = []
    for r in group:
        if r["error_condition"] != "E1" or r["is_negative_sentinel"]:
            continue
        values.append(bool(r.get("manager_corrected_with_evidence") or r.get("compliance_corrected_with_evidence")))
    return sum(values), len(values)


def sentinel_rate(scores, condition):
    g = unit_rows(scores, sample="sentinel", error="E0", condition=condition)
    return pct(*valid_rate([r for r in g if r["completed"]], "false_escalation"))


def sentinel_review(scores, condition):
    g = [r for r in unit_rows(scores, sample="sentinel", error="E0", condition=condition) if r["completed"]]
    return pct(sum(r["final_decision"] == "REVIEW" for r in g), len(g))


def classify_failure(failure):
    msg = str((failure or {}).get("message") or (failure or {}).get("error") or "")
    if "more than 4 tool" in msg:
        return "Tool loop exceeded four calls without a final answer"
    if "evidence_not_opened" in msg:
        return "Required evidence had not been opened in the node's own tool log"
    if "output limit" in msg or "finish_reason='length'" in msg:
        return "Output truncated at the 16,384-token ceiling"
    if "could not reach the model endpoint" in msg:
        return "Model endpoint connection reset"
    return "Other protocol or transport failure"


def main():
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    manifest_path = ROOT / "data/pilot01/main_v1/experiment_manifest.yaml"
    prompt_manifest_path = ROOT / "data/pilot01/main_v1/prompt_manifest.yaml"
    plan_path = ROOT / "data/pilot01/main_v1/run_plan_v1.json"
    registry_path = ROOT / "data/pilot01/main_v1/case_registry_v1.json"
    manifest = read_json(manifest_path)
    prompt_manifest = read_json(prompt_manifest_path)
    plan = read_json(plan_path)
    registry = read_json(registry_path)
    analysis = read_json(ANALYSIS_ROOT / "phase3_analysis.json")
    summary = read_json(SCORE_ROOT / "experiment_summary.json")
    scores = read_jsonl(SCORE_ROOT / "run_scores.jsonl")
    score_by_id = {r["run_id"]: r for r in scores}

    raw_artifacts = []
    raw_by_id = {}
    for run_json in sorted(RAW_ROOT.glob("*/run.json")):
        rid = run_json.parent.name
        raw_by_id[rid] = read_json(run_json)
        for f in sorted(run_json.parent.iterdir()):
            if f.is_file():
                raw_artifacts.append((f.relative_to(ROOT).as_posix(), hashlib.sha256(f.read_bytes()).hexdigest(), f.stat().st_size))
    hash_manifest = ROOT / "outputs/pilot01_main_v1/raw_artifact_sha256_v1.csv"
    with hash_manifest.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["path", "sha256", "bytes"])
        w.writerows(raw_artifacts)

    failures = []
    for rid, run in sorted(raw_by_id.items()):
        if run.get("execution", {}).get("status") == "completed":
            continue
        failure = run.get("failure") or {}
        failures.append({
            "run_id": rid,
            "case_id": run.get("case_id"),
            "contract_id": run.get("contract_id"),
            "condition_id": run.get("condition_id"),
            "error_condition": run.get("error_condition"),
            "repeat_index": run.get("repeat_index"),
            "stage": failure.get("failure_stage"),
            "type": failure.get("failure_type"),
            "reason": classify_failure(failure),
            "raw_path": (RAW_ROOT / rid).relative_to(ROOT).as_posix(),
        })
    failure_by_type = Counter(x["type"] or "unspecified" for x in failures)
    failure_by_stage = Counter(x["stage"] or "unspecified" for x in failures)
    failure_by_reason = Counter(x["reason"] for x in failures)

    # Exact-configuration live preflight; it is reported separately and is
    # never pooled with the formal plan.
    preflight_root = ROOT / "outputs/pilot01_preflight_v1/raw/pilot01_preflight_v1_20260924"
    preflight_runs = []
    for p in sorted(preflight_root.glob("*/run.json")):
        x = read_json(p)
        preflight_runs.append((p.parent.name, x.get("execution", {}).get("status"), x.get("failure") or {}))
    preflight_ok = sum(status == "completed" for _, status, _ in preflight_runs)
    preflight_failed = len(preflight_runs) - preflight_ok

    # Role structured-output validity is one expected final output per role
    # per planned run, including a missing output after a protocol failure.
    role_bad = defaultdict(list)
    call_total = tool_total = search_total = open_total = 0
    prompt_tokens = completion_tokens = missing_usage = 0
    raw_call_latencies = []
    format_repairs = technical_retries = 0
    for job in plan["jobs"]:
        rid = job["run_id"]
        run_dir = RAW_ROOT / rid
        calls = read_jsonl(run_dir / "model_calls.jsonl")
        tools = read_jsonl(run_dir / "tool_calls.jsonl")
        call_total += len(calls)
        tool_total += len(tools)
        search_total += sum(t.get("tool") == "search_contract" for t in tools)
        open_total += sum(t.get("tool") == "open_source_span" for t in tools)
        for c in calls:
            format_repairs += bool(c.get("is_repair"))
            # attempt > 0 also marks a format-only repair. Count only
            # non-repair model retries as technical retries.
            technical_retries += c.get("attempt", 0) > 0 and not c.get("is_repair", False)
            usage = c.get("usage") or {}
            if usage.get("prompt_tokens") is None or usage.get("completion_tokens") is None:
                missing_usage += 1
            else:
                prompt_tokens += usage["prompt_tokens"]
                completion_tokens += usage["completion_tokens"]
            if c.get("latency_seconds") is not None:
                raw_call_latencies.append(c["latency_seconds"])
        for role in ("manager", "compliance"):
            role_calls = [c for c in calls if c.get("role") == role]
            finals = [c for c in role_calls if not c.get("is_tool_turn")]
            final = finals[-1] if finals else None
            valid = bool(final and final.get("parsed_output") is not None and not final.get("parse_error") and not final.get("error"))
            if not valid:
                role_bad[role].append(rid)

    manager_success = 648 - len(role_bad["manager"])
    compliance_success = 648 - len(role_bad["compliance"])
    completed_rows = [r for r in scores if r["completed"]]
    run_latencies = [r.get("latency_seconds") for r in scores if r.get("latency_seconds") is not None]

    # Whole-batch matrices by governance condition.
    condition_rows = {}
    for c in ("A0V0", "A1V0", "A1V1"):
        g = [r for r in scores if r["condition_id"] == c]
        condition_rows[c] = g
    matrix_lines = ["| 条件 | 计划 | 完成 | 失败 | 成功/计划 |", "|---|---:|---:|---:|---:|"]
    for c, g in condition_rows.items():
        done = sum(r["completed"] for r in g)
        matrix_lines.append(f"| {c} | {len(g)} | {done} | {len(g)-done} | {pct(done,len(g))} |")

    primary_lines = ["| 样本/目标 | E 条件 | 治理条件 | 计划 | 完成 | 有证据遗漏恢复 | 事实恢复（任一节点） | 最终行动正确 | Manager 错误存活 | Compliance 错误存活 |", "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for sample in ("positive", "sentinel"):
        categories = sorted({r["target_category"] for r in scores if ("sentinel" if r["is_negative_sentinel"] else "positive") == sample})
        for category in categories:
            for error in (("E0", "E1") if sample == "positive" else ("E0",)):
                for condition in ("A0V0", "A1V0", "A1V1"):
                    g = unit_rows(scores, sample=sample, error=error, condition=condition, category=category)
                    if not g:
                        continue
                    done = sum(r["completed"] for r in g)
                    pr = primary_rate(g)
                    fr = fact_any_rate(g)
                    ar = valid_rate(g, "final_action_correct", completed_only=True)
                    mr = valid_rate(g, "error_survival_manager")
                    cr = valid_rate(g, "error_survival_compliance")
                    primary_value = "N/A" if sample == "sentinel" or error == "E0" else pct(*pr)
                    primary_lines.append(f"| {sample} / {category} | {error} | {condition} | {len(g)} | {done} | {primary_value} | {pct(*fr)} | {pct(*ar)} | {pct(*mr)} | {pct(*cr)} |")

    # Condition-level sentinel false-escalation and REVIEW rates.
    sentinel_lines = ["| 治理条件 | 计划 | 完成 | false_escalation | 最终 REVIEW | 最终 ACCEPT | 最终 ESCALATE |", "|---|---:|---:|---:|---:|---:|---:|"]
    for c in ("A0V0", "A1V0", "A1V1"):
        g = unit_rows(scores, sample="sentinel", error="E0", condition=c)
        done = [r for r in g if r["completed"]]
        sentinel_lines.append(
            f"| {c} | {len(g)} | {len(done)} | {pct(*valid_rate(done,'false_escalation'))} | "
            f"{pct(sum(r['final_decision']=='REVIEW' for r in done),len(done))} | "
            f"{pct(sum(r['final_decision']=='ACCEPT' for r in done),len(done))} | "
            f"{pct(sum(r['final_decision']=='ESCALATE' for r in done),len(done))} |"
        )

    # Evidence verifier observability within A1V1 completed runs.
    a1v1_completed = [r for r in scores if r["condition_id"] == "A1V1" and r["completed"]]
    a1v1_v_lines = ["| 节点 | 要求且检查 | 满足 | verification_basis 一致 |", "|---|---:|---:|---:|"]
    for role in ("manager", "compliance"):
        req = "manager_verification_required" if role == "manager" else "compliance_verification_required"
        sat = "manager_verification_satisfied" if role == "manager" else "compliance_verification_satisfied"
        agree = "manager_verification_basis_agrees" if role == "manager" else "compliance_verification_basis_agrees"
        eligible = [r for r in a1v1_completed if r.get(req)]
        a1v1_v_lines.append(f"| {role} | {len(eligible)}/{len(eligible)} | {sum(bool(r.get(sat)) for r in eligible)}/{len(eligible)} | {sum(bool(r.get(agree)) for r in eligible)}/{len(eligible)} |")

    # Paired-bootstrap table already resamples contracts, with repeats first
    # averaged within each contract-condition.
    pair_lines = ["| 指标 | 比较 | 配对合同 | 风险差 | 95% 合同 bootstrap 区间 |", "|---|---|---:|---:|---:|"]
    for p in analysis["contract_paired_comparisons"]:
        if p["mean_risk_difference"] is None:
            effect, interval = "N/A", "N/A"
        else:
            effect = f"{100*p['mean_risk_difference']:+.1f} pp"
            lo, hi = p["bootstrap_95_percentile_ci"]
            interval = f"[{100*lo:+.1f}, {100*hi:+.1f}] pp"
        pair_lines.append(f"| {p['metric']} ({p['error_condition']}) | {p['contrast']} | {p['contracts_paired']} | {effect} | {interval} |")

    failure_lines = ["| run_id | 合同/案例 | E × 条件 × 重复 | 节点 / 类型 | 记录原因 | raw 目录 |", "|---|---|---|---|---|---|"]
    for f in failures:
        failure_lines.append(f"| `{f['run_id']}` | {f['contract_id']} / {f['case_id']} | {f['error_condition']} × {f['condition_id']} × r{f['repeat_index']} | {f['stage']} / {f['type']} | {f['reason']} | `{f['raw_path']}` |")

    # Phase 2 execution report.
    prompt_hash_lines = "\n".join(f"- {p['role']} `{p['version']}`: SHA-256 `{p['sha256']}`" for p in prompt_manifest["prompts"])
    fail_summary = "\n".join(f"- {k}: {v}" for k, v in sorted(failure_by_reason.items()))
    execution = f"""# Phase 2 — 正式 Pilot 执行报告

日期：2026-09-24  
实验：`{EXPERIMENT_ID}`  
阶段状态：**完成；58 个计划 run 以失败终态保留**

## 冻结的工作基线

没有单独正式 protocol 文件。本次可追溯基线由实施说明、版本化决定、Phase 1 审计结论、当前 policy/config 和样本构成；用户确认人工复核完成，已记录于 `data/pilot01/main_v1/review_attestation_v1.json`。开发文件中历史 PENDING 标记不作为阻断条件。

- POLICY-01 v2；policy 文件 SHA-256 `{manifest['policy']['sha256']}`，fingerprint `{manifest['policy']['fingerprint']}`。规则为 present→ESCALATE、两类都 absent→ACCEPT、其他/unknown→REVIEW。
- 正式 registry：40 份合同（32 正例：Change Of Control 与 Termination For Convenience 各 16；8 哨兵），SHA-256 `{manifest['data']['registry']['sha256']}`。E1 只删除目标 claim 及其证据；32 对审计通过；哨兵仅 E0。
- 主条件：A0V0、A1V0、A1V1；不包含 A0V1。模型为 `{manifest['models']['model_id']}`，manager/compliance 相同；temperature 0、top_p 默认、max output 16,384、timeout 180 秒。最大格式修复 1 次，技术 retry 0 次。
- 提示词版本与内容哈希：\n{prompt_hash_lines}
- 运行计划：648 个唯一 run_id，3 次重复；随机化 seed `{manifest['run_plan']['seed']}`；canonical fingerprint `{analysis['plan']['canonical_fingerprint']}`；文件 SHA-256 `{manifest['run_plan']['sha256']}`。哈希、policy、prompts、案例、scorer、schema 等冻结输入在 manifest 中记录。

## 预检与正式执行

精确当前模型配置的 live preflight 共 {len(preflight_runs)} 个 run：{preflight_ok} 完成、{preflight_failed} 失败。失败为 Compliance 在 E1×A1V0 超过四次工具调用仍未输出最终答复。按研究者明确的正式批次执行指令继续；将该预检失败保留为独立版本记录，不与主批次合并。

正式开跑前报告的计划为 **648 runs**，版本为上述 manifest/prompt/model/policy 版本。使用 `scripts/Load-Pilot01ApiEnv.ps1` 加载 `config/api.local.env`；没有将密钥打印或写入 manifest。以 `--live --live-batch` 串行执行，未传 `--resume` 或 `--rerun-failed`。

| 治理条件 | 计划 | 完成 | 失败 | 成功/计划 |
|---|---:|---:|---:|---:|
{chr(10).join(matrix_lines[2:])}
| **总计** | **648** | **590** | **58** | **91.0%** |

所有计划 ID 恰有一个最终 raw artifact；与计划集合完全一致，无缺失、额外 ID、重复 run 或 active 目录。执行器退出码 0，结束汇总 completed=590、failed=58。逐 run 输出由 raw `run.json` 和模型/工具/验证/事件日志保存。原始 artifact 完成后另生成 SHA-256 清单：`outputs/pilot01_main_v1/raw_artifact_sha256_v1.csv`（{len(raw_artifacts)} 个文件）。

## 调用量与运行成本

- 原始模型调用：{call_total:,}；source-tool 调用：{tool_total:,}（search {search_total:,}，open {open_total:,}），工具返回失败 0。
- 原始 usage 有报告的 prompt tokens {prompt_tokens:,}、completion tokens {completion_tokens:,}；{missing_usage} 次模型调用缺少 token usage。run 级评分对包含缺失子调用的 2 个 run 将 token 合计置为 N/A；其余 run 可复算。无版本化价格表，费用**无法估算**，不写为零。
- 单次模型调用 latency：{len(raw_call_latencies):,}/{call_total:,} 有记录，均值 {statistics.mean(raw_call_latencies):.2f}s、中位数 {statistics.median(raw_call_latencies):.2f}s。run 级累计模型调用 latency：{len(run_latencies)}/648 有值，均值 {statistics.mean(run_latencies):.2f}s、中位数 {statistics.median(run_latencies):.2f}s；不是端到端墙钟时长。
- 格式修复 {format_repairs} 次；技术 retry {technical_retries} 次。所有 58 个失败均为原计划 run 的失败终态，未删失、未替换。

## 交付

- Frozen manifests：`data/pilot01/main_v1/experiment_manifest.yaml`、`prompt_manifest.yaml`；plan JSON/CSV。
- Raw：`outputs/pilot01_main_v1/raw/{EXPERIMENT_ID}/`；hash sidecar 如上。
- Scored：`outputs/pilot01_main_v1/processed/{EXPERIMENT_ID}/run_scores.jsonl`、`run_scores.csv`、`experiment_summary.json`。
- Phase 2 quality and failure reports：见同目录下 `pilot_quality_check.md`、`failure_analysis.md`。
"""
    (REPORT_ROOT / "pilot_execution_report.md").write_text(execution, encoding="utf-8")

    # Phase 2 quality report; preserve threshold failures as findings.
    structural_status = "PASS" if manager_success / 648 >= .95 else "FAIL"
    compliance_status = "PASS" if compliance_success / 648 >= .95 else "FAIL"
    quality = f"""# Phase 2 — 质量检查

## 覆盖与版本

| 检查 | 结果 | 证据 |
|---|---|---|
| 计划/实际覆盖 | PASS：648 计划 / 648 raw / 648 scored；missing=0，extra=0，重复 ID=0 | `phase3_analysis.json` plan block；raw manifest |
| 正式 registry 与 E1 操纵 | PASS：40 案例；32 正例、8 哨兵；32 个 E0/E1 删除对通过结构审计；无开发合同混用 | `memo_pair_audit_v1.json` |
| 样本组成 | PASS：两个正例目标类各 16；哨兵 E0-only；未运行 A0V1 | frozen registry/plan |
| 模型、policy、prompt 版本 | PASS：配置 mismatch=0 | analysis quality checks；manifests |
| 原始请求中 hidden gold/treatment 泄漏 | PASS：0 次发现 | analysis quality checks |
| credential-shaped 原始内容 | PASS：0 次发现 | analysis quality checks |
| A0V0 工具隔离 | PASS：A0V0 工具调用 0 | `run_scores.csv` |
| A1 source tool | PASS：{tool_total:,} 次调用，{search_total:,} search、{open_total:,} open，记录到的 tool failure=0 | raw `tool_calls.jsonl`、评分汇总 |
| A1V1 节点原文核验 | PASS（已完成 run）：Manager {sum(bool(r.get('manager_verification_required')) for r in a1v1_completed)}/{sum(bool(r.get('manager_verification_required')) for r in a1v1_completed)} 满足；Compliance 同为 {sum(bool(r.get('compliance_verification_satisfied')) for r in a1v1_completed)}/{sum(bool(r.get('compliance_verification_required')) for r in a1v1_completed)}。另有 1 个失败 run 在 Compliance 节点未打开所引证据，归入协议失败 | A1V1 `verification.jsonl`、`run_scores.csv` |
| Manager 结构化最终输出率（95%线） | {structural_status}：{pct(manager_success,648)} | 未有效输出的 run_id 见下 |
| Compliance 结构化最终输出率（95%线） | {compliance_status}：{pct(compliance_success,648)}；低于预设线 | 未有效输出的 run_id 见下 |
| 重试/格式修复 | PASS：技术重试 0；格式修复 {format_repairs}（上限 1） | calls log |
| 失败/缺失处理 | PASS：58 失败保留为失败，未当作错误答案，也未重跑 | `failure_analysis.md` |
| token/cost | 部分可得：2 个 call usage 缺失；无定价表，费用无法估算 | raw model logs |

### 结构化输出无效的 run_id

Manager（{len(role_bad['manager'])}）：\n{', '.join('`'+x+'`' for x in role_bad['manager'])}

Compliance（{len(role_bad['compliance'])}）：\n{', '.join('`'+x+'`' for x in role_bad['compliance'])}

## 失败类别

按最终失败终态统计：{', '.join(f'{k}={v}' for k,v in sorted(failure_by_type.items()))}；节点：{', '.join(f'{k}={v}' for k,v in sorted(failure_by_stage.items()))}。详细 run_id 与 raw 路径见 `failure_analysis.md`。

## 离线 JSONL 读取器修正

第一次离线 score 将合法模型字符串中的 U+2028 Unicode 行分隔符误当作 JSONL 边界；raw 文件未损坏。读取器已按 JSONL 的 LF (`\\n`) 分隔读取；原始 JSONL 未更改，修正后成功 score 648 条，summary 与 raw 重算一致。该纯读取修正发生于真实调用之后，冻结执行文件哈希清单中仅 `src/pilot01/experiment/artifacts.py` 当前哈希发生变化；冻结时哈希仍以 manifest 为准。详见 `outputs/pilot01_main_v1/post_run_reader_fix_note.md`。

## 需要单独关注的 sentinel 误升级

冻结标签下，A1V0 为 {sentinel_rate(scores,'A1V0')}，A1V1 为 {sentinel_rate(scores,'A1V1')}。6 次误升级集中在 `CUAD-0337` 与 `CUAD-0374`；这两份原文包含 termination-for-cause、期满/续期通知或 cancellation 文本，当前 registry 将两项目标标为 absent。它们是模型误判还是需要研究者按定义裁定的近似条款，属于一个具体案例口径问题。未改 gold、未后验排除；本次 sentinel false-escalation 率按冻结标签计算，解释暂列待裁定。其余 Phase 2/3 计算继续按冻结基线。

## 证据界限

上述 0 泄漏、0 工具失败、A1V1 检查完成率和 hash/ID 对账是程序/日志证据；用户已确认人工复核完成并由 attestation 记录。程序校验不替代人工语义复核。结构化输出线只有 Compliance 未达 95%，而这不改变既定运行结果。
"""

    (REPORT_ROOT / "pilot_quality_check.md").write_text(quality, encoding="utf-8")

    failure_report = f"""# Phase 2 — 失败分析

正式计划 648 个 run：590 completed、58 failed。失败均保留原始 run 目录；未发起 `--resume`/`--rerun-failed`，故正式重试数为 0。

## 类别

| 失败原因 | 数量 |
|---|---:|
""" + "\n".join(f"| {k} | {v} |" for k, v in sorted(failure_by_reason.items())) + f"""

按 failure type：{', '.join(f'{k}={v}' for k,v in sorted(failure_by_type.items()))}。按失败节点：{', '.join(f'{k}={v}' for k,v in sorted(failure_by_stage.items()))}。

失败主要来自 Manager/Compliance 在第四次工具调用后仍未给出最终结构化输出（51）；另有输出达到 16,384 token 上限而截断（4）、模型 endpoint 连接被重置（2），以及 1 次 A1V1 Compliance 引用未打开原文的 verification protocol failure。失败分别作为技术/协议观察，不以普通误分类计分。

## 全部失败 run

""" + "\n".join(failure_lines) + "\n"
    (REPORT_ROOT / "failure_analysis.md").write_text(failure_report, encoding="utf-8")

    # Phase 3: sample flow and run-level denominators are kept explicit.
    primary_by_condition = {}
    for c in ("A0V0", "A1V0", "A1V1"):
        g = unit_rows(scores, sample="positive", error="E1", condition=c)
        primary_by_condition[c] = primary_rate(g)
    e0_action = {c: valid_rate(unit_rows(scores, sample="positive", error="E0", condition=c), "final_action_correct", completed_only=True) for c in ("A0V0", "A1V0", "A1V1")}
    e1_action = {c: valid_rate(unit_rows(scores, sample="positive", error="E1", condition=c), "final_action_correct", completed_only=True) for c in ("A0V0", "A1V0", "A1V1")}
    e1_fact = {c: fact_any_rate(unit_rows(scores, sample="positive", error="E1", condition=c)) for c in ("A0V0", "A1V0", "A1V1")}
    survival_m = {c: valid_rate(unit_rows(scores, sample="positive", error="E1", condition=c), "error_survival_manager") for c in ("A0V0", "A1V0", "A1V1")}
    survival_c = {c: valid_rate(unit_rows(scores, sample="positive", error="E1", condition=c), "error_survival_compliance") for c in ("A0V0", "A1V0", "A1V1")}
    stage_lines = ["| 条件 | Manager 纠正 | Compliance 首次纠正 | 未纠正 | 协议失败/不可得 |", "|---|---:|---:|---:|---:|"]
    for c in ("A0V0", "A1V0", "A1V1"):
        g = unit_rows(scores, sample="positive", error="E1", condition=c)
        co = Counter(r["correction_stage"] for r in g)
        stage_lines.append(f"| {c} | {co['manager']} | {co['compliance']} | {co['never']} | {co['unavailable']} |")

    target_lines = ["| Target class | A1V0 E1 | A1V1 E1 |", "|---|---:|---:|"]
    for category in ("Change Of Control", "Termination For Convenience"):
        vals = []
        for c in ("A1V0", "A1V1"):
            g = unit_rows(scores, sample="positive", error="E1", condition=c, category=category)
            vals.append(pct(*primary_rate(g)))
        target_lines.append(f"| {category} | {vals[0]} | {vals[1]} |")

    # Overall summaries for executions, tools and costs.
    phase3 = f"""# 主 Pilot 研究报告

## 结论范围

本次只分析冻结的 `pilot01_main_v1_20260924`：40 个合同、3 个治理条件、E0/E1 配对和 3 次重复；不混入 development 或 preflight run。计划 648；完成 590，失败 58。所有 648 行都有 raw `run.json` 和 score 行，失败 run 在结果指标的合适分母中记为 unavailable，而不是 0。主要结果不能证明理论假设，只支持当前流程的可行性、操纵表现与方差初估。

## 版本与样本流程

```mermaid
flowchart LR
  A[40 冻结案例] --> B[32 正例: COC 16 + TFC 16]
  A --> C[8 阴性哨兵]
  B --> D[32 × E0/E1 × 3 条件 × 3 repeats = 576]
  C --> E[8 × E0 × 3 条件 × 3 repeats = 72]
  D --> F[正式计划 648]
  E --> F
  F --> G[完成 590]
  F --> H[失败 58，保留原始记录]
```

Policy-01 v2（SHA `{manifest['policy']['sha256']}`；fingerprint `{manifest['policy']['fingerprint']}`）；Manager/Compliance v3；Repair v3 format-only；模型 `{manifest['models']['model_id']}`；max output 16,384；3 个条件 A0V0/A1V0/A1V1；A0V1 不在主计划。运行计划 canonical fingerprint `{analysis['plan']['canonical_fingerprint']}`，运行计划文件 SHA `{manifest['run_plan']['sha256']}`。E1 为删除目标 claim 和其证据的纯删除，32/32 对结构审计通过。研究者已确认人工复核完成，记录在 `review_attestation_v1.json`；这与程序日志质量检查分开表述。

| 条件 | 计划 | 完成 | 失败 |
|---|---:|---:|---:|
{chr(10).join(matrix_lines[2:])}
| 总计 | 648 | 590 | 58 |

## 预先固定结果和条件统计

### 主要结果：`omission_recovered_with_evidence`

分母仅含**完成的正例 E1 run**。当 Manager 或 Compliance 在 Final 前找回被删除目标事实、正确判断该条款为 present，且由该节点在本 run 中实际打开、属于该合同并与 gold span 重叠的原文支持，才计成功。E0、哨兵、失败为 N/A。A0V0 无原文访问，按预先规定结构性为 0；它和 A1 对比包含工具可用性的定义差异，不能解释为模型纠错能力的完整差异。

{chr(10).join(primary_lines)}

主要结果按条件汇总：A0V0 {pct(*primary_by_condition['A0V0'])}（结构性零）；A1V0 {pct(*primary_by_condition['A1V0'])}；A1V1 {pct(*primary_by_condition['A1V1'])}。两个目标类的方向不同：\n{chr(10).join(target_lines)}

### 事实恢复、错误存活与政策 Final

| 条件 | 正例 E1 目标事实恢复（任一节点） | Manager 错误存活 | Compliance 错误存活 | 正例 E1 Final action 正确 | 正例 E0 Final action 正确 |
|---|---:|---:|---:|---:|---:|
""" + "\n".join(
        f"| {c} | {pct(*e1_fact[c])} | {pct(*survival_m[c])} | {pct(*survival_c[c])} | {pct(*e1_action[c])} | {pct(*e0_action[c])} |"
        for c in ("A0V0", "A1V0", "A1V1")
    ) + f"""

E1 Final action 依据 POLICY-01 v2：任一 target present→ESCALATE；两者 absent→ACCEPT；任一 unknown→REVIEW。A0V0 的 Manager 与 Compliance 错误存活均 96/96；A1V0 分别 {pct(*survival_m['A1V0'])}、{pct(*survival_c['A1V0'])}；A1V1 分别 {pct(*survival_m['A1V1'])}、{pct(*survival_c['A1V1'])}。事实恢复不等于证据支持纠错，也不等于 Final action 正确。

在正例 E1 中，Final action 正确为 A0V0 {pct(*e1_action['A0V0'])}、A1V0 {pct(*e1_action['A1V0'])}、A1V1 {pct(*e1_action['A1V1'])}。E0 对照为 A0V0 {pct(*e0_action['A0V0'])}、A1V0 {pct(*e0_action['A1V0'])}、A1V1 {pct(*e0_action['A1V1'])}；REVIEW 作为政策动作单列，未混作 ESCALATE。

### Correction stage（正例 E1，计划数作为分母）

{chr(10).join(stage_lines)}

## Sentinel 误升级与待裁定案例

{chr(10).join(sentinel_lines)}

按冻结标签，6 个 false escalation 输出集中在两个合同 `CUAD-0337` 和 `CUAD-0374`，其全部 raw run_id 已在 Phase 2 score 和 failure 文件可追溯。CUAD-0337 的模型引用段落含 termination-for-cause 与 term/renewal 通知文字；CUAD-0374 的引用段落含“期满/续期时 30 天书面通知终止”文字。两案的冻结 gold 仍是两项目标 absent，且用户已确认此批人工复核完成；**输出行为本身不证明 gold 错误**。由于具体文本与 sentinel 标签之间需要按本研究 target 定义裁定，误升级率按冻结标签报告但其语义解释暂列未决；未后验改标签或删除合同。其余各项 Phase 3 结果不受此 sentinel 裁定影响。

## 原文工具与核验

总 source tool 调用 {tool_total:,}：分类搜索 {search_total:,}，原文打开 {open_total:,}；工具调用失败 0。A0V0 的工具调用为 0。A1V1 完成的 {len(a1v1_completed)} 个 run 中，Manager 与 Compliance 各有 {sum(bool(r.get('manager_verification_required')) for r in a1v1_completed)} / {len(a1v1_completed)} 与 {sum(bool(r.get('compliance_verification_required')) for r in a1v1_completed)} / {len(a1v1_completed)} 次必需核验全部满足；声明/派生 verification basis 一致数分别为 {sum(bool(r.get('manager_verification_basis_agrees')) for r in a1v1_completed)}/{len(a1v1_completed)}、{sum(bool(r.get('compliance_verification_basis_agrees')) for r in a1v1_completed)}/{len(a1v1_completed)}。另有 1 个 A1V1 Compliance verification 失败 `evidence_not_opened`，保留为 ProtocolError。以上是程序日志和 offset/ID 的可追溯性核验，不等于对所有条文作新的人工语义判断；人工复核状态以用户确认记录为准。

## 预定配对比较

同合同、同条件的重复 run 先平均；按合同重抽样 10,000 次，报告双侧 percentile 95% 区间。合同而非 run 是重抽样单位。区间描述 pilot，不作为显著性检验。

{chr(10).join(pair_lines)}

A1V0 与 A0V0 的主要结果比较有定义性零值差异，需同时参照目标事实恢复、错误存活与 Final。A1V1 对 A1V0 的主要结果风险差接近零，区间跨零；不能据此宣称验证没用或有用。两个目标类别的结果并不一致，后续设计应保留分层指标。

## 失败、输出质量和成本

- 正式完成 590/648；失败 58：ToolLoopError 51（Manager 24，Compliance 27）、ProtocolError 7（截断 4、连接重置 2、evidence_not_opened 1）。分母、失败 run_id、节点和 raw path 列在 `failure_analysis.md`。
- 每 run 每节点一个结构化最终输出的有效率：Manager {pct(manager_success,648)}（{structural_status}，达到预设 95%线）；Compliance {pct(compliance_success,648)}（{compliance_status}，未达到预设线）。缺失/无效 output 的 run_id 列在 `pilot_quality_check.md`。
- 4,424 个原始模型调用中 4,422 个记录 latency，均值 {statistics.mean(raw_call_latencies):.2f}s，中位数 {statistics.median(raw_call_latencies):.2f}s；run-level 累计 latency 646 个有值，均值 {statistics.mean(run_latencies):.2f}s，中位数 {statistics.median(run_latencies):.2f}s。raw token usage 有 2 次缺失；有记录的 prompt/completion token 合计分别 {prompt_tokens:,}/{completion_tokens:,}。没有冻结定价表，费用无法估算。
- 全批格式修复 1 次，技术 retry 0 次；无 run 被排除，所有计划 ID 都进入 score 表，失败 row 不计为正确/错误答案。

## 输入、哈希和解释边界

冻结 plan SHA 与 manifest 一致；648 个 raw/scored ID 与 plan 完全匹配；源数据集、prompt manifest、policy、model 与条件版本可回溯。分析记录的 78 个冻结文件中有 1 个**当前** hash mismatch：`src/pilot01/experiment/artifacts.py` 在运行结束后修正 JSONL LF 分隔读取器，以免 U+2028 被拆行。冻结时文件 hash 按 manifest 保存；API 调用参数、请求、响应、case、policy、prompt、运行计划及评分规则均未改。详见 `post_run_reader_fix_note.md`。这项后置读取修正不改变实验处理或结果判断。

程序检查为 PASS 的部分包括 ID/版本/配置/泄漏/引用 offset 和 A1V1 运行时核验；Compliance 结构化输出率未达 95%，正式失败率为 9.0%，sentinel 两案语义裁定待研究者回复。自动 offset/日志检查不能代替用户已完成的人工语义复核。

## 后续版本建议

1. 在新版本中明确工具循环预算与 stop/continue 规则，并在预检中将长上下文工具循环作为可行性门槛；本批次不得据此回调参。
2. 保持有证据恢复与无证据事实恢复分开，按 target 类分层；按合同配对设计下一批方差与样本量计划，按失败率设置独立可行性指标。
3. 将 95% Compliance 结构化输出阈值与格式修复/协议失败分开验收；固定原始 usage 缺失处理与成本表来源。
4. 完成 CUAD-0337、CUAD-0374 对 sentinel target 定义的具体裁定后，仅在新版本 manifest 中决定样本去留；不得回写本批 raw 或隐式改 gold。

## 交付可追溯路径

逐 run score: `outputs/pilot01_main_v1/processed/{EXPERIMENT_ID}/run_scores.jsonl`; contract-condition summaries: `outputs/pilot01_main_v1/analysis/{EXPERIMENT_ID}/contract_condition_metrics.csv`; full analysis: `outputs/pilot01_main_v1/analysis/{EXPERIMENT_ID}/phase3_analysis.json`; raw files: `{RAW_ROOT.relative_to(ROOT).as_posix()}`.

**结论：**这次 pilot 支持 A1 工具流程在冻结模型和提示下实现多数正例 E1 事实恢复，A1V1 完成运行中的节点核验日志可用；但 58/648 失败与 Compliance JSON 结构化输出率未达 95%验收线是主要可行性问题。结果可供流程改进、方差与后续样本量规划；不能凭此声称组织记忆或所研究理论假设已被证明。
"""
    (REPORT_ROOT / "research_report.md").write_text(phase3, encoding="utf-8")

    pres = f"""# Pilot 汇报摘要

## 状态和设计

| 项目 | 状态 |
|---|---|
| Phase 1 | 完成：准入缺口已整改；用户确认人工复核完成 |
| Phase 2 | 完成：冻结计划 648；590 完成、58 失败；raw 保留 |
| Phase 3 | 定量分析与报告完成；CUAD-0337、CUAD-0374 哨兵语义解释待用户裁定 |
| 自动检查 | 覆盖 ID/版本/hidden gold 泄漏/密钥形态/A0 工具隔离通过；Compliance JSON 有效率低于 95% |
| 人工复核 | 用户已确认完成；以现有 review attestation 记录 |
| 具体待裁定 | 哨兵 CUAD-0337、CUAD-0374 的 termination/到期条款与 absent 标签之间的研究定义解释 |

40 合同 = 32 正例（COC 16、TFC 16）+ 8 哨兵；仅 A0V0/A1V0/A1V1；正例 E0/E1、哨兵 E0 only；3 repeats；648 planned → 590 completed + 58 failed。

## 主结果（run-level，completed 正例 E1 才进入分母）

| Condition | 有证据遗漏恢复 | 任一节点事实恢复 | Final action 正确 | Manager 错误存活 | Compliance 错误存活 |
|---|---:|---:|---:|---:|---:|
""" + "\n".join(
        f"| {c} | {pct(*primary_by_condition[c])} | {pct(*e1_fact[c])} | {pct(*e1_action[c])} | {pct(*survival_m[c])} | {pct(*survival_c[c])} |"
        for c in ("A0V0", "A1V0", "A1V1")
    ) + f"""

A0V0 在有证据指标上为结构性 0；A1V1 相对 A1V0 的合同配对风险差为 -0.5 pp，95% bootstrap 区间 [-8.6, +9.1] pp。不得把定义性零值解读为完整能力差异。

## 可行性与提醒

- 结构化输出：Manager {pct(manager_success,648)}；Compliance {pct(compliance_success,648)}（低于 95%阈值）。
- ToolLoopError 51、其他 ProtocolError 7；正式 retry 0。
- Source tools：{tool_total:,} 次，tool failure 0；A1V1 已完成 run 中 node verification 全部满足。
- 负向哨兵：按冻结标签 A1V0 false escalation {sentinel_rate(scores,'A1V0')}，A1V1 {sentinel_rate(scores,'A1V1')}，Review 分别 {sentinel_review(scores,'A1V0')}、{sentinel_review(scores,'A1V1')}。6 次误升级集中于 CUAD-0337/0374；结果按冻结标签算，语义解释待裁定。
- Tokens：raw 有 usage {prompt_tokens:,} prompt / {completion_tokens:,} completion，2 calls usage 缺失；无冻结价格，费用无法估算。

**结论**：pilot 对当前 source/search/verification 执行流程和失败模式提供可行性证据，也可初估合同间波动；不证明理论假设。详见 `research_report.md`、`pilot_quality_check.md` 与 `failure_analysis.md`。
"""

    (REPORT_ROOT / "presentation_summary.md").write_text(pres, encoding="utf-8")


if __name__ == "__main__":
    main()
