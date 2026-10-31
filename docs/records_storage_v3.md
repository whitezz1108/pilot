# 实验记录分类存储与查询 v3

2026-10-19。v2 只解决了“整批大 JSON”，仍把每轮所有数据装进 record.json。本版恢复研究需要的“类型 → 批次 → 轮次”，网页只按当前查看内容读取。保留历史原始日志和旧包，不改实验材料、模型可见 ID、提示词或研究分母。

## 分类边界

默认导出格式 `pilot01-research-records-v3`，根 manifest.json 保存批次身份、轮次顺序及各类文件映射。文件名含序号、case、E、条件与重复次数；研究侧条件不进入 agent 输入。

| 目录 | 每轮内容 | 拆分理由 |
| --- | --- | --- |
| overview/批次/轮次.json | 身份、条件、状态、正式输出、失败、配置、时间与费用摘要、研究评分 | 快速比较结果，不含完整请求、响应或流程事件 |
| materials/批次/轮次.json | 实际加载的 memo、memo 版本 ID、原文版本、段落位置、引用关系 | 核对处理材料与证据链；不复制合同正文 |
| model_calls/批次/轮次.jsonl | 调用 ID、角色、执行次序、时间、token、修复标志、价格与估算 | 一行一调用，方便统计，不含完整输入/响应 |
| model_inputs/批次/轮次.jsonl | 调用 ID 与完整实际模型输入 | 独立查验模型看到了什么 |
| model_responses/批次/轮次.jsonl | 调用 ID、可见原始响应、解析后的候选输出 | 查验模型说了什么；与正式交付区分 |
| tools/批次/轮次.jsonl | 工具 ID、父模型调用 ID、参数、状态、开始/结束/耗时、返回段落定位 | 区分搜索和打开；不再复制原文 |
| verification/批次/轮次.jsonl | 各节点实际校验记录 | 程序合规与答案正确性分开分析 |
| workflow/批次/轮次.jsonl | 原始事件、节点实际输入与交接 | 保留执行证据，不挤入概览 |
| http/批次/轮次.jsonl | 每次网络尝试、限流、重试与共同调用 ID | 一个模型调用可能对应多次 HTTP 请求 |

不存在某类数据时，manifest 对应项为 null，不创建空文件。CSV 只有概览，不凭空生成调用、材料或时间线。完整模型输入是既有审计证据，可能含当时工具返回的原文；它保留实际请求，materials 和 tools 只新增定位信息。

## ID 与数据库

运行时每次 ModelRequest 生成独立 model_call_id，格式修复使用新 ID。该 ID 通过 urllib Request 的本地属性传给 HTTP 审计，不作为供应商参数或 HTTP 头发送。工具日志带 parent_model_call_id 和独立 tool_call_id。call_index 继续在角色内部编号，invocation 继续区分同一节点的不同执行。

历史模型调用缺少 ID 时，根据 run、角色、invocation、call_index 生成确定性 `legacy-` ID，并标注 derived_composite。历史 HTTP operation 不猜测成共同调用 ID。历史缺失工具时间保持未知。

memo_version_id 是实际事件里 memo 的规范 JSON 内容指纹：UTF-8、ensure_ascii=False、sort_keys=True、separators=(",", ":")、SHA256。E0/E1 仍共用模型可见 memo_id，研究侧通过版本 ID 直接关联实际内容。

SQLite schema user_version=2，新增 memo_versions、research_runs、model_call_refs、tool_call_refs、source_relations。memo 版本唯一，工具父调用带外键，按角色/执行次数/序号和来源关系建索引。数据库只保存研究关联和轻量元数据，不保存完整模型正文。它仍是可重建派生数据，执行引擎不读取它。

## 按需读取与迁移

`GET /api/records/run?id=...&run_id=...` 默认返回 overview。

使用 `section=materials|model_calls|model_inputs|model_responses|tools|verification|workflow|http` 查询对应类型，JSONL 默认 100 条、最大 200 条，支持 offset。模型输入/响应支持 call_id；通过记录的字节偏移直接读取该条，避免扫描本轮所有长文本。网页默认本轮概览，完整输入、校验与流程事件在展开时读取。

实验台的 `/api/detail` 也默认只返回概览，详情弹窗提供该轮分类记录的直接链接，避免另一个入口仍然加载大 JSON。原始候选输出不会冒充正式节点交付。index/ 与 research/ 均为可重建数据，备份保留原始日志及分类导入原件，不重复备份这两处派生内容。

派生文件位于 outputs/experiment_console/research/类型/短批次键/短轮次键，短键兼容 Windows 路径限制；manifest 保存映射。原始 raw、冻结 manifests、processed 和上传原件保留。旧 JSON、v2 ZIP 和原始日志 ZIP 继续可导入；新导入分类保存，旧大包首次访问迁移，原件保留。仅显式 format=json 的旧接口仍能生成兼容大包，网页不使用它。

分类包可以精确还原原始记录的字段和值，包括缺字段和 null 的区别。导入先全部验证，成功后发布；路径越界、重复路径、run_id 不一致、缺失数据类型和超限包拒绝。

离线回填，不调用模型：

```powershell
.venv/Scripts/python.exe scripts/rebuild_records_index.py --sections
```

服务器：

```bash
cd ~/pilot01
PYTHONPATH=src .venv/bin/python scripts/rebuild_records_index.py --sections
```

`--batch local:批次ID` 限定批次。`--rebuild` 清除派生数据库索引再重建，不删除原始数据。

## 工具计时与费用

工具执行前记录 UTC 开始时间，执行结束记录 UTC 结束时间，perf_counter 单调时钟记录耗时。包括普通搜索、分页搜索、打开、拒绝和异常；异常仍按原行为传播。耗时单位秒，不记录首 token 或流式响应指标。

费用采用版本 `deepseek-official-usd-2026-10-19`，来源为 [DeepSeek 官方价目表](https://api-docs.deepseek.com/quick_start/pricing/)。USD/百万 token 的峰值价格：Flash 缓存输入 0.006、非缓存输入 0.30、输出 1.20；Pro 对应 0.044、1.32、3.96。低谷价格减半。UTC 周一至周五 01:00–04:00、06:00–10:00 为峰段，中国节假日除外，周末始终按低谷；2026 假期按[国务院通知](https://www.beijing.gov.cn/zhengce/zhengcefagui/202511/t20251104_4258873.html)。其他年份峰段缺少日历时返回候选金额和缺失原因，不伪造完整金额。

公式为 `(prompt_tokens-cached_tokens)*非缓存价 + cached_tokens*缓存价 + completion_tokens*输出价`，再除以一百万。reasoning_tokens 不重复相加。未知模型、缺失输入/缓存/输出 token 或缺失时间，不按零计费。展示已估算调用数、完整性、已知部分金额及价格版本。此为按固定价目快照统一计算的官方参考成本，第三方账单可能不同，不宣称恢复了历史真实账单。

原始评分文件保持不变；新生成评分增加 official_reference_cost。历史浏览和导出的 overview 另有当前固定价目快照的估算。
