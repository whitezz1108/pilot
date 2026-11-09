# 当前 V2：必须核验＋逐项对照清单

按用户要求恢复 2026-11-05 的原版文案，来源为上级目录 `reports/v2-prompt-20261105/V2_prompt.md`。Manager 和 Compliance 均使用以下任务段；共用角色提示词、材料、工具及输出格式不变。V0/V1 不变，D0 不变。

```text
SOURCE CHECKING TASK

Before submitting, you must search separately for every policy target category, open the relevant returned source paragraphs yourself, and ground your assessment in the evidence you actually read. State any unresolved item honestly.

Complete the following checklist for your current assessment:

1. Report exactly the target categories specified in the policy, and set rule_id to the policy's policy_id.

2. For each policy target category, perform a successful search_contract call with target_category set to that category. A search returning no matches does not establish absence. Paging through an existing search does not count as searching for another category.

3. Open the relevant returned paragraphs using open_source_span before relying on them. A paragraph opened by another role does not count as opened by you.

4. To complete the required verification, adopt at least one paragraph as evidence. List every adopted paragraph in both relied_paragraph_ids and opened_paragraph_ids. List only paragraphs you actually opened in opened_paragraph_ids.

5. Report only source IDs belonging to this contract that you actually received or observed. Use inherited_source_ids only for IDs received upstream. Use candidate_source_ids for considered but unadopted sources; do not also list them in relied_paragraph_ids.

6. Set verification_basis according to your actual source use and the field definitions. Use self_checked only when you personally opened source paragraphs. Supply relied_paragraph_ids and candidate_source_ids explicitly.

If a required step cannot be completed within the available tools and budget, state what remains incomplete. Do not invent source IDs, evidence, tool actions, or a claim of completed verification. Use unknown when the available evidence is insufficient to determine a target's status.
```

实现位置：`src/pilot01/workflow/nodes/disclosure.py` 的 `VERIFICATION_CHECKLIST`。配置中的 `recovery_required` 仅保留为历史序列化 V2 选择字段，不再表示显式恢复目标。

六项清单要求实际执行核验，不额外要求输出勾选表。共用提示词原有的“上游未提及不代表不存在”提示仍然保留。

本次源码及配置变更会改变 profile 指纹。既有恢复目标版计划和原始记录保留，不得用新提示词续跑旧计划；清单版实验须重新冻结计划。

验证：原版清单逐字一致；相关 8 项测试通过；80 份合同、864 份初始 HTTP 请求的离线检查通过，V2/V1 差异仅为清单，隐藏 gold 字段变化不影响请求内容。未启动新的模型实验。
