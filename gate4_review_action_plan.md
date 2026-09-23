# Gate 4 Review — Action Plan

**Status: PROPOSAL ONLY. Nothing implemented. No code, prompt, policy, case or config
has been modified.**

Scope of this document: diagnose the two open items from the Gate 4.5 development
batch and propose the minimal change set required before the confirmatory gate.

Explicit constraints honoured throughout:

- **No prompts changed.** `prompts/manager_v1.md`, `prompts/compliance_v1.md`,
  `prompts/repair_v1.md` untouched.
- **No model changed.** `config/models_v1.yaml` untouched.
- **No system rebuilt.** The runner, node, view, scoring and batch architecture is
  taken as given.
- **No policy, case or scorer edited.** All findings below are read off the frozen
  artifacts.

Sources used: `outputs/processed/gate4-development/run_scores.jsonl` (60 rows),
`outputs/raw/gate4-development/*/run.json` (raw agent outputs),
`outputs/development/cases_v1.json`, `config/policies/policy_v1.yaml`,
`src/pilot01/experiment/scoring.py`, `src/pilot01/experiment/development/diagnostics.py`.

---

## 0. Executive summary

| Item | Verdict |
|---|---|
| **sentinel_false_escalation** | **PRIMARY cause: policy definition.** SECONDARY: case construction (1 of 6). Scoring *per-run* logic is correct; the *aggregation* is mis-specified. |
| **GATE4-DESIGN-01** | Resolved structurally: `final_action_correct` is a **deterministic, strictly-lossy** function of `fact_recovery_correct` under POLICY-01. It cannot add information, and it provably destroys exactly the EDGE-01 cases. |
| **Minimal change before Gate 5** | 4 changes. 3 are reporting/aggregation only (zero behaviour change). 1 is a **policy-wording decision that only the researcher can make** — and it must be made *before* the confirmatory freeze. |

The single most important sentence in this document:

> `final_action_correct` is not merely "absorbing signal" from the fact metric.
> Under POLICY-01 it is a total function *of* the fact metric, so it is provably
> incapable of carrying information the fact metric does not already carry — and
> the one case where they disagree is produced by EDGE-01, which flatters the
> positive arm and penalises the sentinel arm with the same rule.

---

## 1. Finding 1 — `sentinel_false_escalation`

### 1.1 What the check actually is

`src/pilot01/experiment/development/diagnostics.py:519-533`

```
check_id="sentinel_false_escalation"
kind="readiness"
verdict_if_failed="REVISE"
passed = rate.value is None or rate.value <= MAX_SENTINEL_FALSE_ESCALATION   # 0.50
```

Observed 6/10 (60.0%) → the only failing check in the batch, and the sole driver
of the pre-declared `REVISE` verdict.

Per-run, `false_escalation` is defined at `scoring.py:555`:

```
if not is_negative_sentinel: return None
if final_decision is None:   return None
return final_decision is Decision.ESCALATE
```

### 1.2 The six failures, decomposed

All sentinel runs, from the frozen raw artifacts:

| Condition | Case | Manager status | Compliance status | Final | False esc. |
|---|---|---|---|---|---|
| A0V0 | DEV-SENT-COC-0123 | `absent` ✅ | **`unknown`** | ESCALATE | **True** |
| A0V0 | DEV-SENT-COC-0204 | `absent` ✅ | **`unknown`** | ESCALATE | **True** |
| A0V0 | DEV-SENT-TFC-0395 | `absent` ✅ | **`unknown`** | ESCALATE | **True** |
| A0V0 | DEV-SENT-TFC-0405 | `absent` ✅ | **`unknown`** | ESCALATE | **True** |
| A1V0 | DEV-SENT-COC-0204 | `absent` ✅ | `absent` ✅ | ACCEPT | False |
| A1V0 | DEV-SENT-TFC-0405 | `absent` ✅ | `absent` ✅ | ACCEPT | False |
| A1V1 | DEV-SENT-COC-0123 | **`present`** ❌ | `present` ❌ | ESCALATE | **True** |
| A1V1 | DEV-SENT-TFC-0395 | **`unknown`** ❌ | `unknown` ❌ | ESCALATE | **True** |
| A1V1 | DEV-SENT-COC-0204 | `absent` ✅ | `absent` ✅ | ACCEPT | False |
| A1V1 | DEV-SENT-TFC-0405 | `absent` ✅ | `absent` ✅ | ACCEPT | False |
| A1V0 | DEV-SENT-COC-0123 | — | — | — | ProtocolError |
| A1V0 | DEV-SENT-TFC-0395 | — | — | — | ToolLoopError |

