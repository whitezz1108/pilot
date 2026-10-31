# 32-run 预试失败诊断（2026-10-18）

## 结论

32 个已结束，21 个通过，11 个失败，完成率 65.6%。10 个失败由 validator_v3 的逐目标搜索门槛触发，1 个为工具请求格式错误后修复失败。本轮 474 条 HTTP 记录全部为 200；474 条模型返回的 finish_reason 全部为 stop，未见输出截断。不能把完成率解释为判断正确率。

## 分组对比

|条件|本轮完成/总数|上一轮完成/总数|
|---|---:|---:|
|D0|5/14（35.7%）|12/23（52.2%）|
|D1|3/5（60.0%）|6/13（46.2%）|
|D2|13/13（100%）|28/28（100%）|

两轮存在重复合同与条件，且各组样本并不配对平衡；上述数字只是描述，不能作为 D2 提升判断质量的因果证据。上一轮 18 个失败全部为核验拦截，其中 16 个涉及未逐目标搜索（含 1 个同时有其他核验问题）。

## 本轮 11 个失败

|合同|材料/条件|中止节点|直接原因|该节点已用工具次数|
|---|---|---|---|---:|
|CASE-0004|E0/D0|manager|缺少目标搜索：Change Of Control|6|
|CASE-0005|E0/D0|manager|缺少目标搜索：Change Of Control|5|
|CASE-0008|E0/D0|manager|缺少目标搜索：Change Of Control|5|
|CASE-0011|E0/D0|manager|缺少目标搜索：Change Of Control|4|
|CASE-0017|E1/D0|compliance|缺少目标搜索：Change Of Control|3|
|CASE-0023|E1/D0|compliance|缺少目标搜索：Termination For Convenience|9|
|CASE-0029|E1/D1|compliance|缺少目标搜索：Termination For Convenience|5|
|CASE-0040|E0/D0|manager|缺少目标搜索：Termination For Convenience|12|
|CASE-0058|E1/D0|manager|工具请求缺少 tool_request 外层；被当作最终答案，格式修复仍失败|8|
|CASE-0061|E0/D1|manager|缺少目标搜索：Termination For Convenience|7|
|CASE-0061|E1/D0|compliance|缺少目标搜索：Termination For Convenience|5|

10 个被核验拦截的节点均已打开原文、输出结构化答案，动作均为 ESCALATE；工具请求只用了 3–12 次，未用尽 16 次额度。这不等于它们的全部条款判断已经验证正确。6 个在 Manager 被拦，4 个在 Compliance 被拦。

## 具体例子：CASE-0008 / E0 / D0

Memo 给出 Change Of Control 的原文位置 0545fc6a00b6:p0057。Manager 直接打开此段，又对 Termination For Convenience 搜索两次、打开两段，最终提交 ESCALATE。validator 仍因缺少 Change Of Control 的带标签搜索而拒绝提交。原文打开和搜索是两种工具动作；当前规则不允许前者替代后者。

## 需要处理的两类问题

1. 确定的实现缺陷：tool_loop.py 中提供的 JSON 示例多一个右花括号，json.loads 实测报 Extra data。应修正并验证示例。不能断言它造成了本轮唯一格式失败。
2. 需要明确的实验设计选择：逐目标搜索是强制流程规则，不是对语义核验质量的证明。D2 被告知规则，D0/D1 没有完整细则，因此当前完成率很大程度反映是否符合该程序。若研究这种程序透明度，保留规则但把指标命名为程序通过率；若研究实质核验，应另行设计允许直接打开已知原文的判定标准。不要简单删除全部检查。

建议界面将程序核验拒绝、模型格式失败、工具预算耗尽、API/网络错误分开显示。判断准确率与流程通过率分别报告；Manager 被拦导致 Compliance 未执行，应保留为流程中止，不能推断其后续判断。改规则或解析器需新版本、新批次，历史结果不覆盖。当前无需为这些失败提高工具或 token 上限，也没有证据表明降低并发能解决它们。

## 证据位置

- 本轮原始数据：outputs/experiment_console/raw/console-preflight-20261017T190650-2c8543e3
- 上轮原始数据：outputs/experiment_console/raw/console-preflight-20261017T185602-8e7aa493
- 强制搜索规则：src/pilot01/source/verification_v3.py:10–38
- 核验失败直接中止节点：src/pilot01/workflow/nodes/llm_agent.py:304–312
- D0/D1/D2 告知差异：src/pilot01/workflow/nodes/disclosure.py:13–21
- JSON 示例缺陷：src/pilot01/workflow/nodes/tool_loop.py:152–153

本次只诊断并生成文档，未改实验规则、提示词、原始日志，未发起模型调用。
