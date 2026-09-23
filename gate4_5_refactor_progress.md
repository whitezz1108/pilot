# Gate 4.5 pipeline refinement — completion record

Status: **COMPLETE.** The suite is green (983 tests, under both `-p no:randomly`
and random ordering), all five frozen fingerprints reproduce, and the Stage 10
verification runs are done. See `gate4_5_schema_compatibility.md` for the
incompatibility report against the previous outputs.

Sections 1–4 and 7 below are the design record as it stood at the pause point;
§5 and §6 have been replaced by what actually happened.

---

## 1. What this change is

Refining the existing Gate 4.5 pipeline — prompts, schemas, policy definitions —
so that the Gate 4 review findings can be tested confirmatorily. Not a redesign:
the workflow architecture, the agent roles, the workflow order, the treatment
definitions, the dataset cases and the development-batch outputs are all
untouched.

The four objectives, from the task spec:

1. strengthen the distinction between A1V0 and A1V1;
2. introduce explicit REVIEW handling for uncertainty;
3. improve evidence provenance tracking;
4. update acceptance policy semantics.

## 2. Design decisions already settled

Two structural contradictions in the spec were resolved, both by the researcher:

**Layered decision semantics (§3 vs §7).** §3 defines the decision mapping; §7
is a *protocol check* that applies only under V=1. Without the layering, §7's
"ACCEPT requires the original source to have been reviewed" would make ACCEPT
structurally unreachable under A0V0 — which is the same defect that was just
diagnosed as the sentinel root cause, reintroduced by the fix. Under the
layering, status semantics are *evidence-relative* (`absent` = the evidence
available to this agent supports absence), evidence strength is carried by
`verification_basis`, and V=1 separately requires `self_checked` without
changing what the decision means.

**Re-derivation stays the truth.** `source/verification.py` already re-derives
the verdict from the tool-call log and explicitly states that "verified is a
claim, and a claim is not evidence". §5 asked to trust the model's self-report;
the resolution is to keep the derived basis as the verdict and record the
declared basis *beside* it. That creates a new observable — declared vs derived
agreement — which `v1` could not see at all.

**Destructive replacement, versioned.** Old outputs are marked incompatible
rather than kept loadable. `outputs_snapshot_gate4.5/` holds a 9.9M copy of
`outputs/` taken before any schema change (`outputs/` is gitignored).

## 3. Frozen invariants — do not move these

| Artifact | Value |
| --- | --- |
| POLICY-01 **v1** fingerprint | `sha256:d507013815d96a97021d5fe8412d44335e64acfe054e75475d6ce1dd55c0005f` |
| POLICY-01 **v2** fingerprint | `sha256:976571cc0be2f781e54e4f9d0af482a7a88e4156c809b4132205d225618e0cd9` |
| selection fingerprint | `sha256:f24db8fc67c2e681c9cca9736a9856c047a71993cadb6d8a3b2ee2b62383e897` |
| Gate-4 plan fingerprint | `sha256:294287c6c33055c2058c71fd6352163b9a6f941700e0364428bf30c78c2f5019` (60 jobs) |
| smoke plan fingerprint | `sha256:ba9d4f4abb3d73c0e31f6df5d4edcdc06821e10c800ebfc7a5f3840b86ca9e46` (6 jobs) |

All five reproduce from a fresh build after every structural change. The
selection and both plan fingerprints are **identical under revision 1 and
revision 2**: the refinement moved the policy and nothing else.

- `config/policies/policy_v1.yaml` is **unchanged on disk, deliberately** — its
  fingerprint must stay at the Gate 4.5 value.
- `prompts/*_v1.md` are unchanged; v1 stays loadable for reproducing the
  development batch.
- The governance sentences in `render.py` (`MAY_NOT_SEARCH`, `MAY_SEARCH`,
  `MUST_VERIFY`, `UNVERIFIABLE`) are **unchanged** — they are the treatment.
- `MAX_FORMAT_REPAIRS = 1` unchanged. No new validator component was added.

## 4. What is done

### Stage 0 — versioning scaffolding ✅
- `outputs_snapshot_gate4.5/` created before any schema edit.

### Stage 1 — schemas ✅ (`src/pilot01/schemas.py`, `src/pilot01/config.py`)
- `Decision` gained `REVIEW` (gold never predicts it).
- `VerificationBasis` (new): `upstream_only` / `self_checked` / `unavailable`.
- `EvidenceProvenance` (new, frozen): `upstream_claim_ids`,
  `inherited_source_ids`, `opened_paragraph_ids`, `verification_basis`.
- `AgentOutput` rewritten: `target_clause_status: dict[str, ClauseStatus]` +
  `decision` + `rule_id` + `evidence_provenance` + `confidence` +
  `reason_summary` + `uncertainties`. **Removed**: `clause_status`,
  `verification_status`, `evidence_ids`, `adopted_upstream_claim_ids`.
