# 双卡路由与工程测试预算核查

核查日期：2026-10-22。以下区分源码行为、离线验证和仍需服务器实测的部分。

## 当前实验入口的实际行为

- `config/models_v2.yaml`：Manager 和 Compliance 均为 `max_output_tokens: 32768`、`timeout_seconds: 360.0`。`experiment_v2.yaml` 和 `experiment_v3.yaml` 都引用它。
- `src/pilot01/model/client.py:ModelParams` 不包含 `base_url`，且禁止额外字段。不能把两个角色的 URL 直接写入 models YAML。
- `src/pilot01/experiment/cli.py:LiveClientFactory` 的普通 `--live` 入口为两个角色构造读取同一组默认环境变量的适配器。
- `src/pilot01/model/execution.py:PooledLiveFactory` 的原有行为按 run 分配槽位：同一 run 的两个角色固定在同一槽位。第二个 URL 可以把不同 run 分给不同服务，但不代表 Manager/Compliance 分卡。
- `src/pilot01/runtime.py:ExperimentProfile` 要求两个角色的完整模型参数相同；当前 v2/v3 profile 也不能简单把两个 `model_id` 改成不同模型。

结论：原有配置不支持所需的按角色分端点。单改 `model_id` 无法改变请求地址。

## 本次补充的可执行配置

在执行设置新增可选 `role_slots`。不提供该项时保持原有按 run 分配；提供时必须完整声明两个角色，且槽位必须存在。没有失败后跨端点回退。

| 工程文件 | 作用 |
|---|---|
| `config/execution_engineering_dual_gpu_v1.yaml` | Manager → slot 1；Compliance → slot 2；同时最多 1 个 run；每端点最多 1 个请求；HTTP retry = 0 |
| `config/models_engineering_dual_gpu_v1.yaml` | 两角色每次调用均为 16,384 tokens、360 秒；温度 0 |
| `config/experiment_engineering_dual_gpu_v1.yaml` | 使用上述独立模型预算，复用现有 v3 条件和 prompt |

端点从进程环境读取，不写入模型参数或提示词：

| 角色 | URL 变量 | Key 变量 |
|---|---|---|
| Manager | `PILOT01_BASE_URL` | `PILOT01_API_KEY` |
| Compliance | `PILOT01_BASE_URL_2` | `PILOT01_API_KEY_2` |

URL 写到 `/v1`，适配器追加 `/chat/completions`。例如同一服务器的两个独立端口可以分别服务两张 GPU；端口号不同本身不证明 GPU 不同。服务端还需记录两个进程各自绑定的 GPU、模型权重/版本及启动参数。

工程模型文件暂沿用现有模型 ID；运行前将两个 `model_id` 同时设置为本地服务实际提供的名称。相同名称也不证明两端点加载了相同权重，需核对服务端记录。

该配置通过 CLI 的 `--execution-config` 显式启用；当前网页操作台仍自行创建原有按 run 分配的设置，选择两个 API 不会启用角色分卡。普通 `--live` 也不会自动读取工程配置。

创建计划和执行时均指定 `--profile config/experiment_engineering_dual_gpu_v1.yaml`；执行时另加 `--execution-config config/execution_engineering_dual_gpu_v1.yaml`。必须使用新实验 ID 和输出目录。修改源码会改变 profile 指纹，旧计划需重新生成，不能用新配置续跑旧批次。

例如在仓库根目录的 PowerShell 中，先设定一个已冻结 registry 路径与其中一个 case ID，再生成不超过六个 run 的工程计划：

```powershell
$engineeringRegistry = '填入已冻结registry的实际路径'
$engineeringCase = '填入该registry中的一个CASE编号'
$engineeringId = 'engineering-dual-gpu-20261022-01'
$engineeringProfile = 'config/experiment_engineering_dual_gpu_v1.yaml'
$engineeringOut = 'outputs/engineering_dual_gpu'
.\.venv\Scripts\python.exe -m pilot01.experiment plan --registry $engineeringRegistry --profile $engineeringProfile --out $engineeringOut --experiment-id $engineeringId --cases $engineeringCase --conditions A1V1D0 --repeat 1 --seed 22
```

确认两个 URL/Key 已通过现有环境加载器载入当前进程后，以下命令才会调用模型：

```powershell
$engineeringPlan = Join-Path $engineeringOut "manifests/$engineeringId/run_plan.json"
.\.venv\Scripts\python.exe -m pilot01.experiment run --registry $engineeringRegistry --profile $engineeringProfile --out $engineeringOut --plan $engineeringPlan --execution-config config/execution_engineering_dual_gpu_v1.yaml --live --fail-fast
```

## 预算含义与验收

16,384 是工程候选上限，尚未在所述双卡模型上标定，也不是正式实验预算。降低总上限不能保证模型停止 Thinking 或给最终正文；按用户报告的 32K 压测现象，仍可能更早耗尽预算。当前请求仅发送 `max_tokens`，不发送独立的 Thinking 上限或为正文预留 token 的参数。若需要控制 Thinking，须先核对所用服务实际支持的字段再单独实现、验证。

预算按每次模型调用计算，工具后续调用、格式修复各自使用同一上限，并非每个角色整个 run 合计 16,384。360 秒传给 `urllib.request.urlopen`，是阻塞网络操作的超时设置，不是整个角色或整次 run 的严格墙钟截止时间；本次未增加全流程硬截止。

真实工程验收应至少覆盖 E0/E1、需要工具查证的实际任务，并分别记录：

1. 客户端 HTTP 日志的 `role`、`slot`、`model_call_id`，以及服务端接收记录和 GPU 进程绑定；两者共同证明请求到达预期 GPU。
2. `model_calls.jsonl` 的实际参数、`finish_reason`、usage、非空正文和结构解析结果。`length`、空正文、超时及解析失败均保留为失败，不能当有效答案。Thinking 细分 usage 缺失时记为未知。
3. 两个角色均完成实际输出后，再根据截断率、耗时及内容长度决定正式预算；变更预算用新批次，不覆盖旧结果。

执行清单新增 `run_role_slots`，HTTP 审计逐条保存角色与槽位。更改角色分配或端点后，原批次执行策略校验会拒绝恢复。

## 已验证与未验证

`tests/test_role_endpoint_routing.py` 使用真实适配器和注入的离线 transport，检查多个 run、多次调用的完整 URL、Key 选择、请求 `max_tokens=16384` 和传输超时 `360.0`；同时覆盖映射校验、审计、冻结策略拒绝变更、超时不切换端点。原有第二槽位测试继续检查默认的同 run 同端点行为。

本次相关回归结果：**144 passed**。范围为角色路由、完整 Manager → Compliance 工作流、并发执行、模型适配器、CLI、操作台及 API 配置。全部使用离线 transport 或脚本响应；没有向真实模型发送请求。

这些测试证明客户端构造和路由；本次没有连接 GPU 服务、运行 32K 压测或启动真实 Pilot，不能据此声称物理双卡路由和正式预算已经实测通过。原 `models_v2.yaml` 保留其 32,768 / 360 秒参数，默认实验入口仍使用它；只有显式指定工程 profile 才使用 16,384。
