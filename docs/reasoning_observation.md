# 本地模型思考记录

现有 `execution_engineering_dual_gpu_v1.yaml` 已启用思考观测；另提供相同设置的独立配置，
新工程批次也可使用 `--execution-config config/execution_engineering_reasoning_v1.yaml`。
它保持 Manager/Compliance 分卡、单 run、零 HTTP 重试，增加 `stream: true` 和
`stream_options.include_usage: true`。不修改思考开关、提示词、温度或 16K 输出上限。
配置里的 tokenizer 路径属于 t640 主机；其他机器需改为实际模型的 tokenizer.json。
实验 Python 环境需要可选依赖 `pip install '.[reasoning]'`。

## 每次调用记录

- `raw_response`：原来的最终正文或工具指令，继续用于业务解析。
- `reasoning_content`：服务端返回的思考文本，不传给下游角色或格式修复提示词。
- `provider_response`：保留服务端响应；流式模式附 `stream_events`，逐事件保存内容和单调时钟到达偏移。
- `reasoning_observation.tokens/token_source`：优先使用服务端 reasoning_tokens；缺失时对思考文本使用实际 tokenizer 重算，标记 `local_tokenizer_reencode`，保存 tokenizer SHA-256。重算不含思考分隔符，可能与生成时的 token 边界不同，不能称为服务端精确计数。
- `usage.reasoning_tokens`：只接收服务端原生计数。客户端重算不会冒充原生 usage，也不额外加到 completion_tokens 或费用中。
- `first_reasoning_seconds / last_reasoning_seconds / first_content_seconds`：从实际 HTTP 尝试开始，到相关流式分片到达的时间。
- `reasoning_phase_seconds`：首个思考分片至首个正文分片的间隔，含网络/缓冲，排除首个思考前的排队和 prefill；不是 GPU 纯计算时长。
- `reasoning_span_seconds`：首个至最后一个思考分片的间隔。
- `stream_complete`：是否收到了 `[DONE]`。截断、超时、流中断保存已收到的思考及分片，不自动重跑。缺失正文边界时阶段时长为 null；缺失 token 数也为 null，不能按零处理。

没有流式观测的历史响应无法恢复思考耗时。仅有最终正文的旧日志也无法恢复原始思考。
本功能不以“输出 tokens / 平均速度”伪造思考时长。

## 查询、展示和导出

原始证据写入每轮 `model_calls.jsonl`；`run_scores_v2.jsonl` 中新增独立
`reasoning_observation` 汇总，保留缺失计数及 token 来源。原有 usage/费用口径不变。
实验记录页的“过程回放”显示思考 tokens、观测时长、完整思考内容与原始响应证据。
长思考和流式证据放入 `model_responses` 分类，按调用懒加载，避免挤入列表或 SQLite 索引。
分类 ZIP 导出/导入可无损还原新增字段，历史字段保持兼容。

必须新建计划和实验 ID：源码与执行策略变化后，不用新版本续跑冻结的旧批次。
`urllib` 的 timeout 仍是阻塞网络操作超时，流式读取下不是整次请求墙钟上限。

## 2026-10-27 双卡实测

两端 SGLang 0.5.22 均直接返回顶层 `usage.reasoning_tokens`，旧适配器仅读取嵌套
`usage.completion_tokens_details.reasoning_tokens`，会漏记这个字段。新适配器兼容两者。
两张卡分别完成一个独立的小型 JSON 算术请求，各记录到 53 个服务端原生思考 tokens、
183 个思考字符、61 个总输出 tokens；思考阶段客户端观测时长约 1.82 / 1.88 秒。
这些是记录功能的工程探针，不是合同实验结果，也不证明复杂任务质量。
