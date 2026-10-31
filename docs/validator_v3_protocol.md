# Validator v3：来源与输出契约

实际规则定义位于 `src/pilot01/source/verification_v3.py` 的 RULES 与 evaluate_verification_v3。D2 告知文字直接使用 RULES，三个 D 条件运行同一个函数。

1. 输出目标集合恰好等于政策要求，不能漏报或多报；缺失记入检查结果。
2. rule_id 与政策编号一致。
3. 每个目标有本节点、本次 invocation 的成功 target_category 查询；空结果仍是搜索尝试，不等于证明不存在；翻页不会改变目标。
4. 至少声明采用一个来源；全部 opened 和 relied ID 均有本节点实际打开记录，relied 同时登记在 opened 列表。
5. 来源属于本合同且实际已见；inherited ID 必须由上游交付，不能用自己搜索到的 ID 冒充继承。
6. verification_basis=self_checked 并与打开记录一致；显式提供 relied 和 candidate 列表；同一 ID 不同时作为候选和采用证据。

`inherited_source_ids` 是来源起点，可以和 `opened_paragraph_ids` 重叠；`relied_paragraph_ids` 是实际采用；`candidate_source_ids` 是未采用候选。正文中段落编号仅作诊断，不用字符串位置推断采用关系。所有 ID 的存在和已见检查仍执行。

Compliance 交接包含 Manager 输出中全部来源列表，均可登记为上游已见；只有 Compliance 自己的 open 调用才计作自己的打开。账本按节点和 invocation 隔离。

本检查不读取 gold，不判断 query 的语义相关性或段落对判断的支持程度；unknown 不豁免流程要求。当前 D 实验采用 audit_only：未通过项写入 validation_pass=false、audit_failures 和 failures，节点答案继续交接，最终输出照常计为交付。检查结果不反馈任何节点，也不触发重做。运行是否完成与检查是否通过分别统计。旧强制拦截批次仍保留 validator_rejected 分类。旧 verification.py 保留 v2.1 解释。