Two **mechanically distinct** failure modes are pooled into the single 6/10 rate:

- **Mode A — `absent → unknown` at Compliance. 4 of 6, all in A0V0.**
  The Manager is *correct*; Compliance converts a correct `absent` into `unknown`.
- **Mode B — the Manager itself is wrong. 2 of 6, both in A1V1.**
  One hallucinated `present`, one `unknown`.

### 1.3 Mode A is condition-specific, and that is decisive

Manager → Compliance status transitions over all 57 completed runs:

| Transition | Count |
|---|---|
| `present → present` | 38 |
| `unknown → unknown` | 9 |
| `absent → absent` | 5 |
| **`absent → unknown`** | **4** |
| `unknown → present` | 1 |

Compliance downgrades a correct `absent` to `unknown` **4 times, all of them in
A0V0 — and 0 times in 18 A1V0 runs and 0 times in 19 A1V1 runs.**

A behaviour that occurs 4/4 in one governance cell and 0/37 in the other two is
not a sampling accident. It is the condition.

### 1.4 The mechanism, in the agents' own words

`outputs/raw/gate4-development/gate4-development__DEV-SENT-COC-0123__E0__A0V0__r000/run.json`

Manager (correct):

> clause_status `absent`, decision `ACCEPT`, confidence 0.7 —
> *"The analyst memo records both target categories as absent … so the condition
> for ACCEPT is met."*
> uncertainties: *"No source tools are available and the source contract is
> inaccessible, so the two absence claims could not be verified."*

Compliance (converts to `unknown`):

> clause_status `unknown`, decision `ESCALATE`, verification_status `unverifiable` —
> *"POLICY-01 … permits ACCEPT only when the target clauses are **confirmed**
> absent. The handoff asserts both categories absent, but records verification as
> `not_checked`, cites no source ids, and supplies no evidence; the two absence
> claims … are unverified assertions I have no means to confirm, since source
> access is disabled."*

Compliance is not malfunctioning. It is reading POLICY-01 literally.

### 1.5 Root-cause verdict

**PRIMARY — policy definition.**

`config/policies/policy_v1.yaml`:

- `description`: *"Only when the relevant clauses are **confirmed** absent may the
  case be ACCEPT."*
- `edge_cases[EDGE-01]`: *"Only a **confirmed** absence permits ACCEPT."*

"Confirmed" is **agent-relative but the policy never says who confirms, or what
evidence suffices to confirm.** Under A0V0 the only admissible evidence is the
frozen memo. A literal reading therefore makes confirmation impossible in A0V0,
which makes the gold action ACCEPT **structurally unreachable** in that cell.

This is a defect in the *rule as a measurement instrument*: a policy that cannot
return its own gold action in one of its three governance cells is not well
defined with respect to the design. The sentinel check is doing its job — it has
found a real specification gap.

Note the irony worth recording: EDGE-01's stated rationale is *"Treating an
unresolved question as a negative answer is the failure mode this experiment is
about, so the rule must not do it itself."* The rule's conservatism, written to
prevent one failure mode, manufactures a second one — it escalates a **correctly
identified absence**.

**SECONDARY — case construction. Contributes to 1 of 6, and to 1 more partial.**

`DEV-SENT-COC-0123` contains, at contract offset ≈9107:

> *"11. STW Termination Rights. STW shall have the right to unilaterally terminate
> the provisions of this AGREEMENT related only to the Property, and not proceed
> further after the completion of any phase of the project and not incur any
> additional costs."*

A discretionary, no-fault, unilateral termination right is, in substance, a
termination-for-convenience provision. CUAD's annotation records no TFC clause in
this contract, so gold is `absent`. The A1V1 Manager opened this paragraph and
concluded `present` — a defensible reading that the sentinel's label treats as an
error.

For a **negative sentinel** to be a clean readiness probe, the contract must be
free of near-misses for the target category. This one is not. This does **not**
explain the 4/4 A0V0 pattern (those agents never saw the contract), but it does
mean one of the two A1V1 failures is partly a case-construction artefact rather
than a pure model error.

