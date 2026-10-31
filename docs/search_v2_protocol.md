# 合同搜索 v2

仅显式的 clean v2 profile 使用 PagedSourceTools；旧 SourceTools 接口保留。

```json
{"tool_request":{"tool":"search_contract","arguments":{"query":"change control","target_category":"Change Of Control"}}}
```

响应含最多五条 results（rank、段落 ID、score、excerpt），offset、range_start/end、total_matching、has_more、next_cursor、new_result_count、already_seen_ids。snippet 默认 400 字符，不当作完整打开。

下一页保留同一 query 与 target_category，增加返回的 cursor；顺序为 BM25 降序、同分按段落序号。cursor 是本节点本次 invocation 的随机不透明令牌，绑定本合同排名；换 query、目标、节点或 invocation 均不能复用。完整排名在当前工具实例中缓存，未返回的段落不会写成已见。

重复不带 cursor 的第一页请求给出 repeat_page 提示和相同 next_cursor，不再次返回五段正文；仍消耗一次额度。显式重放某个 cursor 返回同一页候选。换 query 可得到已见 ID，响应标记重复。页大小由冻结配置控制，模型不能借 page_size 参数放大信息量。

open_source_span 仍按 paragraph_id 打开一个完整段落。搜索、分页、打开、无效请求共享每节点 16 次额度。目标检索登记是过程记录，不证明 query 有效或目标不存在。

`scripts/diagnose_retrieval_cost.py` 比较类别名 top5、类别名两页、全局定义改写的候选覆盖与返回字符量；没有自动选择“最优策略”。该诊断不调用模型，不能把它的字符量当实际 token 账单，不能拿旧数据调参后的结果当新确认性证据。
