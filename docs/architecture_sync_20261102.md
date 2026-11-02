# Current experiment architecture — 2026-11-02

This checkpoint captures the current application, experiment profiles, prompt templates, retrieval rules, console, deployment scripts and serving patch. It supersedes the statement that closed-v4 is still uncommitted in `work_summary_20261030_20261102.md`; that statement describes the workspace before this checkpoint.

## Application and experiment configuration

- Repository: `whitezz1108/pilot`, branch `codex/typed-tool-protocol`.
- Server application root: `/home/evan/ai/staging/protocol-refactor-native-v2-20261101`.
- The local-GPU console selects `config/experiment_native_tools_closed_v4.yaml`. API execution retains its separately configured profile.
- That profile resolves conditions v3, workflow v1, policy v2, the context-safe BF16+MTP xhigh model profile, and `runtime_native_tools_closed_v4.yaml`.
- closed-v4 uses native-v2, closed-final-or-tool-v1, Manager/Compliance v6, at most 16 tool requests per node, two pages per query, duplicate-search/open rejection, a search stop after three no-new-result requests, and retrieval-ledger context compaction. Verification remains audit-only, with no automatic revision rounds.
- The Manager and Compliance factories still load `repair_v3.md` through the default repair loader. Loading a repair template does not establish that a particular run invoked repair.
- Core files under `src/`, all prompt templates and server-owned configuration files were compared against the server by SHA256 after normalizing CRLF to LF. Application source, prompts and configuration matched. Local console tests, documentation and launcher adaptations are retained alongside them.

## Serving boundary patch

At synchronization, GPU0/1 run image `sha256:b1e0264ebb66e7a192b613875f286c4757f79ace76d47d030789a3327f7d0dd0`; GPU2 retains base image `sha256:e3a4f06001a29542fc4a8638536d9ffc5eb9c9942b08603c09b19a56eae4a1ae`.

`deploy/boundary_v1/` includes the exact before/after sources, unified patches, SHA256 manifest, Dockerfile and associated operation/validation scripts for `explicit-think-end-v1`. The downloaded source bytes match the server's patch manifest. Only explicit `</think>` ends Qwen3 reasoning; the parser and grammar backend share that boundary. This is a separate serving-layer change, not a prompt rewrite.

The owning server evidence directory is `/home/evan/ai/repairs/qwen-boundary-v1-20261102`. It records one collector failure followed by one completed targeted CASE-0067 workflow, with 27 model calls and 25 tool-log rows, including one duplicate-open refusal. This is neither a four-workflow concurrency test nor an estimate of reliability or judgment accuracy. Exact historical token-path attribution remains limited by missing historical raw tokens.

Deployment and replay scripts require the archived engine environment, tokenizer/model mount and evidence files referenced in their source. Generated evidence, container inspection dumps and raw runs are not part of this source checkpoint. The scripts must not be treated as instructions to rerun archived experiments automatically.

## Prompt inventory and evidence boundaries

`prompt_inventory_20261102.json` lists all 18 Markdown prompt templates, their hashes and usage, plus code/configuration that constructs the final model-visible messages and schemas. D disclosure and V verification are recorded separately. The exact request for an existing run is preserved in its `model_calls.jsonl`, including dynamically rendered materials and history.

This Git synchronization runs offline checks only. It does not launch a model request, restart a service, resume an experiment or replace historical failure records. Raw CUAD source, generated runs, credentials, virtual environments and temporary builds are excluded from the commit.

## Offline validation for this checkpoint

The focused protocol/retrieval/model/prompt/monitor checks passed 241 tests; console checks passed another 22. The full suite was run and was not green: 1,122 passed, 86 failed, 62 errors, six skipped. Observed blockers include missing local canonical CUAD and clean80 material files, dependent historical-suite subprocesses, and a raw-text network audit that flags mocked `urlopen` targets. See `architecture_sync_validation_20261102.json`. These outcomes are recorded without importing private/raw experiment data or rewriting the old assertions during a synchronization task.