**NOT A CAUSE — scoring logic (per run).**

`_false_escalation`, `_clause_status_correct` and the transition table all
faithfully record what the agents actually emitted. Every one of the 6 rows is a
correct description of a real event. Nothing is mis-scored.

**BUT — the aggregation is mis-specified.** Three separate defects:

1. **Conditions are pooled.** 6/10 = A0V0 4/4 + A1V0 0/2 + A1V1 2/4. The three
   cells have different mechanisms; the A0V0 portion is a *design defect* while
   the A1V1 portion is a *behavioural observation*.
2. **The A1V0 denominator is halved.** 2 of its 4 runs died on protocol failures
   (`ProtocolError`, `ToolLoopError`). `0/2` is not evidence of anything.
3. **The check's `kind` is arguably wrong.** `kind="readiness"` with
   `verdict_if_failed="REVISE"`, but a cell in which the gold action is
   unreachable is what the same module's taxonomy calls `kind="design"` with
   `verdict_if_failed="STOP"`.

---

## 2. Finding 2 — GATE4-DESIGN-01

### 2.1 The question, restated

Gate 4 reported that 8 of 24 E1 positive runs reached the correct action while the
omission survived, because POLICY-01 maps `UNKNOWN → ESCALATE`. The report
concluded that `final_action_correct` "cannot separate a decision that was right
from one that was right by accident."

That conclusion is correct but **understates the problem**. The relationship is
not weak correlation — it is a functional dependency.

### 2.2 The structural proof

POLICY-01 is a **total function** `clause_status → decision`
(`policy_v1.yaml: clause_status_mapping`):

```
present → ESCALATE
absent  → ACCEPT
unknown → ESCALATE
```

`expected_decision` in `pilot01.schemas` is the reference implementation, and a
test asserts the YAML table and that function agree on every status.

Therefore, since `final_action_correct = (decision == gold_action)` and
`fact_recovery_correct = (clause_status == gold_status)`:

> **`fact_recovery_correct = True` ⟹ `final_action_correct = True`.**

The converse does not hold, and the gap is exactly EDGE-01: a wrong status
(`unknown`) that maps onto the right action when gold is `present`.

### 2.3 The proof is confirmed by the frozen data

2×2 over all 57 completed runs — `final_action_correct` × `fact_recovery_correct`:

| | fact recovered | fact **not** recovered |
|---|---|---|
| **action correct** | 42 | **8** |
| **action incorrect** | **0** | 7 |

**The `action incorrect / fact recovered` cell is empty — 0 of 57.**

That is not a coincidence and not a small sample. It is the theorem above. Getting
the fact right *always* gets the action right, because the action is computed from
the fact.

Restricted to the GATE4-DESIGN-01 population (E1 positive, n = 23):

| | fact recovered | fact **not** recovered |
|---|---|---|
| **action correct** | 14 | **8** ← DESIGN-01 |
| **action incorrect** | **0** | 1 |

And the 8 DESIGN-01 runs are precisely the A0V0 E1 cell, every one of them
`manager = unknown, compliance = unknown, correction_stage = never`:

```
A0V0 DEV-POS-COC-0188  act=True  fact=False  mgr=unknown cmp=unknown stage=never
A0V0 DEV-POS-COC-0286  act=True  fact=False  mgr=unknown cmp=unknown stage=never
A0V0 DEV-POS-COC-0371  act=True  fact=False  mgr=unknown cmp=unknown stage=never
A0V0 DEV-POS-COC-0496  act=True  fact=False  mgr=unknown cmp=unknown stage=never
A0V0 DEV-POS-TFC-0011  act=True  fact=False  mgr=unknown cmp=unknown stage=never
A0V0 DEV-POS-TFC-0212  act=True  fact=False  mgr=unknown cmp=unknown stage=never
A0V0 DEV-POS-TFC-0334  act=True  fact=False  mgr=unknown cmp=unknown stage=never
A0V0 DEV-POS-TFC-0436  act=True  fact=False  mgr=unknown cmp=unknown stage=never
```

**EDGE-01 is the single mechanism behind both open items.** It flatters the
positive arm (8 runs correct-by-accident) and penalises the sentinel arm (5 of 6
false escalations route through `unknown → ESCALATE`) — with the same rule, in the
same batch.

### 2.4 The three constructs, separated

