# 在 t640 上用 DeepSeek 驱动开发 agent：可行性与部署步骤

核对日期：2026-10-27（Asia/Shanghai）。本轮只核对环境并编写文档，尚未安装或启动 Claude Code/Pi，也未测试它们访问 DeepSeek 的真实会话。

## 1. 建议

可行。这里的“Claude harness”按 Claude Code 终端开发 agent 理解；不需要先自行开发 Claude Agent SDK 服务。推荐先安装 **Claude Code + DeepSeek API**：已经有 DeepSeek 官方接入说明，完成读仓库、改代码、运行测试和交接即可使用。需要自定义工具、会话事件或将开发 agent 集成进控制台时，再评估 Pi 的扩展/RPC/SDK。

这两者都是开发工具。DeepSeek 承担开发 agent 的推理；模型服务仍由现有 SGLang 提供，实验仍经 pilot01 的冻结 plan、受限视图、工具与评分器运行。开发 agent 不替代 Manager/Compliance，不将其个人会话当作实验数据。

```text
你通过 SSH 提需求
        ↓
Claude Code 或 Pi ── API ── DeepSeek（开发推理）
        ↓ 读代码、补丁、pytest、受控实验命令
pilot01 plan → run → score → summary
        ↓
Manager GPU0 → Compliance GPU1（或原实验 API 配置）
        ↓
独立原始证据与派生报告
```

API 驱动的开发 agent 本身不需要加载 GPU 权重；主要消耗普通主机内存、网络与 API tokens。它主动运行本地实验时，实验进程才使用 GPU。它可以协助定位瓶颈、实现优化、做配对测试，不能保证自动找出最优配置或自动形成有效研究结论。

## 2. 已核对的服务器条件

通过现有 SSH 别名 `t640`，2026-10-27 实时只读核对：

| 项目 | 当前结果 |
|---|---|
| 系统 | Ubuntu 24.04.5 LTS，x86_64 |
| 项目 | `/home/evan/ai/experiments/pilot01` |
| Python | 项目 `.venv/bin/python` 可用 |
| 内存 | 当时 MemAvailable 约 229 GiB，仅为瞬时快照 |
| CLI | 当前非交互 SSH PATH 中未找到 claude、pi、node、npm、tmux；不能据此断言磁盘上绝无其他安装 |
| 工作区 | `git status --porcelain` 有 181 条记录，包含既有工作 |
| 模型配置 | 远端两角色均为 Qwen3.8-27B、16,384 输出上限、360 秒网络操作超时 |

三张 A800 80GB PCIe 的硬件信息来自 2026-10-26 留档；当前工程只把 GPU0/GPU1 分给两个角色。启动新会话时仍需核对进程与占用。

## 3. 先区分 API 来源

历史实验的 `DeepSeek-V4.1-Flash` 是当时端点使用的模型名，不能直接当作官方 DeepSeek 端点接受的 ID。

- 官方 DeepSeek：下文默认走它，Claude Code 使用 Anthropic 兼容接口。
- Paratera 等第三方：必须使用其提供的 endpoint、API 协议和 model ID；不能把第三方 key 发给官方域名，也不能默认其支持完整 Anthropic 协议。
- 开发 agent 凭据与实验的 `PILOT01_*` 凭据分开配置。不要为了装 harness 覆盖 `config/api.local.env`。

用户此前在另一台 Windows 环境使用过 Paratera 配置；这只是历史背景，未证明服务器已经有同一账号或可用密钥。本次没有读取或复制这些密钥。

## 4. 路线 A：Claude Code + 官方 DeepSeek（推荐）

以下命令均在 **SSH 登录后的 Ubuntu Bash** 执行，不在 Windows PowerShell 中直接执行。只选一条路线先完成验收。

### 4.1 安装与版本记录

Claude Code 官方推荐原生安装；这条路线不要求先安装 Node/npm。可以先下载检查安装脚本，再执行：

```bash
harnessInstaller="$(mktemp)"
curl -fsSL https://claude.ai/install.sh -o "$harnessInstaller"
less "$harnessInstaller"
bash "$harnessInstaller"
export PATH="$HOME/.local/bin:$PATH"
claude --version
```

