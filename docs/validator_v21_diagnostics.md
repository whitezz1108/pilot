# Validator v2.1：旧轨迹复核与遗漏诊断（2026-10-02）

这些数值来自**离线复放原始记录**，不是新增真实模型运行，不回写旧 raw / score，不推测 gate 提前阻断后的下游反事实。新版真实预实验另行报告。

## 上一轮六格 smoke 的缺口

上一版 v2 只查结构化来源，8 个有 source access 的节点都通过。按新增正文引用规则复放相同候选，8 个节点中只有 2 个完整通过，另外 6 个出现未登记或未打开的正文段落引用。A1V1 的 4 个已记录节点中，2 个按新增规则通过、2 个出现缺项。

这里的 4 个节点都来自旧 gate 的实际流程，不能把它们组合成“新版真实运行的完成率”：若新版在 Manager 拦截，原先的 Compliance 不会以原输入继续运行。

## E1 没恢复遗漏的具体原因

困难案例 `PILOT-POS-COC-0053` 的冻结目标证据与段落 `6ed1f77b684b:p0060`、`6ed1f77b684b:p0088` 重叠。它们仅用于离线诊断，不提供给 agent 或 validator。

| 旧 smoke 条件 | 节点 | 实际返回的候选中有目标证据段落重叠 | 实际打开目标重叠段落 | 离线诊断 |
| --- | --- | --- | --- | --- |
| E1 A1V0 | Manager | 有 | 无 | 返回后没有选中并打开 |
| E1 A1V0 | Compliance | 有 | 无 | 返回后没有选中并打开 |
| E1 A1V1 | Manager | 无 | 无 | 实际执行的检索没有返回目标 |
| E1 A1V1 | Compliance | 无 | 无 | 实际执行的检索没有返回目标 |

A1V1 Manager 打开的是修订 / 同意相关段落 p0101 和含 “for convenience” 的标题说明 p0107；Compliance 只打开了 p0107。按类别名称检索、再打开返回的段落，并不保证找到法律含义对应的目标条款。模型最后把 COC 保留为 `unknown`，不是确认 `absent`。

当前查询使用既有 BM25 词面检索，原预算是每节点 4 次成功工具调用，search / open 共用。诊断将“检索没返回”和“返回但没打开”分开；没有在本轮根据 gold 改 query、检索器、提示词或预算。

## 全部 648 条历史运行的复放

读取旧正式实验的 648 个 run，其中 590 完成、58 失败。只有已产生解析候选并留下原 verification 记录的节点能复放，共 1211 条；未到达该节点或未产生最终候选的不虚构审计值。

| 条件 | 有记录可复放的节点 | 按新版完整通过 | 旧 gate 允许、按新版有缺项的节点 |
| --- | ---: | ---: | ---: |
| A0V0 | 430 | 不适用 | 不适用 |
| A1V0 | 395 | 237 | 158 |
| A1V1 | 386 | 261 | 124 |

A1V0 原本就允许不核验，158 是同标准审计发现缺项，并不说明其运行违反可选条件。A1V1 的 124 条旧允许节点中，124 条有未打开的引用，117 条还有正文引用未登记；原因可以重叠。其余存在旧 gate 已拒绝的记录，不把它算作新增发现。

这证明强化项能从真实旧日志中识别出程序差异；不能证明识别质量提高，也不能把节点数量当作运行数量或合同数量。

## 可复现与输出

```powershell
.venv\Scripts\python.exe scripts/validator_trace_audit.py --registry data/pilot01/main_v1/case_registry_v1.json --raw outputs/validator_v2_smoke_20261001/raw/validator-v2-smoke-20261001 --out outputs/validator_v21_preflight_20261002/previous_smoke_replay.json
.venv\Scripts\python.exe scripts/validator_trace_audit.py --registry data/pilot01/main_v1/case_registry_v1.json --raw outputs/pilot01_main_v1/raw/pilot01_main_v1_20260924 --out outputs/validator_v21_preflight_20261002/historical_full_replay.json
```

完整复核保留各节点原 gate 状态、新版复放结果、未登记引用、实际打开列表及离线检索代理。复放限定当前 node / invocation，且只读取最终候选之前成功的工具记录。`uncertainties` 中未采纳的标识不自动转成证据。

对本轮 v2.1 新运行，同一个脚本逐字段比较 runtime 与复放 outcome，不一致直接报错；已用 synthetic 实验验证拒绝候选留存和 raw 文件不变。
