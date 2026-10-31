# 实验并发与第二个API

2026-10-22 补充：可通过独立执行配置的 `role_slots` 将 Manager 和 Compliance 固定到不同端点。下文描述的默认按 run 分配仍保留；工程预算、启用方式和验证边界见 [双卡工程核查](dual_gpu_engineering_check_20261022.md)。

日期：2026-10-18。本次修改执行方式，不运行正式实验，也不决定尚待确认的A/V/D矩阵。

## 怎么提速

不同run同时执行；每个run内部仍是Manager完成后Compliance接着处理。旧入口保持串行，使用新执行配置才启用并发。

`config/execution_v1.yaml` 当前设置：

| 配置 | 值 | 含义 |
|---|---:|---|
| max_parallel_runs | 32 | 最多同时执行32个独立run |
| endpoints[*].max_in_flight | 32 | 同一quota_group共用32个HTTP请求名额，两节点共用 |
| endpoints[*].quota_group | shared-account | 两个key暂按共享账号限额处理 |
| http_retries | 2 | 明确HTTP 429/502/503/504时最多补发2次 |
| recovery_successes | 20 | 降速后每20次成功请求，恢复1个并发名额 |

429或503会将该账号额度组的请求并发减半，最低1；等待Retry-After或指数退避。同组所有key一起降速。单次要求等待超过60秒时不再补发该请求。超时、断线可能已经被服务端处理，不自动补发。模型答案、解析失败和validator失败不会因此重跑。

**32是试运行上限，不是查证的Paratera账号额度，也不保证32倍提速或100%完成率。** 当前连接Paratera，不能套用DeepSeek官方服务额度。用旧合同预试检查429、超时、吞吐量和失败类型，再为新批次固定并发。未确认两个账号独立时，共用一个额度组。

## DeepSeek官方文档核对

2026-10-18实际读取[官方限速与隔离文档](https://api-docs.deepseek.com/zh-cn/quick_start/rate_limit)：`deepseek-flash`每账号2500并发，`deepseek-v4-pro`每账号500并发。请求发出至响应完成占用一个名额；与key数量无关，超过额度返回429。同账号不同user_id通常合并计算，不用user_id绕过限流，也不传合同、标签等信息作user_id。

这些规则针对官方服务和对应模型，不证明Paratera的`DeepSeek-V4.1-Flash`具有同样额度。没有切换服务、模型或凭据。

官方非流式请求可能返回空行保活；当前适配器读取完整body后用json.loads解析，兼容前导/尾随空白，新增离线测试覆盖。未启用SSE流式模式，因此不将`: keep-alive`当JSON处理。文档所说10分钟关闭连接，是等待10分钟仍未开始推理的情形，不是全部生成必须10分钟结束，也不是必须把客户端超时改成600。现有urllib超时360是阻塞socket操作的超时，不能表述为整次生成的严格总时限；有保活时可继续等待。模型预算和超时参数本轮保持不变。

## 第二个API填哪里

编辑被git忽略的 `config/api.local.env`，保留现有配置，追加：

```dotenv
PILOT01_BASE_URL_2=https://llmapi.paratera.com/v1
PILOT01_API_KEY_2=填第二个账号的key
```

地址写到 `/v1`，不含 `/chat/completions`。如果第二个服务地址不同，填它自己的兼容地址。向两个服务发送相同冻结模型ID和参数；服务是否提供同一模型、配额是否独立需要核实。不新增第二个模型ID变量，避免悄悄改变实验模型。

然后编辑 `config/execution_v1.yaml`：

```yaml
max_parallel_runs: 32
http_retries: 2
recovery_successes: 20
endpoints:
  - slot: 1
    max_in_flight: 32
    quota_group: shared-account
  - slot: 2
    max_in_flight: 32
    quota_group: shared-account
```

两项32共用一个quota_group，因此合计上限仍为32，不是64。只有确认账号/额度独立后才将quota_group改成不同名称；若届时希望总计64，还需提高max_parallel_runs。按计划顺序和API容量确定每个run的分配，不根据结果分配。同一个run的Manager和Compliance固定使用同一个API；不在失败后切换服务。实际分配留痕。限流器约束本次CLI进程内的请求，其他程序占用同一账号的额度也会影响服务响应。

## 如何启用

先按最终确认矩阵创建新版计划；在已有run命令中追加：

```text
--execution-config config/execution_v1.yaml
```

在同一个PowerShell进程先执行 `. ./scripts/Load-Pilot01ApiEnv.ps1`。读取环境不发请求。`--live` 才开启付费调用，超过6个计划run仍需 `--live-batch`。不带执行配置的旧命令保持串行，也不自动启用第二个API。模型输出32768和每节点16次工具额度不变。

## 失败、审计与恢复

- 各run独立state、模型/工具/validator日志；不覆盖历史结果。
- `schedule_*.jsonl`记录提交顺序、完成通知和UTC时间；模型日志记录实际请求时间。实际开始/完成顺序可以不同于提交顺序，不能声称与串行时序相同。
- `execution_*.json`记录设置、服务域名、端点hash、计划hash、每run槽位，不保存key。
- `http_attempts_*.jsonl`记录全部HTTP尝试状态、重试次数、起止时间和限流后并发数，不保存请求头/正文。失败HTTP尝试的token及费用不可知，最终成功响应usage不等于完整账单。
- `execution_policy.json`固定本批设置、端点、分配。恢复时改变配置会拒绝，需要另建实验ID及输出目录。
- `--resume`跳过已有完成记录、保留并移开中断尝试；不默认重跑失败结果。`--fail-fast`停止提交新run，已提交run允许结束并保留。

新增源码会改变实验profile指纹，原离线计划需重新生成；原始材料和已有实验结果不改写。应报告并发设置和API分配。
