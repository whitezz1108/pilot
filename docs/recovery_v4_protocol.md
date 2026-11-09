# Recovery prompt study, 2026-11-09

This exploratory study tests whether adding an explicit, evidence-conditioned
recovery objective changes correction of upstream omissions. The primary contrast
is V2 minus V1. V0 allows discretionary checking, V1 requires checking, and V2
retains V1 verbatim and adds only the recovery objective. All use A1/D0 and the
same two-role workflow, model, budget, tool implementation and output schema.

The historical checklist V2 draft is superseded for this study. Historical
experiments, prompts and artifacts retain their existing interpretation.

## Frozen inputs and sampling

`scripts/prepare_recovery_materials.py` derives uniformly worded, permuted memos
from the hash-verified clean80 registry. It preserves contract text, policy,
labels and source IDs. This removes positional and prose-style cues, while
also removing descriptive detail from every summary. Results apply to this
derived material version. Reviews remain AI reviews, with no assertion of human
adjudication or infallible CUAD labels.

`scripts/freeze_recovery_study.py` selects cases before any outcome is observed.
The small phase has one case from each positive target stratum and one negative
sentinel, giving 15 full workflow runs. The medium phase selects four other
cases per stratum, with two repetitions, giving 120 runs. Positive cases include
both E0 and E1, negative sentinels include E0. Plans interleave conditions using
seed 20261109. This seed controls scheduling and selection, not model sampling.
Model temperature is 1.0 and no per-request generation seed is supplied.

All small jobs must terminate and at least 80% must deliver a final output before
promotion. Every failure, actual prompt boundary and unexpected tool event is
reviewed. An unresolved harness or leakage defect prevents promotion. Any
intervention-affecting correction requires successor plans and separate results.
Failed runs are retained and count in the planned denominator.

## Observation and analysis

The primary endpoint is E1 final fact recovery and delivery divided by all
planned E1 runs. Also report recovery among completed runs, policy decision
accuracy, E0 retention, absent-category false positives, source-overlap proxies,
role-specific restoration/loss, query changes, tool errors, token usage and
latency. Missing measurements remain explicit. Source overlap does not establish
semantic support. Selected anomalies receive source-text review.

Paired contrasts use the same case, error condition and repeat, with bootstrap
resampling at contract level (4000 draws, seed 1109). These small exploratory
samples can identify candidates for follow-up, not guarantee general effects.
The common validator checklist includes optional V0 checking behaviors, so its
pass rate is checklist satisfaction rather than V0 duty compliance.

## Execution and evidence

Use `config/experiment_recovery_v4.yaml` with the standard CLI. The execution
configuration is `config/execution_recovery_v4.yaml`, with one live request at
a time, no transport retries, streamed reasoning measurements and a local copy
of the server tokenizer. T640 server settings are recorded independently from
client settings. The selected server reports maximum concurrency 4, while this
client limits its own concurrency to 1. Cross-request radix caching is disabled.

The pre-change push of commit `c3bfa28` to the existing branch succeeded before
edits. Evidence lives in ignored `outputs/recovery_v4`, including lineage,
plans, server information, test logs, raw model/tool calls and analyses. Source
and configuration changes are versioned separately from large raw evidence.

Two independent review rounds examine treatment isolation and hidden metadata.
HTTP fixture tests exercise search/open success, duplicate tool errors, context
compaction and final format repair. Hidden gold mutations must leave all wire
payloads unchanged. Full regression verification also checks historical behavior.
These tests bound the tested leakage channels; they do not prove universal
absence of leakage or semantic labeling error.
