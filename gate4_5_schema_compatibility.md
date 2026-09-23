# Gate 4.5 — schema compatibility with the previous outputs

Stage 10, item 3 of the refinement spec: *"Report any schema incompatibilities
with previous outputs."*

This is a report, not a change. Nothing here was worked around, and no old
artifact was rewritten to make the current code accept it.

---

## 1. Summary

The refinement replaced four field names, added two nested blocks and moved one
piece of gold into a per-category mapping. Every artifact that records a
**model's structured output** or a **score row** carries the old spellings, and
the current pydantic models refuse them. Artifacts that record only *plumbing* —
run plans, aggregate summaries — are unaffected, because none of them holds an
`AgentOutput` or a `RunScore`.

| Artifact | Count | Current code | Failure |
| --- | --- | --- | --- |
| `outputs/raw/gate4-smoke/*/run.json` | 6 | **0 / 6 readable** | 12 validation errors |
| `outputs/raw/gate4-development/*/run.json` | 60 | **3 / 60 readable** | 12 validation errors |
| `outputs/processed/gate4-smoke/run_scores.jsonl` | 6 | **0 / 6 readable** | 5 validation errors |
| `outputs/processed/gate4-development/run_scores.jsonl` | 60 | **0 / 60 readable** | 5 validation errors |
| `outputs/development/cases_v1.json` | 1 | **refused** | 3 validation errors |
| `outputs/manifests/*/run_plan.json` | 4 | all readable | — |
| `outputs/processed/gate4-*/experiment_summary.json` | 2 | both readable | — |
| `outputs/development/cases_v1_v2.json` | 1 | readable (12 cases) | — |
| `outputs/raw/gate5-regression/*/run.json` | 9 | all readable | — |

The three readable Gate-4 development runs are the three that **failed at the
Manager stage**. They carry no `manager_output` at all, so the schema never gets
as far as refusing anything. In other words: the entire *successful* record of
the Gate-4 development batch is now unreadable, and only its protocol failures
survive.

This is the approved design — "destructive replacement, old outputs marked
incompatible" — and `outputs_snapshot_gate4.5/` holds the pre-refactor copy.
It is recorded here so the incompatibility is a documented fact rather than
something discovered later by a script that quietly returns nothing.

---

## 2. Run artifacts — `RunArtifact` (12 errors per run)

Every field of `AgentOutput` that the refinement touched appears twice in each
artifact, once for the Manager and once for the Compliance node:

```
missing            execution.manager_output.target_clause_status
missing            execution.manager_output.evidence_provenance
extra_forbidden    execution.manager_output.clause_status
extra_forbidden    execution.manager_output.verification_status
extra_forbidden    execution.manager_output.evidence_ids
extra_forbidden    execution.manager_output.adopted_upstream_claim_ids
missing            execution.compliance_output.target_clause_status
missing            execution.compliance_output.evidence_provenance
extra_forbidden    execution.compliance_output.clause_status
extra_forbidden    execution.compliance_output.verification_status
extra_forbidden    execution.compliance_output.evidence_ids
extra_forbidden    execution.compliance_output.adopted_upstream_claim_ids
```

The four old spellings and their replacements:

| v1 | v2 |
| --- | --- |
| `clause_status: ClauseStatus` | `target_clause_status: dict[str, ClauseStatus]` |
| `verification_status: VerificationStatus` | `evidence_provenance.verification_basis` |
| `evidence_ids: list[str]` | `evidence_provenance.opened_paragraph_ids` + `inherited_source_ids` |
| `adopted_upstream_claim_ids: list[str]` | `evidence_provenance.upstream_claim_ids` |

A representative recorded value, from
`gate4-smoke__DEV-POS-COC-0496__E0__A0V0__r000`:

```json
{"clause_status": "present",
 "verification_status": "not_checked",
 "evidence_ids": ["480cfbb8c3b8:p0021"],
 "adopted_upstream_claim_ids": ["C-CHANGE-OF-CONTROL", "...NATION-FOR-CONVENIENCE"],
 "role": "manager"}
```

