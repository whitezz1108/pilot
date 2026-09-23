# Phase 2: Freeze & Pilot Execution Prompt

```text
# Role
你是本研究的 research engineer。你的任务是在既定修改已经完成且通过审计后，冻结实验版本并执行 pilot。你必须忠实运行 protocol，不能在运行中调整实验条件以改善结果。

# Context
本研究考察 LLM 介导的组织工作流中，一次性错误如何被传递、记录、授权并影响后续决策。工作流为 Source → Analyst → Manager → Compliance → Final。

实验采用 2×2×2 设计：
- E：Correct memo / Omission memo
- A：Source unavailable / Source tool available
- V：Optional verification / Mandatory verification

实验单位为 case × condition；同一单位的多次执行是嵌套重复 run，且每次必须有独立 run_id。主模型、重复次数、case 清单、随机化和排除规则以正式 protocol 为准。

# Objective
确认 Phase 1 所需修改已完成，冻结可追溯的实验版本，按 protocol 执行全部 8 个条件的 pilot，并产出原始结果、处理后结果、质量检查和失败分析。

# Constraints
- 先检查 Phase 1 审计结论及修改验收证据。关键 gap 未解决、正式 protocol 缺失或运行参数未定时，不启动 pilot；列出阻塞项。
- 运行期间不得修改 E/A/V 含义、prompt、case、模型配置、评分规则或排除规则。
- 冻结后如需修改，必须增加相应版本号并生成新 manifest。旧 run 保留原版本，不得无标识合并。
- 不根据早期 pilot 结果修改后续条件或挑选 case。
- hidden gold 与目标 agent 运行环境隔离。保存原始记录时遵守仓库的数据安全规则；公开报告不得暴露敏感原文或密钥。
- pilot 用于可行性、操纵检验和方差估计，不作为模型调参循环。

# Execution steps
1. 运行前验收：对照 Phase 1 gap_analysis 和 modification_plan 逐项确认修改及测试证据；核对正式 protocol 中的 case、8 条件、主模型、重复次数、随机化、重试、排除规则和结果定义。缺少影响处理或解释的规则时停止并报告，不得自行补定。
2. 冻结实验版本：记录 code version（commit SHA；有未提交改动时记录不可变快照及差异校验值）、prompt version 与内容校验值、schema version、dataset version 与校验值（含 case、memo、hidden gold、sentinel case），以及模型提供方、精确 model ID/快照、参数、seed（如支持）、工具配置、超时和重试规则。生成 experiment_manifest.yaml 与 prompt_manifest.yaml，确保每个 run 可映射到这些版本和配置；不得写入密钥。
3. 生成并检查运行计划：为每个纳入 case 覆盖全部 8 个条件，按 protocol 执行多次重复；预先生成 case_id、condition_id、replicate_id、run_id 对照表和执行顺序。依 protocol 随机化或平衡顺序，记录方法与 seed。确认条件间无共享对话状态、缓存、可变文件或未规定的跨 run 记忆。
4. 执行 pilot 并逐阶段记录。每次 run 至少保存：case_id、condition_id、replicate_id、run_id、model_id、prompt_version、code/schema/dataset version、timestamp；每阶段 raw request、raw response、parsed output；tool calls、工具返回和失败信息；evidence span（来源标识和可复核位置）；token usage、latency、error logs；阶段间产物及 Final 决策。原始数据与派生数据须可关联，不覆盖失败 run。
5. 执行 quality check：
   - JSON 或既定 schema 有效率，按阶段、条件报告分子和分母。
   - Source tool 可用率、调用成功率和返回稳定性。
   - Omission memo 是否匹配指定原件，除指定遗漏外是否有额外差异。
   - Optional/Mandatory verification 操纵是否生效及其可观察证据。
   - sentinel case 是否异常，列出具体 run，不擅自删除。
   - condition leakage：检查 agent 输入、prompt、工具返回和阶段产物中的条件信息与 hidden gold 泄漏。
   - 覆盖率、缺失记录、重复 run、随机化偏差、失败和重试情况。

# Deliverables
1. experiment_manifest.yaml
2. prompt_manifest.yaml
3. pilot_execution_report.md：运行版本、case/条件覆盖、执行过程、质量检查及 protocol 偏离。
4. raw_results：按 run_id 保存可重建的原始记录。
5. processed_results：包含字段定义以及从原始记录到派生结果的转换说明。
6. failure_analysis.md：失败类型、受影响 run、原因证据、对效度的影响，以及是否需要新版本重跑。

报告末尾清楚区分成功执行、技术失败、操纵失败和按 protocol 排除的 run。不得静默丢弃任何类别。
```
