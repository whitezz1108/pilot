# Phase 3: Pilot Analysis & Research Report Prompt

```text
# Role
你是本研究的 research engineer。你的任务是分析已经冻结并完成的 pilot，形成可供研究团队讨论的证据报告。当前阶段不继续调 prompt、改条件或优化模型。

# Context
研究问题是：LLM 介导的组织工作流如何把一次性模型错误转化为持续存在、被记录、被授权并影响后续决策的“组织事实”。

工作流：Source → Analyst → Manager → Compliance → Final

2×2×2 条件：
- E：Correct memo / Omission memo
- A：Source unavailable / Source tool available
- V：Optional verification / Mandatory verification

分析使用 Phase 2 冻结的 manifest、raw results、processed results、quality check 和正式 protocol。case × condition 是实验单位；重复 run 嵌套于该单位，不得把重复 run 当作新的独立 case。

# Objective
描述 pilot 的可行性、操纵是否成功、各阶段错误的存活与纠正模式、Final 决策表现及资源成本；识别 confirmatory study 需要处理的设计问题。

# Constraints
- 不修改原始结果、实验条件、prompt、评分标准或排除规则。
- 使用 protocol 中预先定义的指标。若指标没有操作化定义，标记“待定义”；可以提出候选定义，但不得将其称为预注册指标。
- 分别报告技术失败、操纵失败、缺失和排除；每项分析须明确分母。
- 比较若属探索性，须明确标记。pilot 不用于宣称理论假设已获证明，不以证据不足的因果语言解释差异。
- 区分自动评分与人工判断。未经人工复核的“授权”“组织事实”等语义判断，不得写成已确认事实。

# Execution steps
1. 核实输入：校验 experiment_manifest.yaml、prompt_manifest.yaml、数据版本和 run_id 关联；检查 8 条件覆盖、各条件样本数、重复次数、缺失与失败；不得无标识混合不同版本。
2. 构建阶段轨迹：逐 run 追踪错误是否进入 Analyst、Manager、Compliance 和 Final。分别标记错误是否被传递、写入持久化记录、获得流程中的授权、影响 Final 决策，并给出可追溯的原始记录依据。标记首次纠正阶段、纠正是否持续、错误是否复现；无法自动判定的类别保留并列出人工复核需求。
3. 按 protocol 分析以下结果：error survival across stages、correction stage、final decision accuracy、evidence usage（区分引用 memo 与使用 Source tool/原始来源）、source access effect、verification obligation effect、false escalation、cost metrics（token、latency、工具调用、失败/重试成本）。每项说明定义、分子、分母、缺失处理、统计单位和方法。按条件报告描述统计；若 protocol 允许，可给出探索性因子对比和不确定性范围。不得将重复 run 当成独立样本。
4. 检查异常与效度边界：追踪 sentinel case、操纵失败、condition leakage、工具不稳定、解析失败和异常高成本 run。区分可能的实验机制、技术故障、数据问题和评分歧义。说明 pilot 对可行性和 variance 能提供什么信息，以及不能支持什么结论。
5. 提出 confirmatory study 准备事项：指出需要澄清的操作化定义、人工复核方案、样本量或方差估计及运行稳定性要求。所有建议标记为“下一版本提案”，不得回写本次 pilot 的条件或分析规则。

# Deliverables
生成 research_report.md，至少包括：
- experiment setup 与冻结版本
- 8 条件表、case 数、重复次数和实际运行覆盖
- methodology：实验单位、随机化、操纵、测量、分析集与缺失处理
- descriptive statistics：逐阶段及逐条件结果，明确分母
- findings：由原始记录和统计结果支持的有限结论
- anomalies 与具体 run_id
- limitations：pilot 样本、操纵、测量和因果解释边界
- suggestions for confirmatory study

另生成 presentation_summary.md，供导师汇报，至少包括：
- 已完成的审计、冻结、执行和分析
- 系统架构简述：Source → Analyst → Manager → Compliance → Final，以及记录和授权发生的位置
- pilot 主要发现和异常，并注明证据级别
- 下一步 confirmatory study 的具体准备事项

关键表格、结论和异常都应能通过 run_id 追溯到处理后结果及原始记录。结尾明确说明：pilot 用于验证可行性、发现设计问题和估计 variance，不能据此声称理论假设已被证明。
```
