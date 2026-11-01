# V2 结果指标

入口为 experiment/scoring_v2.py，输入是完整冻结 plan、registry 和原始日志；不能只遍历完成的 run.json。

| 项目 | 定义及分母 |
|---|---|
| execution_status | completed、failed、interrupted、not_started 分别记录；没有最终 run.json 但有日志为中断 |
| action_accuracy_among_completed | 最终动作正确数 / completed；没有完成运行则为 null |
| correct_delivery_per_planned | 正确动作实际交付数 / 全部计划；失败、中断、未启动均未贡献交付，但不称为错误答案 |
| target_unknown | 节点是否保留 unknown，不等同错误 absent |
| incorrect_absent | gold 为 present，但节点报告 absent |
| final_fact_recovered | 正例最终 Compliance 报告目标 present，负例不适用 |
| e1_fact_delivery_per_planned | E1 最终事实恢复并交付数 / 全部计划 E1 |
| recovered_with_open_overlap_proxy | 事实恢复，且采用的段落由本节点实际打开、与标注证据段重叠；只是代理 |
| compliance_new_recovery | Compliance 报 present 且 Manager 未报 present，作为路径描述 |
| last_candidate | 每个节点最后一个已解析候选，拒绝候选不计最终交付 |
| node_validation_pass / node_audit_failures | 每节点检查是否通过及未通过项；与流程完成、最终判断正确性分别记录 |
| failure_category | 输出截断、工具超限、超时、其他传输错误、validator 拒绝、其他格式/协议失败 |

每节点还记录上游来源、自身打开、工具次数，以及标注段未返回/返回未打开/打开但未恢复等过程诊断；后者不能证明模型的内部因果机制。

同时提供 by_condition 和 by_contract_condition。重复 run 不作为独立合同，当前报告是描述统计，没有新增显著性检验或等效性声明。部分批次明确 batch_complete=false，计划交付率只作为进度快照。

token 细分缺失为 null，不填零；中断日志已返回的 usage 也计入 known_* 下界。实际消耗仍可能包含没有返回 usage 的在途请求。可选明确版本的 PricingTable 计算固定单价估计，缓存、时段等不能由该估计精确还原；未提供价格时费用为空。

当前 audit_only 批次的检查不通过不计为 execution failed，也不计 failure_category。汇总 validation_by_node 分别报告 evaluated/passed/failed；CSV 导出两节点 validation_pass 与 audit_failures。旧强制拦截批次的 validator_rejected 保持历史含义。

### 原文暴露与规定核验动作

检索命中、摘录暴露、完整段落打开和最终证据采用必须分别解释：

- 搜索事件与结果 ID 表明段落进入词法候选；
- 搜索结果中的逐字摘录及其 `excerpt_start_char` / `excerpt_end_char`
  表明该局部原文实际进入模型上下文；
- `open_source_span` 事件表明完整段落已返回，并可满足协议规定的完整段落读取动作；
- `relied_paragraph_ids` 及最终输出只表明可观察的证据采用，不能反推出特定内部推理过程。

因此“未调用 open”不等于“未见过任何原文”，“调用 open”也不等于已经正确理解。
V1 的指标应表述为是否完成规定的完整段落读取与证据登记，而不是笼统的“是否见过原文”。

`verification_mode: audit_only` 不取消 V1 给模型的核验要求，也不取消验证器和证据账本记录；它只表示检查失败不会自动阻断合法输出的交接和流程完成。因此当前 V 操纵是“是否施加核验要求并观察遵从”，不是运行时保证指定动作一定发生。
V0 与 V1 均由同一 validator v3 事后评估，以便比较实际行为；V0 未完成规定动作可出现在 `audit_failures` / `validation_pass` 中，但不会形成需要运行时执行的 V1 `failures`，也不会因为 `audit_only` 中止流程。