Note `adopted_upstream_claim_ids` held **claim** ids (`C-CHANGE-OF-CONTROL`),
while `evidence_ids` held a **paragraph** id (`480cfbb8c3b8:p0021`). The v1
schema carried two different id spaces in two flat lists with nothing saying
which was which. That ambiguity is what `EvidenceProvenance` was introduced to
remove — the incompatibility is the fix working, not a defect in it.

### One incompatibility that did **not** announce itself

`scripts/gate4_smoke_check.py` check 5 ("A0 could not access the source") reads
`manager_output.evidence_provenance`. Against a v1 tree that key is absent, so
both of its provenance assertions read empty lists and the check **passed** —
not because A0 held only the memo's citations, but because nothing was read at
all. It reported `"passed": true` with the weaker note "no A0 run reached a
source tool", where at Gate-4 the same check had said "E0 x A0V0 cited 1
evidence id(s) from the memo".

That is a check that stops checking without failing, which is worse than one
that crashes. Check 5 now detects the missing block and reports itself as
`PARTIAL`, naming the runs and stating that the stronger claim is not supported
by a tree in that state. The tool-level isolation assertions still run and still
hold; only the provenance half is withheld.

Check 8 already refused the tree, and now diagnoses *why*: it reads the recorded
`prompt_version` values out of `model_calls.jsonl` and reports
`compliance_v1, manager_v1`, so the failure reads as a version mismatch rather
than as corruption.

---

## 3. Score rows — `RunScore` (5 errors per row)

```
missing            gold_target_clause_status
missing            target_category
extra_forbidden    gold_clause_status
extra_forbidden    manager_clause_status
extra_forbidden    compliance_clause_status
```

`RunScore` gained three fields beyond the renames: `target_category`,
`manager_/compliance_fact_recovery_correct`, and the
`manager_/compliance_verification_basis_{declared,derived,agrees}` triple — the
observable that separates "the node says it verified" from "the ledger shows it
did".

`experiment_summary.json` still loads in both trees, because it carries
aggregates rather than per-run fields. It is therefore **not** evidence that the
tree is readable: a summary can be reconstructed from nothing and still parse.

---

## 4. The registry — `cases_v1.json` (3 errors)

```
policy.decision_if_target_unknown   Field required
gold_target_clause_status           Field required
gold_clause_status                  Extra inputs are not permitted
```

`cases_v1.json` was serialized before `CaseSpec` gained the per-category gold
mapping and before `ExperimentalPolicy` gained `decision_if_target_unknown`, so
the current loader refuses it. `cases_v1_v2.json` (12 cases) loads.

### The version gate does not catch this, and that is the real finding

Three on-disk format versions exist, and **all three are still `"1"`**:

| Constant | Where | Value |
| --- | --- | --- |
| `REGISTRY_VERSION` | `experiment/cases.py:64` | `"1"` |
| `SCORE_SCHEMA_VERSION` | `experiment/scoring.py:69` | `"1"` |
| `SUMMARY_VERSION` | `experiment/summary.py:47` | `"1"` |

`CaseRegistry.from_payload` does check `registry_version` and raises on a
mismatch — but `cases_v1.json` declares `"1"` and `REGISTRY_VERSION` is `"1"`,
so the gate opens and the failure surfaces later as a pydantic `ValidationError`
with no mention of versions. `score_schema_version` is worse: it is written into
every row and into the summary and **read by nothing** (four references under
`src/`, all of them writes).

The consequence is not confined to the old files. A future revision that only
*adds* an optional field would produce rows that the old reader accepts
silently, with `score_schema_version` still saying `"1"` on both sides and
nothing to distinguish them. The version fields are currently decorative.

**Recommendation (not implemented — it is a change, and this task is a report):**
bump `REGISTRY_VERSION` to `"2"` so `cases_v1.json` is refused by the version
gate with a message that names the revision, and have the score reader assert
`score_schema_version` on load rather than defaulting it. Both are small; both
are outside the "prompts, schemas and policy definitions" scope of this stage.

---

## 5. What is unaffected

- **Run plans.** All four (`gate4-development`, `gate4-smoke`,
  `gate5-development`, `gate5-regression`) load. They carry no `AgentOutput`
  and no policy fingerprint, which is why the Gate-4 plan is byte-identical
  across the two policy revisions.
