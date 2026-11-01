# 原生工具七层配置组合（阶段一）

组合版本：`native-seven-layer-20261101-v2`。七层依次为 experiment、conditions、runtime、models、workflow、policy、execution；各历史组件的内部版本号保留其真实含义，不改写旧实验。配置组合、文件和 SHA256 见 `config/native-seven-layer-manifest.json`，校验命令为 `.venv/bin/python scripts/config_manifest.py --check`。修改组合文件后需重新运行该脚本生成清单，并重新审核差异；清单用于检测变化，不是签名。

三个冻结入口：`config/experiment_native_tools_v1.yaml` 是 `historical-reproduction`（temperature 1.0），`config/experiment_recommended_primary_v1.yaml` 是 `recommended-primary`（temperature 0.6），`config/experiment_native_tools_v2.yaml` 是严格状态机和 `human-lexical-v2` 检索的 `strict-native-v2`。v2 是新的实验协议，不回写或替代 native-v1 历史记录；其采样沿用 BF16+xhigh 的历史异常复测配置。各入口均显式设置 top-p 0.95、top-k 20、min-p 0。历史任务还需使用各自冻结 seed、输入和数据，配置相同不等于结果重现。

## 固定启动

在指定 Linux 服务器的项目目录安装项目依赖后运行：

```sh
bash deploy/native/launch.sh
```

完整 Docker 和 SGLang 参数见启动脚本。镜像固定为本机镜像 ID `sha256:e3a4f06001a29542fc4a8638536d9ffc5eb9c9942b08603c09b19a56eae4a1ae`（历史标签 `lmsysorg/sglang:v0.5.22`）；只读挂载 `/home/evan/ai/models/Qwen3.8-27B`。GPU 2 必须没有计算任务且功率上限已经是 200 W；脚本不会更改共享 GPU 的功率或停止已有服务。2026-11-01 已为三张 GPU 启用 NVIDIA persistence mode，`nvidia-persistenced` 处于 active 状态。主机或驱动重启后仍需复核 GPU 2 的 200 W 功率上限，并确认 persistence mode 和模型服务状态；功率不符时启动脚本会拒绝继续。端口为本机 30002。模型 BF16、NEXTN/MTP steps 3、draft tokens 4、top-k 1、49,152 上下文、并发 1、禁用 radix 前缀缓存，两种 parser 均显式指定。服务 preferred sampling 默认值固定为 `recommended-primary`（temperature 0.6、top-p 0.95、top-k 20、min-p 0、max new tokens 32,768）；两个实验入口仍在每次请求中显式发送各自采样参数。镜像 ID 是 Docker 本地镜像 ID，并非 registry 的可跨主机拉取 digest。

启动后校验服务器返回的模型、配置与 parser、模型列表，两套配置实际经 runner 构造出的请求采样字段，并发送一次有界合成请求检查服务接受推荐采样字段及返回结构化工具调用；发现不匹配即失败。服务器不回显单次请求的实际采样状态，因此后者只能证明客户端构造了正确请求，不能证明推理引擎每一步执行或模型权重内容；正式运行需保留实际请求日志、服务器信息和模型文件身份。验证失败后容器保留以供排查；不应将其视作可用服务。

2026-11-01 已在目标服务器 `evan-PowerEdge-T640` 的空闲 GPU 2 上用上述单条命令启动并完成实机验证。运行中的容器名为 `pilot-native-seven-layer-v1`，仅监听 `127.0.0.1:30002`；未修改 GPU 0、1 的既有服务。SGLang 0.5.22 将命令行的 `NEXTN` 报告为内部 `EAGLE` speculative algorithm，并同时报告 steps 3、draft tokens 4，因此验证器接受这两个等价报告值但仍严格检查 MTP 参数。

实机返回并通过断言的配置包括：BF16、无量化、上下文 49,152、TP=1、显存比例 0.8、并发 1、禁用 radix cache、`qwen3` reasoning parser、`qwen3_coder` tool parser、recommended-primary preferred sampling，以及完整镜像 ID和 GPU 2 的 200 W 功率上限。合成请求得到 `finish_reason=tool_calls`、provider call ID、`probe` 函数和 `{"value": 7}` 参数。完整输出保存在服务器 `/home/evan/ai/benchmarks/native-seven-layer-stage1-20261101/live_verification.log`，本机副本在忽略目录 `outputs/phase1-verification/live-verification-server.log`。

## 本机全量测试数据

隔离检出不携带 CUAD 原始数据、clean80 材料或 Gate 4 历史产物。本次本机验证使用 `C:/Users/44977/Desktop/pilot01/data/raw` 和 `data/pilot01/clean80_v1` 的现有材料，clean80 由 `verify_ready_registry` 校验登记和审核文件哈希；Gate 4 的计划与 case registry 从同一本机项目复制到隔离检出的忽略目录。缺少这些材料时，相关测试不能被解释为产品通过。原始数据和运行产物不应被打包进共享代码归档。

2026-11-01 Windows 本机最终树全量 `python -m pytest --tb=short`：1205 passed，276.09 秒；包括两个历史嵌套重跑检查。完整控制台输出在忽略目录 `outputs/phase1-verification/pytest-final.log`。上述数据目录仅在测试期间通过 junction 引入，测试结束后已移除链接；重复运行需重新提供相同材料。服务器隔离源码另通过原生协议、工具循环、模型客户端和角色路由测试，并完成上述真实服务 smoke test。
