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

Before any medium inference, a source-only semantic review identified a
registration-withdrawal annotation whose scope differs from contract termination,
incomplete agreement sources, and unresolved change-of-control boundaries.
`scripts/amend_recovery_medium.py` records the exclusions and refills the same
strata using the original hash order. The original `r4m-1109` plan is preserved
without execution. Its successor is `r4m2-1109`, still 12 cases and 120 runs.
The amendment requires review hashes and refuses to run after either medium
raw directory contains an attempted run. Full reasons and source offsets live
in the preflight semantic reviews and `study_design_amendment.json`.

CASE-0016 in the small pilot also has a disputed negative category. Small
results serve the workflow gate and remain separate from primary effect
estimates. The medium negative controls CASE-0044 and CASE-0052 retain disclosed
generic assignment/successor interpretation boundaries; report sensitivity
excluding those controls alongside their original-label results. The primary
E1 recovery comparison uses eight positive contracts and is unaffected by
these two negative-control interpretations.

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

Before medium inference, the additional semantic-review coverage is fixed as:
every E1 recovery in either accepted role output, both members of every V2/V1
pair differing in terminal delivery status, final clause statuses or decision,
every negative-control false positive in either accepted role output, and every
delivery failure. The original endpoint is not changed by this expanded review.
Review copies hide the V/E arm and run identifier where possible, with a separate
mapping retained for audit and opaque contract keys preserving source grouping.
Reviewers assess the actual conclusion, cited source text and text available to
the role. The original-label endpoint remains unchanged. AI-reviewed support,
unsupported conclusions, unresolved boundaries and unreviewed records are
reported separately; AI review is not human adjudication.
The review exporter requires all planned runs to be terminal and validates the
plan/material/runtime identity. Three contract-grouped review shards divide the
work; they do not constitute three independent ratings of each record. Behavior
described in an output can still let a reviewer infer the condition, and cited
excerpts alone cannot establish absence throughout a contract.

Process summaries separate E1, E0 positive cases and negative controls, and
separate manager and compliance. Numerical paired differences use actual
counts or measured values, not the boolean recovery estimator. Resource tables
distinguish cumulative request tokens, model-request round-trip latency and
workflow wall time. Missing logs and missing usage are reported, not replaced
with zero. A zero gold-paragraph overlap can still accompany valid evidence in
another paragraph, and source IDs visible in prose can be absent from the
structured observation ledger.

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
Both phases execute on the same local Python environment. Medium uses lossless
SSH transport compression to the same server endpoint; this preserves model
request content. Phase latency is reported separately. An optional server-local
runner transfer was rejected by automatic approval review and abandoned. Its
unused isolated environment was removed after path and content verification.

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
