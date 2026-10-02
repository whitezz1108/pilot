# Validator v2.1 后续实验报告（2026-10-02）

本轮完成了正文引用强化、旧轨迹复核、E1 检索诊断，以及 **6 个案例、30 次真实模型预实验**。强制 gate 已能按规则拦截，但现有流程在严格核验下交付率偏低，不宜直接用这批结果声称效果提升。

## 实现、冻结与验证

冻结实现 `131d3cac6e3e51da25856db1c7408b42c03c172e`，远端标签 `codex/validator-v21-preflight-20261002`。新版为 `validator_v2_1`，在原结构化来源检查之外，核对 `reason_summary` 的完整段落 ID 和 `pNNNN` 简写是否登记并自行打开；`uncertainties` 中的未采纳标识单独留痕。明确协议见 [validator_v21_protocol.md](validator_v21_protocol.md)。

完整离线 suite **1011 passed in 141.15s**；随后新增的 **2 项报告统计测试通过**，覆盖失败留在计划交付分母、未到达节点不算通过，以及缺失 raw 不得产出完整报告。报告脚本之后的失败分类调整也通过这两项测试。

原模型、prompt、policy、gold、memo、源代码运行版本和工具预算在整个真实批次中保持冻结，SHA256 校验通过。不增大 token / tool 预算，不自动重跑失败运行。选样在请求前固定：两个类别各取最长 / 最短正例，加各一个负例；repeat=1、seed=20261002。

## 程序完整性

计划 30，尝试 30，完成 **23**，失败 **7**；模型调用 **194**，工具调用 **138**。原始记录为 30 个 run、每个 5 个文件。

- 计划与 raw 运行集合完全匹配。
- A0 成功 source 调用为 **0**。
- 所有 run 的 gate 版本一致；运行代码 / 配置 / prompt / 冻结输入 hash 均未变。
- **53 条**已产生候选的节点审计，从当前 node / invocation、最终候选之前的成功工具记录复放，与 runtime outcome 逐字段一致。
- 被 A1V1 拒绝却仍写入 workflow state 的候选为 **0**。
- 每节点 / invocation 最多 **4** 次成功工具调用。

## 按条件报告交付和结果

| 条件 | 完成 / 计划 | 最终动作正确 / 已答 | 正确动作交付 / 计划 | E1 证据恢复 / 已完成 E1 | E1 证据恢复交付 / 计划 E1 |
| --- | --- | --- | --- | --- | --- |
| A0V0 | 10/10 | 5/10 | 5/10 | 0/4 | 0/4 |
| A1V0 | 9/10 | 9/9 | 9/10 | 3/3 | 3/4 |
| A1V1 | 4/10 | 4/4 | 4/10 | 不可估计：无完成 E1 | 0/4 |

这里的“E1 证据恢复”沿用冻结 scoring 定义，要求条款恢复及自身打开、出处可追溯和 gold span 重叠。它是已有确定性评分，不是新增人工语义核验。

**不能只说 A1V1 的已答正确率是 100%。** 它只交付了 4/10，四个 E1 均没有完整交付。0/4 指成功交付的有证据恢复为零，不是把四个失败计成四个已答语义错误。

两个负例在三个条件下均正常交付，错误升级为 **0/2**；其中 A1V1 的两次负例均满足强制程序核验并输出 `ACCEPT`。

## 审计与失败的区别

| 条件 | Manager 完整通过 / 已评估 | Compliance 完整通过 / 已评估 | 运行失败 |
| --- | --- | --- | --- |
| A0V0 | 不适用 | 不适用 | 0 |
| A1V0 | 7/10 | 2/9 | 1 次工具预算耗尽 |
| A1V1 | 6/9 | 4/5 | 4 次 validator 拒绝、1 次工具预算耗尽、1 次输出截断 |

未产生最终候选或未到达 Compliance 的节点不进入“已评估”分母。A1V0 虽可带缺项继续，其缺项仍按同标准记录：6 个 run 有未打开 / 未登记的正文引用，2 个 run 有目标未搜索，原因可重叠。

A1V1 的四个 gate 拒绝都含 `evidence_not_opened` 和 `narrative_reference_not_registered`。两个预算失败分别发生于 A1V0 / A1V1；输出截断发生于 A1V1 Compliance，在 16,384 token 上限结束，没有进行格式修复或自动重试。

## 已完成的遗漏原因排查

旧六格 smoke 的困难 COC 案例：A1V0 两个节点曾拿到与目标 gold 重叠的段落，但没有打开；A1V1 两个节点的实际检索均没有返回目标。目标位置只用于离线分析，未进入 agent 或 validator。

另外已复放旧正式实验全部 648 个 run、1211 个可复放节点。124 条旧 A1V1 获准节点按新增正文规则会有缺项；这是历史观察的独立审计，不是新版重跑失败率，不推测提前拦截后的下游反事实。详见 [旧轨迹复核与诊断](validator_v21_diagnostics.md)。

## 当前结论与下一阶段

**可以确认的是强制核验已真实落实，不能确认的是它提高了语义恢复效果。** 小样本中，严格程序要求暴露了来源登记不完整，以及 source / output 预算内无法交付的问题。

下一阶段应独立设计“来源登记与工具使用策略”的开发验证：让真正用于判断的来源进入结构化字段并自行打开，未打开的候选只作为明确的不确定事项；在原预算内检验检索与打开选择。若要增加预算或改 prompt / 检索器，要作为独立版本冻结并同时覆盖对照条件，不能改完后并入这 30 次结果。

完成这项开发验证后再决定正式复验规模；本轮没有直接启动 648 次全量重跑。六个按长度 / 类型目的性选取的案例、每格一次，不支持代表性或稳健的因果结论。

## 输出和复现

输出根目录：`outputs/validator_v21_preflight_20261002/`。

- `selection_manifest.json`：请求前固定的样本选择。
- `validator_manifest.json`：实现 commit 与 SHA256。
- `manifests/validator-v21-preflight-20261002/`：计划与 batch index。
- `raw/validator-v21-preflight-20261002/`：30 次原始运行记录。
- `processed/validator-v21-preflight-20261002/run_scores.csv`、`run_scores.jsonl`：全部运行评分。
- 同目录 `experiment_summary.json`、`preflight_metrics.json`、`trace_audit.json`：汇总、交付分母及节点复放。
- `preflight_report.md`：含逐运行结果的自动报告；`raw_sha256.json`：原始文件校验。
- `previous_smoke_replay.json`、`historical_full_replay.json`、`historical_replay_summary.json`：独立历史审计。

离线重建报告（不会调用模型）：

```powershell
.venv\Scripts\python.exe scripts/analyze_validator_preflight.py --registry data/pilot01/main_v1/case_registry_v1.json --out outputs/validator_v21_preflight_20261002 --experiment-id validator-v21-preflight-20261002
```

真实运行使用既有 CLI 的 `run --live --live-batch`，计划 ID 为 `validator-v21-preflight-20261002`。复跑必须另建输出目录 / experiment ID，保留本次一次执行的失败记录。
