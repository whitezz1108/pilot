# GitHub 基线整理说明（2026-11-01）

本次整理的目标是形成可上传、可复现的第一阶段基线，不重写研究历史。

## 当前入口

- 实验代码与配置：仓库根目录、`src/`、`config/`、`prompts/`。
- 原生工具部署：`deploy/native/`。
- 七层配置与 SHA256：`config/native-seven-layer-manifest.json`。
- 只读模型监控：根目录 `启动模型监控面板.cmd`，实现位于 `tools/model-ops-dashboard/`。
- 当前工程实验计划：`docs/engineering_benchmark_plan_20261101.md`。

## 整理决定

- 移除三份已被 `-v2` 版本明确取代、且未被冻结清单引用的无版本号 Phase Prompt；历史内容仍保留在 Git 历史。
- 保留版本化 prompt、旧协议报告、冻结 manifest 和历史运行说明，因为它们是研究可追溯性证据，不是可随意删除的缓存。
- `.venv`、`__pycache__`、`.pytest_cache`、egg-info、`outputs/`、快捷方式和本机运行日志均由 `.gitignore` 排除，不进入 GitHub 基线。
- `outputs/` 中的实机日志继续作为本机证据保存；GitHub 文档只记录其路径、边界和可公开配置，不上传潜在大体积或含运行环境细节的原始输出。

## 上传边界

本基线不包含 SSH 私钥、API key、sudo 密码、本机凭据或未审查的模型输出。推送前必须重新运行全量测试、配置 SHA256 检查和监控面板健康检查。
