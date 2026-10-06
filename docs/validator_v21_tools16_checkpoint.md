# 16 次工具预算实验：暂停 checkpoint

实验 `validator-v21-tools16-20261006` 按用户请求于北京时间 2026-10-06 23:45 暂停，2026-10-07 核实没有残留实验进程。本文件记录暂停现场；尚未完成整批测试，不是最终报告。等待用户明确继续后再启动模型调用。

冻结运行实现 `4dd80104560a90e48d69ef35e4de8de854475fe9`，远端标签 `codex/validator-v21-tools16-preflight-20261006`。每节点 / invocation 的搜索和打开共用 16 次请求预算；每次模型输出上限 16,384 tokens，其他运行条件保持冻结。离线测试 1016 项通过。

计划 30 个实验格，目前 23 个有最终 `run.json`：20 完成、3 失败。第 24 个中断，另外 6 个未启动。中断不算作模型或 validator 的失败。

| 已记录失败 | 节点 | 该节点实际工具调用 | 原因 |
| --- | --- | --- | --- |
| COC-0263 / E0 / A1V1 | Compliance | 3 次搜索 + 4 次打开 = 7 | 正文引用 p0018，未登记且未自行打开 |
| TFC-0329 / E1 / A1V1 | Manager | 4 次搜索 + 1 次打开 = 5 | 正文引用 p0007，未登记且未自行打开 |
| COC-0053 / E1 / A1V1 | Manager | 10 次搜索 + 6 次打开 = 16 | 请求第 17 次工具操作，无最终候选，预算终止 |

第 24 个为 `validator-v21-tools16-20261006__PILOT-POS-COC-0053__E0__A1V0__r000`。Manager 最终候选已落盘，当前停在 Compliance；已记录 Manager 15 次工具调用、Compliance 6 次。运行器不支持从某个节点内的对话恢复，续跑会将这次中断的日志移到 `.partial`，从此实验格的 Manager 重新执行；已经落盘的 23 个成功或失败观察均跳过，不重新运行。

目前记录 250 次模型调用、205 次工具调用，包含中断尝试的 22 次模型调用和 21 次工具调用。未返回的在途请求可能没有 usage 记录，这些数量只代表已落盘日志。

完整逐运行状态及原始文件 SHA256 在 `outputs/validator_v21_tools16_20261006/checkpoint.json`；冻结计划与 manifest 也在同一输出根目录。原始记录没有改写。

续跑前校验 checkpoint 与 manifest 的 SHA256，加载本地 API 环境，再使用：

```powershell
& .\scripts\Load-Pilot01ApiEnv.ps1
& .\.venv\Scripts\python.exe -m pilot01.experiment run --registry data/pilot01/main_v1/case_registry_v1.json --plan outputs/validator_v21_tools16_20261006/manifests/validator-v21-tools16-20261006/run_plan.json --out outputs/validator_v21_tools16_20261006 --live --live-batch --resume
```

不得加 `--rerun-failed`。保留暂停前 `live_batch.log`，续跑日志另存。续跑会有 7 个需要执行的实验格，其中 1 个是中断重启；暂停与重启必须在最终报告中披露。

整批完成后再运行 `scripts/analyze_validator_preflight.py` 和输出目录内的 `compare_budgets.py`。打包脚本需要区分 150 个最终正式 raw 文件与额外 `.partial` 文件，全部保留；中断尝试的调用 / token 消耗单独报告，不与 30 格最终结果混为一次无中断执行。

当前检索工具对当前合同全部段落做 BM25 词面排序，每次返回前 5 段，摘要约 400 字符；重复查询不会分页。后续若改变检索器、候选 / 正式引用协议、提示词或停止策略，应另建版本，不能在本批中途混改。
