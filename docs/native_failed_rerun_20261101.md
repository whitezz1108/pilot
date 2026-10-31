# 五次历史异常的原生工具协议实机重跑报告

日期：2026-11-01  
最终协议代码版本：`128b353952d59b37d27942c0bd44a7874be60ba0`  
分支：`codex/typed-tool-protocol`

## 实验边界

- 模型：Qwen3.8-27B，BF16 + MTP。
- 推理设置：`reasoning_effort=xhigh`，`max_output_tokens=32768`，单次请求超时 360 秒。
- 采样：`temperature=1.0`、`top_p=0.95`、`top_k=20`、`min_p=0`、`presence_penalty=0`、`repetition_penalty=1`；每个冻结案例沿用原 seed。
- GPU2 功率上限：200 W；隔离端点：`127.0.0.1:30002/v1`。
- 每节点工具预算：16 次。Manager 与 Compliance 分别计数。
- 五个历史异常的原始记录未覆盖；每次诊断和最终重跑均使用新的 experiment/run ID。

冻结输入清单：`/home/evan/ai/benchmarks/protocol_fix_20261031/frozen_manifest.json`  
历史异常原文：`/home/evan/ai/benchmarks/xhigh_sampling_20261031/frozen_new_anomalies/`

## 重跑过程中发现并修复的协议缺口

1. SGLang 0.5.22 即使收到 `parallel_tool_calls=false`，仍可能在一轮返回多个工具调用。runner 原先只会事后拒绝。
   - 修复：工具声明为 strict；runtime 正式支持一轮多个原生只读调用，按 provider 顺序执行，完整保留所有 call ID 和 tool 结果。
   - 提交：`44b0dff`、`4505c47`。
2. native 工具结果没有回传 `used/limit/remaining`，模型无法知道工具预算位置。
   - 修复：每条 `role=tool` 消息携带机器可读的 `tool_budget`。
   - 提交：`a57c1c6`。
3. SGLang 偶发返回缺失 call ID 的结构化工具调用；旧 adapter 直接失败，且没有把原始 stream envelope 写入日志。
   - 修复：保留原始 provider envelope；仅为关联键生成确定性 ID，并标记 `call_id_origin=adapter_generated`。工具名和参数仍按原样严格验证。
   - 提交：`27cac81`。
4. 工具预算归零后，下一轮仍继续向模型提供工具定义，模型可能请求第 17 次调用。
   - 修复：预算归零后的下一轮切换为 final-only，不再提供工具，并要求依据已取得证据提交答卷； unresolved target 必须标为 `unknown`。
   - 提交：`128b353`。

相关离线回归：190 项通过。此前全量 `pytest -q` 快照仍有 15 项旧测试/旧产物问题，本轮没有把全量套件宣称为绿色。

## 五条最终有效重跑

| 历史异常 | 原异常类 | 新 experiment ID | 实际代码 | 状态 | 总耗时 | 模型请求 | 工具调用 M/C | 正式答卷 M/C |
|---|---|---|---|---:|---:|---:|---:|---:|
| `3cde17a9` | `tool_request:null` | `native-rerun-multitool-20261101-3cde17a9-e1` | `4505c47` | completed | 228.330 s | 12 | 9 / 11 | 1 / 1 |
| `78353028` | cursor/query mismatch | `native-rerun-budgettelemetry-20261101-78353028-e0` | `a57c1c6` | completed | 599.887 s | 14 | 14 / 15 | 1 / 1 |
| `988899e6` | `tool_request:null` | `native-rerun-budgettelemetry-20261101-988899e6-e1` | `a57c1c6` | completed | 506.456 s | 12 | 11 / 11 | 1 / 1 |
| `d1e72e8b` | cursor/query mismatch | `native-rerun-budgettelemetry-20261101-d1e72e8b-e1` | `a57c1c6` | completed | 792.343 s | 16 | 15 / 13 | 1 / 1 |
| `f856360e` | `tool_request:null` | `native-rerun-finalonly-20261101-f856360e-e0` | `128b353` | completed | 317.077 s | 14 | 11 / 15 | 1 / 1 |

合计：5/5 完成；68 次模型请求；125 次工具调用；五条有效重跑的模型任务墙钟时间合计 2444.093 秒（40 分 44.093 秒）。

五条最终有效重跑均为：

- Manager 与 Compliance 各一份结构化正式答卷；
- `tool_request:null = 0`；
- cursor/query mismatch = 0；
- tool error = 0；
- parse error = 0；
- format repair = 0；
- `finish_reason=length = 0`；
- `finish_reason=stop` 但无正式答卷 = 0。

原始结果目录：`/home/evan/ai/benchmarks/native_tool_failed_rerun_20261101/tasks/raw/`

## 中间失败记录仍保留

中间验证没有覆盖或删除，包括：

- 首轮因一轮两个工具调用被顺序 runner 拒绝；
- strict 单调用约束在 SGLang 0.5.22 上仍未被兑现；
- 缺少预算遥测时用满 16 次后请求第 17 次；
- SGLang 偶发缺失 tool call ID；
- 预算归零后仍提供工具导致 Compliance 请求第 17 次。

这些记录用于说明每个协议修正的直接触发证据，不能与最终五条 completed 结果混为一组统计。

## 结论边界

- 这次结果证明：在逐步演进的原生工具协议、分页 cursor 绑定、预算遥测和有界状态机下，五个冻结历史异常任务都能完整完成，而且过程日志中未再出现原三次 null 或原两次分页错误。
- 这不等于证明 Qwen3.8/SGLang 的底层随机异常已经消失。最终有效重跑没有再次触发缺 ID，也没有实际进入 final-only；这两条修复目前由冻结结构的离线测试覆盖，而不是由最终五条实机结果直接触发验证。
- 因而可以宣称“实验客户端已能处理已观察到的协议异常，并在这五条冻结任务上 5/5 完成”，不能宣称“SGLang 已修复”或“未来故障率为 0”。
