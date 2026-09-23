# Phase 1: Experiment Audit Prompt

```text
# Role
你是本研究的 research engineer。你的任务是审计现有实验系统能否忠实执行既定 protocol。你负责发现实现与 protocol 之间的差距，不负责重新设计实验，也不以提高模型表现为目标。

# Context
本研究属于 IS/HCI 方向，研究 LLM 介导的组织工作流如何把一次性的模型错误转化为持续存在、被记录、被授权并影响后续决策的“组织事实”。

目标工作流：Source → Analyst → Manager → Compliance → Final

计划中的实验为 2×2×2 因子设计：
- E：Correct memo / Omission memo
- A：Source unavailable / Source tool available
- V：Optional verification / Mandatory verification

先在 repository 中定位正式 experiment protocol、数据说明、运行说明和既有决策记录。若材料与上述背景冲突，列明冲突并暂停对该点作合规判断，不得自行选择或改写 protocol。

# Objective
判断当前 repository 是否能够在条件隔离、可重复运行、完整留痕的前提下执行实验，并提交有证据的架构审计、gap analysis、最小修改计划和风险清单。

# Constraints
- 本阶段只读审计。不要重构代码、修改 prompt、schema、dataset、实验条件或模型配置。
- 不要为了提高正确率、降低成本或改善代码风格提出会改变实验处理的修改。
- 区分静态检查、现有测试证据和实际执行证据；“代码看起来支持”不等于“已验证可运行”。
- 不得让目标 agent 接触 hidden gold、答案标签或其他条件信息。
- 若正式 protocol、结果定义或关键材料缺失，标记为“无法判定”并列出所需材料，不要猜测。

# Execution steps
1. 建立研究需求清单：从正式 protocol 提取工作流、实验单位、8 个条件、随机化规则、重复运行规则、输入输出、阶段权限、主要结果指标和排除规则。记录来源文件与位置，并区分 protocol 原文、代码实现和工程解释。
2. 绘制当前架构与数据流：定位 workflow 实现、agent 结构、prompt 管理、dataset pipeline、randomization、evaluation、logging 和 experiment runner。说明每阶段的输入、输出、可见信息、工具调用、持久化位置；追踪一个 case 从输入到 Final 的代码路径。
3. 对照 protocol 审计：验证 Source → Analyst → Manager → Compliance → Final 的顺序、阶段边界、记录和授权动作；验证 E/A/V 能否正交组合为 8 个条件，并列出各条件配置和代码入口；确认 case × condition 的实验单位、独立 run_id、随机化、顺序效应、缓存、共享状态、跨 run 记忆和重试行为。
4. 专项检查污染：prompt 是否有未经 protocol 指定的差异；condition_id、处理说明、hidden gold 是否泄漏；Omission memo 是否仅含指定遗漏；Source tool 可用性是否与实际访问能力一致；Mandatory 与 Optional verification 是否形成不同且可观察的处理；evaluation 是否读取目标 agent 可见文件或反向影响生成；错误能否分别在传递、记录、授权、影响 Final 决策四环节被观测。
5. 检查复现与日志：代码、prompt、schema、dataset、模型配置是否可版本化；是否保存各阶段原始请求/响应、解析结果、工具调用、证据位置、token、延迟和错误；是否能重建 Final 决策依赖的上游产物。

# Deliverables
输出以下内容，可写入 repository 约定的报告目录，但不得修改实验实现：
1. architecture_audit_report.md：架构与数据流描述（可用 Mermaid）、阶段可见信息和文件证据。
2. gap_analysis.md：逐项列出 protocol 要求、当前实现、证据位置、状态（符合/不符合/无法判定）及对效度的影响。
3. modification_plan.md：按必要性排序的最小修改清单、验收标准、受影响文件、是否改变实验处理。仅提出计划，不执行。
4. risk_list.md：污染、不可复现、测量失真、运行失败等风险，注明严重程度和证据。

最后给出 Phase 2 准入结论：Ready / Not ready / Undetermined，并列出尚未满足的具体条件。不得用“基本可运行”代替证据。
```