- **Aggregate summaries.** Both `experiment_summary.json` files load.
- **The frozen fingerprints.** All five reproduce exactly — see §6.
- **The Gate-4 prompt files.** `prompts/*_v1.md` are unedited; their sha256
  values are pinned in `tests/test_prompts.py`.

---

## 6. Frozen invariants, re-verified after every change

```
v1 policy fingerprint:    sha256:d507013815d96a97021d5fe8412d44335e64acfe054e75475d6ce1dd55c0005f
v2 policy fingerprint:    sha256:976571cc0be2f781e54e4f9d0af482a7a88e4156c809b4132205d225618e0cd9
selection fingerprint:    sha256:f24db8fc67c2e681c9cca9736a9856c047a71993cadb6d8a3b2ee2b62383e897
Gate-4 plan fingerprint:  sha256:294287c6c33055c2058c71fd6352163b9a6f941700e0364428bf30c78c2f5019   (60 jobs)
smoke plan fingerprint:   sha256:ba9d4f4abb3d73c0e31f6df5d4edcdc06821e10c800ebfc7a5f3840b86ca9e46   (6 jobs)
```

The selection and both plan fingerprints are **identical under revision 1 and
revision 2** — the refinement moved the policy and nothing else. That is the
structural claim the whole refactor rests on, and it is checked by recomputing
each value from a fresh build rather than by reading it back out of the artifact
it describes.

---

## 7. Two outputs that were touched, and their disposition

- `outputs/development/smoke_check.json` — **overwritten** by a re-run of
  `gate4_smoke_check.py` during Stage 10 item 1, then **restored** from
  `outputs_snapshot_gate4.5/development/smoke_check.json` and verified back at
  `sha256:49986c0ef5e0a2b96176dc175fe45657` (all 10 checks pass — it describes
  the Gate-4 tree read by the Gate-4 code). The current-code run is written
  beside it as `outputs/development/smoke_check_v2.json`, following the
  `_v2` convention already used by `prompt_review_v2.json`,
  `selection_audit_v2.json`, `memo_qc_v2.json` and `cases_v1_v2.json`.
  It reports 9 pass, check 8 fail.
- `outputs/raw/gate5-regression/` — created and re-created by the Stage 10 item
  2 harness. It is not a prior development output; no Gate-4 tree was written
  to.

`scripts/build_development_cases.py --policy-version 1` was **deliberately not
re-run**. It would rewrite `cases_v1.json` and `development_cases_v1.json` — the
Gate-4 record — with a v2-schema serialization under the v1 policy, which is
exactly the overwrite the task forbids. The v1 build was instead reproduced in
memory and confirmed byte-identical, which is what the frozen selection
fingerprint in §6 already asserts.

---

## 8. Stage 10 status

| Item | Status |
| --- | --- |
| 1. Run existing smoke tests | **done** — 10 checks, 9 pass, check 8 fails on the version mismatch of §2 |
| 2. Run a small regression batch | **done** — `scripts/gate4_5_regression_batch.py`, 9 jobs, 9 completed, 9 scored, 0 protocol failures, under both revisions |
| 3. Report schema incompatibilities | **this document** |

The regression batch is offline and scripted: it proves the pipeline carries
what it was handed, and it is not a result. Its `accuracy` of 9/9 is a property
of the script, not of the system under study, and it must not be reported as
one.

### Subsequent decision (2026-09-23)

§2 of the spec defines `absent` as "checked against the original contract source
and no matching clause exists". Read literally that makes `absent` — and
therefore ACCEPT — unreachable under A0V0. The approved layering (evidence-
relative status semantics, with `verification_basis` carrying evidence strength)
is what `prompts/*_v2.md` and `config/policies/policy_v2.yaml` implement. This
was confirmed by the supervisor, as reported by the researcher after Stage 10.
The v2 prompts and policy already implement that reading. This addendum does
not change what the Stage 10 smoke checked at the time.

After Stage 10, the checker was tightened and the v1 tree was rechecked into
`outputs/development/smoke_check_v2_rechecked.json`: eight checks pass, check 5
is `PARTIAL`, and check 8 fails on the documented schema mismatch. The original
`smoke_check.json` and Stage 10's `smoke_check_v2.json` remain as historical
outputs. The recheck still does not qualify as a live smoke of the v2 pipeline.
