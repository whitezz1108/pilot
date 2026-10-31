# Clean v2：运行与材料协议

本版本已实现独立入口。2026-10-17 当前材料为原40重做＋新40，共80份合同、144份memo的AI审核冻结版，使用独立准备配置；入口为 `data/pilot01/clean80_v1/case_registry_v2.json`。人工复核未完成，正式实验尚未启动。详见 [80份数据集说明](../outputs/clean80_dataset_20261017/80份合同与144份memo_说明.md)。旧 main_v1、clean_v2首版、validator v2.1及tools16数据保持历史版本，不能混作新实验结果。

## 设置

入口为 `config/experiment_v2.yaml`，绑定 D0/D1/D2 配置、runtime_v2、models_v2、旧 workflow_v1 和 policy_v2。Manager → Compliance；最终动作为 Compliance 的输出，无跨节点修改或 validator 反馈重做。

- 两节点均可查原文，均执行同一个 validator v3。2026-10-18 起 verification_mode=audit_only：检查不通过只记录，合法输出继续交接并完成流程；不反馈检查结果。D0/D1/D2 仅改变初始告知。
- 两节点分别有 16 次工具请求额度；搜索、翻页、打开、失败请求共用计数，第 17 次不执行；第 16 次之后仍能交答案。
- 单次模型输出上限 32768 tokens，单次 HTTP 超时 360 秒。实际服务对这些参数的支持尚未做联网预试。
- 相同模型参数适用于两节点和所有 D 条件。上限不是实际费用，也不是累计输出额度。
- E0/E1 在三个 D 条件中配对；负例只有 E0。D0 表示未明确告知检查，不能证明模型心理上完全不知情。

## 从材料准备到运行

当前80份的实际准备流程为 `build_clean80_source.py` → `prepare_clean80_materials.py --live` → `curate_clean80_materials.py` → `build_clean_pilot_cases.py` → `export_clean_materials.py`。旧40与新40均重新生成，统一采用独立准备配置；细节、冻结记录及分组见上方80份数据集说明。下文保留旧40首版的执行过程供追溯，旧冻结目录不能覆盖。

**配置边界修正：** 材料生成与 AI 审核读取独立的 `config/material_preparation_v1.yaml`（可用 `--preparation-config` 指定），不载入实验 profile，也不继承 Manager/Compliance 的 32K 或工具额度。准备配置的 `max_output_tokens: null` 表示请求不传 `max_tokens`，实际仍受服务端默认值和能力限制。实验节点继续使用 models_v2 中的 32768。已冻结的首版材料是在修正前误用 Manager 配置后产生的，历史记录不改写，详见 [修正说明](../outputs/clean_v2_preparation_config_fix_20261017/修正说明.md)。

以下命令在仓库根目录执行，默认均不联网。导出任务不会生成或批准 claim：

```powershell
.\.venv\Scripts\python.exe scripts/prepare_neutral_claims.py --registry data/pilot01/main_v1/case_registry_v1.json --out outputs/clean_v2_material_preparation_20261017
```

输出包括中性编号任务、研究侧 ID 映射和沿用原数据集标注的 label_policy.json 记录。LLM 提供至多八条同合同候选，常规每份合同一次生成。显式调用某一个任务的入口是 `--generate-case CASE-nnnn --live`，会花费模型 token。本轮已执行 40 份；经用户追加授权，两份生成截断的合同各以 `--attempt 2` 补充一次，保留首次记录。第二次使用统一的完整段落筛选规则和 recovery prompt，不是沿原失败输入无限重试。

候选须审核 category、summary、原文摘录及完整引用段落。审核者填 approved、reviewer、review_kind、review_notes，并核对 reviewed_content_sha256 与当前候选一致。修改候选后必须重新审核并重新绑定内容指纹。生成记录必须保留并与候选 hash 一致。

2026-10-17 用户明确授权本版以标明的 AI 审核完成冻结，后续再人工复核。AI 审核通过独立上下文的一轮调用核对候选与完整段落；生成与审核使用同一服务模型，不声称独立模型验证或人工复核。常规一次生成、一次服务模型审核；追加授权仅允许生成失败合同补充一次。本轮有两份服务审核失败，改由 Codex 直接阅读原文并另存 AI 审核记录，没有追加服务审核调用。少数候选的表述、来源编号或背景陈述由 Codex 明确修订；原候选、完整来源、改后内容和理由同时保存。候选绑定 review_record_sha256 与可选 review_record_file；补充生成绑定 generation_record_file。冻结 manifest 明确记录 review_kinds、reviewers 及 human_review_completed。

显式执行单份 AI 审核（会消耗 API token）：

```powershell
.\.venv\Scripts\python.exe scripts/review_neutral_claims.py --registry data/pilot01/main_v1/case_registry_v1.json --out outputs/clean_v2_material_preparation_20261017 --case CASE-0001 --live
```

把同一任务目录内审核过的候选汇总为 `reviewed_candidates.jsonl`后构建：

```powershell
.\.venv\Scripts\python.exe scripts/build_clean_pilot_cases.py --registry data/pilot01/main_v1/case_registry_v1.json --candidates outputs/clean_v2_material_preparation_20261017/reviewed_candidates.jsonl --out data/pilot01/clean_v2
```

生成记录 `generation_CASE-nnnn.jsonl` 应和候选汇总文件位于同一目录。构建时拷贝并校验审核与生成记录；已冻结材料不能覆盖。任一合同候选不足或审核无效，则只写阻塞清单，不生成可运行 registry，也不悄悄删合同。

材料完成后才可计划。CLI 会校验材料 manifest、审核记录 hash、source registry、实际使用材料及执行 profile：

```powershell
.\.venv\Scripts\python.exe -m pilot01.experiment plan --profile config/experiment_v2.yaml --registry data/pilot01/clean_v2/case_registry_v2.json --experiment-id clean-v2-example --repeat 1 --seed 20261017 --out outputs/clean_v2
```

`run` 同样传 `--profile`，并明确选择 `--responses`（脚本离线）或 `--live`（真实 API）。没有自动联网模式。score/summary 也要传 profile，生成 `run_scores_v2.jsonl` 和 `experiment_summary_v2.json`；不能用旧 error_survival 口径评分新版。

## 新旧版本

V2 计划冻结 runtime、披露映射、材料 hash、模型参数、配置/prompt/修复提示与代码指纹；恢复执行时参数变化会拒绝。旧入口不传 profile，保留旧行为。旧配置中的 A1V0 “unused” 描述是历史文案，实际允许自主核验；不得把它解释为禁止或未发生工具调用。

现有 registry 文件结构保持可读兼容；新材料明确标记 material_version=2。中性 claim ID 表示摘要槽位，同一槽位可在 E0/E1 承载不同陈述，原操纵信息只留研究侧。条数相同仍不等于遗漏不可察觉。

## 验证边界

离线测试只验证执行与记录。本版中性材料经授权采用明确标记的 AI 审核，人工复核尚未完成；目标定义与 gold 沿用原始 CUAD，无额外 sentinel 裁定。材料生成成功不等于 Manager/Compliance 正式工作流已完成联网预试。程序通过、打开过标注段和语义正确是不同概念。本版不新增实验运行中的语义裁判、单节点对照、代表性抽样或 tools16 续跑。