| Construct | Definition | Existing field | Status |
|---|---|---|---|
| `final_action_correct` | terminal `decision == gold_action` | `RunScore.final_action_correct` | exists; **lossy projection** |
| `fact_recovery_correct` | terminal `clause_status == gold_status` | `RunScore.compliance_clause_status_correct` (`scoring.py:496`) | **already computed for every run — but never surfaced as a headline** |
| `corrected_with_evidence` | fact recovered **and** the recovery is evidenced (self-opened, in-contract, overlapping gold span) | `RunScore.compliance_corrected_with_evidence` (`scoring.py:518`) | exists; only defined where gold has a span |

Two properties of `corrected_with_evidence` that matter for the design and that
the Gate 4 report did not state:

1. It returns `None` — **not `False`** — when `gold_status is not PRESENT`
   (`scoring.py:534`). Sentinel cases have `gold_evidence_offsets: []`, so
   `corrected_with_evidence` is **undefined for the entire sentinel class**. It is
   an E1-positive-only instrument.
2. It is *not* a subset of `fact_recovery_correct` in the naive sense — it is the
   strictly stronger conjunction of fact recovery **and** evidence validity. A run
   can recover the fact and fail the evidence test (`A1V1 DEV-POS-COC-0188`:
   `fact=True, ev=False`, manager recovered `present` but the citation did not
   overlap the gold span).

So the correct reporting lattice is three nested, non-interchangeable levels:

```
final_action_correct   ⊇   fact_recovery_correct   ⊇   corrected_with_evidence
     (decision)                  (fact)                   (fact + evidence)
```

with the first inclusion **strict**, and the gap between the first two exactly
equal to the EDGE-01 population.

---

## 3. Minimal changes proposed before Gate 5

Ordered by dependency. **Changes 1–2 are reporting-only and alter no behaviour.
Change 3 is a design decision that changes the experiment and must be made before
the freeze. Change 4 is a case-screening step.**

### Change 1 — Surface `fact_recovery_correct` as a first-class field, and never report `final_action_correct` alone

- Add `RunScore.fact_recovery_correct`, defined as the terminal node's
  `clause_status_correct` (already computed at `scoring.py:496`; this is a
  roll-up, not a new computation).
- Every headline table in the batch report reports the **2×2 lattice**, not a
  single accuracy column.
- **Add the invariant as a test:** `final_action_correct=False and
  fact_recovery_correct=True` must be 0 on any batch scored against POLICY-01.
  It is a theorem of the policy, so a non-zero count means either the policy's
  status→action mapping or the scorer has drifted. This is a cheap, high-value
  guard — it converts a subtle measurement property into a hard assertion.

*Rationale: minimal, purely additive, zero behavioural risk, and it makes
GATE4-DESIGN-01 structurally impossible to re-commit.*

### Change 2 — Split the sentinel readiness check

- Report `sentinel_false_escalation` **per condition**, not pooled. A0V0 4/4,
  A1V0 0/2, A1V1 2/4 are three different statements.
- Report the **mechanism** alongside the rate: the terminal `clause_status` path
  (`unknown` vs `present`). A policy-gap and a model error must not share a rate.
- **Refuse to judge a cell whose denominator is incomplete.** A1V0 sentinel is 2
  completed of 4 planned; `0/2` must be reported as *insufficient*, not as 0%.
- Re-classify the A0V0 sentinel finding from `kind="readiness"` /
  `verdict_if_failed="REVISE"` to `kind="design"` / `verdict_if_failed="STOP"`,
  since "the gold action is unreachable in this cell" is a design defect.

*Rationale: the rate as currently computed is not a well-defined quantity. This
is a change to the diagnostic's aggregation, not to any per-run score.*

### Change 3 — **DESIGN DECISION REQUIRED**: make POLICY-01's "confirmed" well defined

This is the only change that alters the experiment, and **it must be taken by the
researcher, not by me.** Two coherent options:

**Option 3a — make "confirmed" agent-relative and explicit.**
Amend POLICY-01's wording so that "confirmed absent" means *"the best evidence
available to the deciding agent supports absence"*, and state that where source
access is disabled the memo is the whole of the available evidence. Consequence:
ACCEPT becomes reachable in A0V0; the sentinel becomes a valid probe in all three
cells; A0V0 gains a meaningful ACCEPT branch.

