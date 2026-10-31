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