原生安装器的版本与更新策略应按实际安装结果记录。若下载不可达，核对官方安装文档中的替代渠道；不要从不明镜像安装同名包。这里尚未执行这些命令。[Claude Code 官方安装说明](https://code.claude.com/docs/en/quickstart)

### 4.2 仅在当前开发会话设置凭据

```bash
read -rsp 'DeepSeek developer API key: ' HARNESS_DEEPSEEK_KEY
printf '\n'
export ANTHROPIC_BASE_URL='https://api.deepseek.com/anthropic'
export ANTHROPIC_AUTH_TOKEN="$HARNESS_DEEPSEEK_KEY"
unset HARNESS_DEEPSEEK_KEY
export ANTHROPIC_MODEL='deepseek-flash'
export ANTHROPIC_DEFAULT_OPUS_MODEL='deepseek-flash'
export ANTHROPIC_DEFAULT_SONNET_MODEL='deepseek-flash'
export ANTHROPIC_DEFAULT_HAIKU_MODEL='deepseek-flash'
export CLAUDE_CODE_SUBAGENT_MODEL='deepseek-flash'
export CLAUDE_CODE_EFFORT_LEVEL='high'
cd /home/evan/ai/experiments/pilot01
claude
```

`deepseek-flash` 是本次读取官方示例所用名称；安装时再核对账号实际可用模型。主会话先采用 high；这不是实验模型的温度、思考开关或输出预算。暂时用单个开发会话；确需子 agent 时优先低 effort，并检查该 harness/端点版本是否真的能单独控制思考。[DeepSeek 官方 Claude Code 接入说明](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/)

这个配置请求的是 DeepSeek 模型；界面叫 Claude Code 不代表调用了 Claude 模型。兼容接口也不代表所有专属工具和扩展行为都与 Anthropic 完全相同，必须做下一节的工具链验收。[DeepSeek Anthropic 兼容接口说明](https://api-docs.deepseek.com/guides/anthropic_api/)

## 5. 路线 B：Pi + DeepSeek

目前上游 `badlogic/pi-mono` 已跳转为 `earendil-works/pi`；当前 npm 包名为 `@earendil-works/pi-coding-agent`。官方文档要求 Node.js **22.19+**，安装器可以协助安装运行时；不要照旧教程假设 Node 18 足够。

```bash
harnessInstaller="$(mktemp)"
curl -fsSL https://pi.dev/install.sh -o "$harnessInstaller"
less "$harnessInstaller"
sh "$harnessInstaller"
```

按安装器提示更新 PATH 或重新登录，然后执行 `pi --version`。若已自行安装合适 Node，可改用 `npm install -g --ignore-scripts @earendil-works/pi-coding-agent`；不要两个渠道重复安装。[Pi 上游安装说明](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md)

优先使用内置 DeepSeek provider：

```bash
read -rsp 'DeepSeek developer API key: ' DEEPSEEK_API_KEY
printf '\n'
export DEEPSEEK_API_KEY
pi --list-models deepseek
read -rp 'Exact DeepSeek model ID from the list: ' HARNESS_MODEL_ID
cd /home/evan/ai/experiments/pilot01
pi --provider deepseek --model "$HARNESS_MODEL_ID" --thinking high
```

Pi 官方列出了 `DEEPSEEK_API_KEY`；模型名以安装版本的实际目录为准。若 high 不受该模型支持，检查它的实际选择，不要只看启动参数推断思考已开启。[Pi provider 说明](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/providers.md)、[Pi 模型选择](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md)

若连接第三方 OpenAI 兼容网关，再使用 `~/.pi/agent/models.json` 自定义 provider。模板的 host/model 均是待填写值，不能原样运行：

```json
{
  "providers": {
    "pilot-dev-gateway": {
      "baseUrl": "https://YOUR-PROVIDER/v1",
      "api": "openai-completions",
      "apiKey": "$HARNESS_GATEWAY_API_KEY",
      "authHeader": true,
      "models": [
        { "id": "YOUR-EXACT-MODEL-ID", "reasoning": true }
      ]
    }
  }
}
```

模板只解决 provider 描述，不证明多轮工具调用、思考回传或模型上下文上限正确。认证、思考参数及兼容标志要根据真实响应验证。[Pi 模型配置字段](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models-configuration.md)

Pi 默认具有执行工具的能力；工作目录不是权限隔离边界。初次了解仓库可按已安装版本使用 `--tools read,grep,find,ls` 限定工具，确定修改范围后再开放 edit/write/bash。若希望长期无人值守，再考虑独立用户或容器及资源限制，而不是仅靠一段提示词。[Pi CLI](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/cli.md)、[Pi 权限说明](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md)

## 6. 首次验收与交接

先打开 [实验项目 handoff](HANDOFF_server_experiment_agent_20261027.md)，把文末“第一条消息”交给 agent。首次验收依次检查：

1. 能返回一条回答，记录真实 harness 版本、provider、model ID；成功打印版本号不算 API 连通。
2. 能读取指定源码并给出函数和行号，确认文件内容确实进入上下文。
3. 能执行一个离线测试并解释真实退出码，不能只复述命令。
4. 后续明确授权一个小改动后，能展示 diff、运行对应测试、保存交接记录。
5. 最后才授权明确数量的本地模型工程 run；先使用新 ID、受控预算，完整保留失败及原始证据。

当前只需要单个交互会话。SSH 中断后，可按安装版本使用 Claude Code `claude -c` 或 Pi `pi --continue` 恢复保存的会话；这不等于断线时进程仍在运行。tmux 是可选的进程保持工具，当前 PATH 中尚未发现它；需要长任务时另行安装或使用已有服务管理方式。[Claude Code CLI](https://code.claude.com/docs/en/cli-reference)、[Pi CLI 会话选项](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/cli.md#sessions)

## 7. 本轮已完成与待执行

已完成：官方文档核对、服务器只读检查、可直接交接的项目文档。尚未执行：harness 安装、凭据设置、DeepSeek 开发会话、开发 agent 工具链验收。此次服务器模型配置和实验源码没有因本交接任务发生变更。