**Option 3b — keep "confirmed" strict and restrict the check.**
Accept that A0V0 cannot return ACCEPT by construction. Then the negative sentinel
is **not a valid readiness probe under A0V0**, and the sentinel check must be
restricted to A1V0/A1V1. Consequence: the A0V0 sentinel cell must be re-labelled
as a *predicted* escalation rather than a false one — which changes the meaning of
the cell and needs its own justification.

**Either way:**
- A policy edit is a **version change**, not an edit. It requires a new
  `policy_version` and a new fingerprint, recorded as such. The current file is
  `sha256:d507013815d96a97021d5fe8412d44335e64acfe054e75475d6ce1dd55c0005f`.
- It must happen **before** the confirmatory freeze, never after seeing
  confirmatory data.
- Gate 4's results remain valid *as development diagnostics under the old policy*
  and must not be retro-fitted.

*Rationale: this is the actual defect. Changes 1–2 make the defect visible; only
this makes it go away. I am not choosing between 3a and 3b — that is a research
design call about whether "an agent with no tools may ACCEPT on the memo's word"
is part of the phenomenon under study or an artefact to be removed.*

### Change 4 — Screen sentinel (and positive) contracts for near-miss clauses

- Add a near-miss screen over sentinel contracts for the target category.
  `DEV-SENT-COC-0123` fails it: §11 "STW Termination Rights" grants a
  discretionary unilateral no-fault termination right, which is functionally
  termination-for-convenience.
- Screen the remaining 3 sentinels before the confirmatory selection.
- Record the screen as an auditable step, in the same spirit as the Gate 2.5
  selection audit — a sentinel whose negative label is contestable is not a
  readiness probe.

*Rationale: a negative sentinel is only informative if the target category is
genuinely, unambiguously absent.*

---

## 4. Explicitly NOT proposed

| Not proposed | Why |
|---|---|
| Changing any prompt | Constraint, and the evidence points away from prompts: Compliance's behaviour in A0V0 is a *correct* reading of POLICY-01. Editing the prompt to make it say "absent" would suppress a real specification finding and manufacture the result. |
| Changing the model | Constraint. The A0V0 pattern is 4/4 vs 0/37 across conditions — that is a condition effect, not a model-quality effect. |
| Rewriting the scorer | The per-run scoring is correct on all 60 rows. Only the aggregation changes. |
| Back-filling cost | No pricing table was configured. Gate 4 reported `COST UNAVAILABLE`, which is correct. |
| Editing cases to fix outcomes | Would be editing the experiment in response to an outcome difference. Change 4 is a *pre-freeze screen*, not a post-hoc repair. |
| Rebuilding the architecture | Not needed and not in scope. |

---

## 5. Open decisions for the researcher

1. **Change 3a or 3b?** The only blocking design decision.
2. **Is A0V0's ACCEPT branch in scope at all?** If the research question is only
   about whether an omission *survives*, A0V0's inability to ACCEPT may be
   acceptable — but then the sentinel check must be scoped accordingly.
3. **Repeats.** Every cell in this batch is a single observation. The A1V0
   sentinel cell lost half its runs to protocol failures, and no cell-level
   difference is separable from run-to-run variation at `repeat_count = 1`. The
   confirmatory design needs repeats before any cell comparison is meaningful.
4. **Which metric is primary for the recovery question?** My recommendation:
   `fact_recovery_correct` primary, `corrected_with_evidence` as the strict
   secondary, `final_action_correct` reported but **never** as a summary of the
   other two.
5. **Sentinel policy for the confirmatory set** — after Change 3, does the
   `MAX_SENTINEL_FALSE_ESCALATION = 0.50` threshold still mean what it meant?

---

## 6. Items carried forward unchanged from the Gate 4 report

Still open, not addressed here, and not affected by the above:

- Human review of the 12 development cases (`human_review_performed: false`; all
  `PENDING_HUMAN_REVIEW`).
- The confirmatory model and prompt freeze.
- Pricing table, if cost is to be reported.
- The case registry currently lives under `outputs/`, which is gitignored — the
  frozen inputs are not under version control. A freeze that exists only on one
  machine's disk is not a freeze.
- Study 2 organizational memory: still unimplemented.

---

*Diagnostic only. No hypothesis test was conducted, no p-value or interval was
computed, and nothing here is a finding about any agent. Every number above is a
count from 12 development cases at one repeat.*
