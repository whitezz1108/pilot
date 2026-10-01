# Validator v2 实现与验证报告（2026-10-01）

已在现有 Manager → Compliance track 接入确定性 validator 强化，不新增模型角色或修改轮次。设计与字段解释见 [validator_v2.md](validator_v2.md)。

## Git 备份和实现边界

- 改造前 main 已提交并推送：`91cfda2`，远端标签 `codex/pre-validator-20261001`。
- 实现分支：`codex/validator-enforcement-20261001`。
- 旧实验 raw / score、冻结 main_v1 输入、policy、prompt、模型配置和工具预算均没有改写。新 smoke 独立 experiment ID 为 `validator-v2-smoke-20261001`。

## 实现结果

逐条记录每个目标条款的成功搜索，以及每个引用来源是否在当前节点 / invocation 成功打开、是否属于当前合同和是否可追溯。A1V0 / A1V1 计算相同的 `audit_failures`；A1V0 记录并放行，A1V1 未通过时在应用候选结果前终止并保留原始记录。

补齐了混合引用漏洞：打开一个段落，不再能为其他未自行打开的继承段落引用提供通行证。完整检查结果单独写入 `validation_pass`，与旧 `status=verified`（打开过原文）区分。评分与 condition summary 同步新增完整通过字段和明确分母，旧日志不追认为通过。

## 离线测试

改造前 suite：**990 passed**。改造后完整 suite：**1000 passed in 121.33s**。

新增边界覆盖混合已打开 / 未打开引用、重开继承引用、V0 漏搜仍记录、V0 审计缺项不变成 protocol failure、旧日志与 A0 的 null 语义、其他节点 / invocation 的打开记录排除、失败工具调用排除，以及 Manager / Compliance 分别在 V1 应用候选前阻断、V0 正常继续。

复现命令：

```powershell
.venv\Scripts\python.exe -m pytest
```

## 真实模型 smoke

使用冻结正式 registry 中的 `PILOT-POS-COC-0053`，覆盖 E0/E1 × A0V0/A1V0/A1V1；一次运行，无重跑。使用现有配置 `DeepSeek-V4.1-Flash`，不改配置或预算。

计划 6，尝试 6，完成 6，protocol failure 0，未交付 0。共 42 次模型调用、30 次工具调用。两个有来源条件共 8 个节点的完整程序审计均通过；A0 的 4 个节点为不适用，不进入完整核验通过率分母。

| Memo | 条件 | 完成 | Manager 完整核验 | Compliance 完整核验 | 最终动作正确 |
| --- | --- | --- | --- | --- | --- |
| E0 | A0V0 | 是 | 不适用 | 不适用 | 是 |
| E0 | A1V0 | 是 | 通过 | 通过 | 是 |
| E0 | A1V1 | 是 | 通过 | 通过 | 是 |
| E1 | A0V0 | 是 | 不适用 | 不适用 | 否 |
| E1 | A1V0 | 是 | 通过 | 通过 | 否 |
| E1 | A1V1 | 是 | 通过 | 通过 | 否 |

**效果边界**：最终动作正确为 3/6；三个 E1 在两个节点均仍把 `Change Of Control` 评为 `unknown`，没有恢复遗漏。这里没有将 unknown 称为已经确认 absent。即使逐条搜索且打开全部报告的引用，也可能没有找到足以确认目标条款的段落。这是程序操作核验的边界；本次 smoke 不能支持“validator 提升语义识别或纠错效果”的结论，也不提供单案例的因果效果估计。

输出根目录：`outputs/validator_v2_smoke_20261001/`。

- `validator_manifest.json`：validator 版本及代码 / 冻结输入 SHA256。
- `manifests/validator-v2-smoke-20261001/run_plan.json`：六格计划，fingerprint `sha256:11d883ccdce705c6e529dede32dd4eea817ab5e93b90ae6fb5f365ea663206d8`。
- `raw/validator-v2-smoke-20261001/`：6 个 run 的完整原始记录。
- `processed/validator-v2-smoke-20261001/run_scores.jsonl` 和 `run_scores.csv`：含新增审计字段的评分。
- `processed/validator-v2-smoke-20261001/experiment_summary.json`：交付 / 完成数及逐条件指标。

运行与导出命令（真实模型运行会产生 API 调用）：

```powershell
.venv\Scripts\python.exe -m pilot01.experiment plan --registry data/pilot01/main_v1/case_registry_v1.json --out outputs/validator_v2_smoke_20261001 --experiment-id validator-v2-smoke-20261001 --repeat 1 --cases PILOT-POS-COC-0053
. .\scripts\Load-Pilot01ApiEnv.ps1
.venv\Scripts\python.exe -m pilot01.experiment run --registry data/pilot01/main_v1/case_registry_v1.json --out outputs/validator_v2_smoke_20261001 --plan outputs/validator_v2_smoke_20261001/manifests/validator-v2-smoke-20261001/run_plan.json --live
.venv\Scripts\python.exe -m pilot01.experiment score --registry data/pilot01/main_v1/case_registry_v1.json --out outputs/validator_v2_smoke_20261001 --experiment-id validator-v2-smoke-20261001
.venv\Scripts\python.exe -m pilot01.experiment summary --registry data/pilot01/main_v1/case_registry_v1.json --out outputs/validator_v2_smoke_20261001 --experiment-id validator-v2-smoke-20261001
```

不要直接覆写已经存在的运行目录；复跑使用新的 experiment ID / out。

## 一个旧记录的独立复核

只复放了一条历史节点记录，未做全量历史审计，未推测阻断后的下游反事实：

`pilot01_main_v1_20260924__PILOT-POS-COC-0053__E0__A1V1__r000`，Manager invocation 0。

旧 gate 的 `satisfied=True`；新 validator 检出 `evidence_not_opened`，完整核验为 false。缺少当前节点打开记录的是继承引用 `6ed1f77b684b:p0088`。该复核只使用记录的输出、当前节点 / invocation 的成功工具记录、原 memo 来源、policy 和合同，不依据 gold 修正判断。

独立 sidecar：`outputs/validator_v2_smoke_20261001/historical_boundary_example.json`。原始历史日志没有回写。该例与离线反例共同确认了所补漏洞的可观察行为。

本轮完成实现、边界测试、完整离线验证和六格 smoke。没有启动正式全量实验；正式批次应冻结新版实现并使用独立实验标识，不能将旧版与新版 gate 的结果合并视为同一处理条件。