- `expected_decision` now takes a `Mapping[str, ClauseStatus]`; new
  `require_policy_targets` guard.
- `HiddenGold`: `gold_target_clause_status: dict[str, ClauseStatus]` replacing
  the single `gold_clause_status`; validator rejects `unknown` gold.
- `ExperimentalPolicy` gained `decision_if_target_unknown`.
- `config.py`: `PolicyConfig.decision_if_target_unknown` (deliberately **not**
  in `fingerprint()` — `clause_status_mapping` already carries it, and hashing
  it twice moved v1's fingerprint); `_default_unknown` `mode="before"`
  validator so v1 YAML still loads; `load_policy_v2()`.
- `config/policies/policy_v2.yaml` created: revision 2, `unknown → REVIEW`,
  7 edge cases (EDGE-06/07 new).

### Stage 2 — prompts v2 ✅
- `prompts/manager_v2.md`, `prompts/compliance_v2.md`, `prompts/repair_v2.md`.
- `## Required fields` / `## Fields with a default` headings match the schema
  exactly (the guard test parses these two headings).
- Contains the explicit mapping and the instruction *"Do not treat `unknown` as
  a risk finding, and do not convert `unknown` into `ESCALATE`."*
- Validated against every guard in `tests/test_prompts.py` by a standalone
  script: forbidden substrings, `e0`/`e1` tokens, field-name coverage, enum
  coverage, required/defaulted partition, "Defaults to" presence, and the
  "no substantive rule for an incomplete memo" phrase list. **All pass.**
  (One trap: `repair_v2.md` needs `"do not add, remove or replace any evidence"`
  on a single line — a newline inside the phrase fails the guard.)

### Stage 3 — prompt loading ✅ (`src/pilot01/prompts.py`)
- Added `V2`, `CURRENT_VERSION = V2`. The role loaders and `load_prompt` now
  default to `CURRENT_VERSION`, so the nodes pick up v2 without edits.

### Stage 4 — rendering & nodes ✅ (partly)
- `render.py`: `render_policy_block` emits the three-way mapping including
  REVIEW; `render_manager_handoff_block` emits the new shape.
- `llm_agent.py`: `_describe` gained an `object` branch so the derived repair
  field list describes `target_clause_status` and the nested
  `evidence_provenance` instead of collapsing to `"object"`. Order matters —
  `properties` is tested **before** `additionalProperties`, because
  `extra="forbid"` emits `additionalProperties: false` alongside `properties`.
- `manager.py`: no edit needed (default version flip covers it).
- `compliance.py`: `compliance_upstream_ids` now reads
  `evidence_provenance.inherited_source_ids`; the handoff's
  `opened_paragraph_ids` are deliberately **not** included.
- `state.py`: `_check_gold_consistency` uses `require_policy_targets` +
  per-category mapping.

### Stage 5 — verification & ledger ✅
- `source/verification.py`: `derive_basis()` computes the basis from the
  **ledger**, not the output. `VerificationOutcome` carries `declared_basis` /
  `derived_basis` / `basis_agrees` / `target_clause_status` /
  `opened_paragraph_ids` / `inherited_source_ids`. New failure
  `BASIS_NOT_SELF_CHECKED`; `CHECK_INCOMPLETE` redefined as
  declared-vs-derived disagreement; `STATUS_NOT_VERIFIED` retained (never
  raised) so v1-era records still parse. `status` is always the derived verdict.
- `source/ledger.py`, `source/tools.py`: docstrings updated; `record_upstream`
  renamed its parameter and documents why the previous stage's
  `opened_paragraph_ids` are excluded.

### Stage 7 — cases ✅ (partly)
- `experiment/cases.py`: `gold_target_clause_status` + validator (non-empty,
  no `unknown`, keys == policy targets, `target_category` must be among them);
  `gold_action` derived from it; `gold_status_for()` and `gold_target_category`
  helpers.

## 5. What was done after the pause point

**Stage 6 — scoring** ✅ (`experiment/scoring.py`): per-category
`clause_status_correct`; provenance-based `_assess_evidence`; per-category
`_error_survival` / `_correction_stage`; new `fact_recovery_correct`; the
`verification_basis_declared` / `_derived` / `_agrees` triple.

**Stage 7 (rest) — cases/registry** ✅: `experiment/development/registry.py`,
`experiment/development/selection.py`, `experiment/batch.py`.

**Stage 8 — scripts** ✅: `gate4_batch_check.py`, `gate4_batch_report.py`,
`review_prompts_gate4.py`, `gate4_smoke_check.py`, `gate4_budget_probe.py`.
The registry filename rule now lives in one place,
`build_development_cases.registry_filename()`; revision 1 keeps the unsuffixed
`cases_v1.json` it was written under, every later revision writes its own file
beside it, and `gate4_smoke.py` / `gate4_smoke_check.py` ask for it rather than
spelling it out.

**Stage 9 — tests** ✅: 983 tests, green under both `-p no:randomly` and random
ordering. The v1-era assertions were replaced with version-independent ones
rather than being deleted:

- `tests/test_prompts.py` pins the sha256 of `prompts/*_v1.md`, so a prompt
  revision has to be a new file and cannot be an edit to a retired one.
- `tests/test_treatment_isolation.py` no longer compares two version-pinned
  strings. It asserts that the two SOURCE TOOLS sections are equal after
  normalising the artifact noun, **and** that they really do differ by that
  noun — so the equality is not the vacuous kind that holds because neither
  section names anything. This is what caught the v2 wording introducing an
  incidental extra difference; the fix was to reword the prompts, not to widen
  the normaliser.
- `tests/test_source_access_conditions.py` gained
  `test_the_v1_status_failure_is_retained_but_never_raised`, which reads the
  module source and asserts `VerificationFailure.STATUS_NOT_VERIFIED` appears in
  no raised path.

**Stage 10 — verification runs** ✅: see `gate4_5_schema_compatibility.md`.

## 6. A new script

`scripts/gate4_5_regression_batch.py` — Stage 10 item 2. Offline and scripted,
no model call. It builds the set at the named policy revision, takes one
positive and one sentinel case across all cells (9 jobs), writes the plan and
the declared response script, runs the real `ExperimentRunner` against a
`ScriptedClientFactory`, scores, and writes `run_scores.jsonl`, `summary.json`
and `regression_report.json`. `--policy-version` selects the revision and
`--out` the root, so it can be run under v1 without touching the v2 tree.

It refuses to overwrite its own observations: if the raw tree already holds
completed runs it stops and says so rather than re-running over them. A
regression batch that silently replaced its previous run would make "the
pipeline passed" unfalsifiable.

A scripted batch proves the pipeline carries what it was handed. Its accuracy is
a property of the script, not of the system under study, and it is not a result.

## 7. Subsequent research decisions (2026-09-23)

§2 of the spec defines `absent` as "has been checked against the original
contract source and no matching clause exists". Read literally, that makes
`absent` — and therefore ACCEPT — unreachable under A0V0. This is the **third**
instance of the same structural problem the researcher already ruled on twice.

Proposed, applying the approved layering: status semantics are evidence-relative
(`absent` = the evidence available to this agent supports absence); evidence
strength is carried by `verification_basis`; V=1 separately enforces
`self_checked` as a protocol check without changing the decision.

The researcher subsequently reported the supervisor's confirmation: `absent`
means that the evidence available to the current node clearly supports the
target clause's absence. The v2 prompts' evidence-relative wording is therefore
the approved interpretation. Mere silence about a category is insufficient and
should be reported as `unknown`. This does not turn a memo citation into a
self-opened passage; A1V1's source-verification requirement remains a separate
protocol check.

The supervisor also confirmed `omission_recovered_with_evidence` as the primary
outcome and that A0V0 scores zero on it because original-source access is
unavailable. The metric applies to positive E1 observations; sentinels have no
omitted claim and protocol failures must be reported separately rather than
silently counted as zero. `RunScore.omission_recovered_with_evidence` now
exposes the either-node roll-up for completed positive E1 runs: A0V0 is false,
while E0, sentinels, and protocol failures are not applicable. The formal
analysis plan must state this denominator before the run.

Engineering follow-through after those decisions: Check 5 now returns a
machine-readable `PARTIAL` and refuses source ids absent from the actual memo;
Check 8 names a version mismatch only for a superseded prompt revision. The
offline regression script keeps each revision's raw tree under its own
`development/regression_v<revision>/` directory. A1V1 now requires a
category-named search for each policy target and rejects any paragraph id
reported as self-opened when this node did not open it. This checks the
recorded verification procedure; it cannot prove semantically that an absent
clause does not exist anywhere in a contract. Both revisions' 9-job scripted
batches completed in one output root with identical plan fingerprints. The
990-test offline suite passed under fixed and randomized ordering, including
tests for the confirmed A0V0 outcome and current-prompt error handling.
The clarified prompt instructions are versioned as v3: v2 files are preserved
and pinned by hash, while new runs record `manager_v3` / `compliance_v3`. Both
policy revisions' 9-job scripted batches also completed under v3. A
current-version *live* smoke has not yet been run because this environment has
neither `PILOT01_BASE_URL` nor `PILOT01_API_KEY` configured.
