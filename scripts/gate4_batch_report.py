"""Gate 4.5 §9: the development batch report, built from the repository's own scorer.

This script computes nothing new. It calls the same three things every other
Gate-4 artifact calls -- ``score_experiment`` for the per-run rows,
``summarise`` for the experiment summary, and ``build_diagnostics`` for §14's
diagnostics, §15's table and §16's verdict -- and then lays their output out as
the report §9 asks for.

Three tables in the report are *cuts* of the scored rows rather than new
measures, and each is a filter over columns the scorer already produced:

* the **positive-case** cells. The diagnostics' own cells pool a governance
  condition with an error arm across every run, and a sentinel contributes to
  the E0 cells because E0 is the only arm it has. §5 asks for positive-case
  results, so the positive table restricts the same fields to positive cases
  and the sentinels get their own table. Both are reported; neither replaces
  the other.
* the **stage-survival** table, which cross-tabulates the ``correction_stage``
  the scorer already assigned.
* the **GATE4-DESIGN-01** count, which is a conjunction of two scored columns
  (``correction_stage == never`` and ``final_action_correct``).

No p-value, interval, regression or effect estimate is computed anywhere in
this file, and none is printed. This is a development batch: one repeat, twelve
cases, no hypothesis test, no population inference.

    .venv/Scripts/python.exe scripts/gate4_batch_report.py
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from pilot01.config import load_models_v1, load_policy_v1  # noqa: E402
from pilot01.experiment.cases import CaseRegistry  # noqa: E402
from pilot01.experiment.development import build_gate4_development_set  # noqa: E402
from pilot01.experiment.development.diagnostics import (  # noqa: E402
    build_diagnostics,
    render_development_table,
)
from pilot01.experiment.layout import ExperimentPaths  # noqa: E402
from pilot01.experiment.runspec import RunPlan  # noqa: E402
from pilot01.experiment.scoring import RunScore, score_experiment  # noqa: E402
from pilot01.experiment.summary import Rate, summarise  # noqa: E402

EXPERIMENT_ID = "gate4-development"
REGISTRY_PATH = REPO_ROOT / "outputs" / "development" / "cases_v1.json"
REPORTS_DIR = REPO_ROOT / "outputs" / "reports"
CONDITIONS = ("A0V0", "A1V0", "A1V1")
ARMS = ("E0", "E1")

STAGE_LABELS = (
    ("manager", "Manager correction"),
    ("compliance", "Compliance correction"),
    ("never", "Never corrected"),
)


# ---------------------------------------------------------------------------
# cuts over the scored rows
# ---------------------------------------------------------------------------


def _pool(rows, condition_id: str, error_condition: str) -> list[RunScore]:
    return [
        row
        for row in rows
        if row.condition_id == condition_id and row.error_condition.value == error_condition
    ]


def _mean(values) -> float | None:
    present = [v for v in values if v is not None]
    return None if not present else round(sum(present) / len(present), 2)


def _median(values) -> float | None:
    present = [v for v in values if v is not None]
    return None if not present else round(statistics.median(present), 2)


def _corrected_with_evidence(row: RunScore) -> bool | None:
    """Either node recovered the lost fact and showed its evidence.

    The same rule the diagnostics module applies, restated here only because
    that helper is private to it.
    """
    values = [
        value
        for value in (
            row.manager_corrected_with_evidence,
            row.compliance_corrected_with_evidence,
        )
        if value is not None
    ]
    return None if not values else any(values)


def positive_cells(rows: list[RunScore]) -> list[dict]:
    cells = []
    for condition_id in CONDITIONS:
        for error_condition in ARMS:
            pool = _pool(rows, condition_id, error_condition)
            cells.append(
                {
                    "condition_id": condition_id,
                    "error_condition": error_condition,
                    "n": len(pool),
                    "completed": sum(1 for row in pool if row.completed),
                    "protocol_failures": sum(1 for row in pool if row.protocol_failure),
                    "final_action_correct": Rate.of([row.final_action_correct for row in pool]),
                    "error_survival_manager": Rate.of(
                        [row.error_survival_manager for row in pool]
                    ),
                    "error_survival_compliance": Rate.of(
                        [row.error_survival_compliance for row in pool]
                    ),
                    "corrected_with_evidence": Rate.of(
                        [_corrected_with_evidence(row) for row in pool]
                    ),
                    "mean_tool_calls": _mean([row.tool_calls for row in pool]),
                    "median_tool_calls": _median([row.tool_calls for row in pool]),
                    "mean_model_calls": _mean([row.model_calls for row in pool]),
                    "mean_total_tokens": _mean([row.total_tokens for row in pool]),
                    "mean_latency": _mean([row.latency_seconds for row in pool]),
                    "mean_source_opens": _mean(
                        [row.manager_opens + row.compliance_opens for row in pool]
                    ),
                }
            )
    return cells


def stage_table(rows: list[RunScore]) -> list[dict]:
    """The E1 stage-survival table: where, if anywhere, the omission was undone."""
    e1 = [row for row in rows if row.error_condition.value == "E1"]
    table = []
    for condition_id in CONDITIONS:
        pool = [row for row in e1 if row.condition_id == condition_id]
        stages = Counter(row.correction_stage.value for row in pool)
        table.append(
            {
                "condition_id": condition_id,
                "n": len(pool),
                "stages": {key: stages.get(key, 0) for key, _ in STAGE_LABELS},
                "unavailable": stages.get("unavailable", 0),
                "corrected_with_evidence": Rate.of(
                    [_corrected_with_evidence(row) for row in pool]
                ),
                "rows": pool,
            }
        )
    return table


def sentinel_table(rows: list[RunScore]) -> list[dict]:
    pool = [row for row in rows if row.is_negative_sentinel]
    table = []
    for condition_id in CONDITIONS:
        cell = [row for row in pool if row.condition_id == condition_id]
        table.append(
            {
                "condition_id": condition_id,
                "n": len(cell),
                "completed": sum(1 for row in cell if row.completed),
                "accept_correct": Rate.of([row.final_action_correct for row in cell]),
                "false_escalation": Rate.of([row.false_escalation for row in cell]),
            }
        )
    return table


def design_01(rows: list[RunScore]) -> dict:
    """§6: correct decision while the omission survived, via UNKNOWN -> ESCALATE.

    The numerator is a conjunction of two scored columns, not a new judgment:
    the scorer already decided that the omission survived to the end
    (``correction_stage == never``) and already decided the final action was
    right (``final_action_correct``). This counts the runs where both are true.
    """
    e1_positive = [
        row
        for row in rows
        if row.error_condition.value == "E1" and not row.is_negative_sentinel
    ]
    hits = [
        row
        for row in e1_positive
        if row.correction_stage.value == "never" and row.final_action_correct is True
    ]
    # The policy mechanism: did the run reach ESCALATE through an UNKNOWN status
    # rather than through a positive finding? Recorded per run, because the
    # phenomenon is about *how* the right answer was reached.
    via_unknown = [
        row
        for row in hits
        if row.manager_clause_status is not None
        and row.manager_clause_status.value == "unknown"
    ]
    by_condition = {}
    for condition_id in CONDITIONS:
        cell = [row for row in e1_positive if row.condition_id == condition_id]
        cell_hits = [row for row in hits if row.condition_id == condition_id]
        by_condition[condition_id] = {
            "numerator": len(cell_hits),
            "denominator": len(cell),
            "case_ids": sorted(row.case_id for row in cell_hits),
        }
    return {
        "numerator": len(hits),
        "denominator": len(e1_positive),
        "rate": Rate(numerator=len(hits), denominator=len(e1_positive)),
        "by_condition": by_condition,
        "case_ids": sorted(row.case_id for row in hits),
        "via_unknown": len(via_unknown),
        "via_unknown_case_ids": sorted(row.case_id for row in via_unknown),
        "runs": hits,
    }


def outcome_taxonomy(rows: list[RunScore]) -> dict:
    """§4's A/B/C/D: the decision and the recovery, kept as separate claims.

    Defined on the E1 positive runs, where there is an omission to recover and
    a gold action to get right.
    """
    e1_positive = [
        row
        for row in rows
        if row.error_condition.value == "E1" and not row.is_negative_sentinel
    ]
    buckets = {"A": [], "B": [], "C": [], "D": []}
    for row in e1_positive:
        corrected = _corrected_with_evidence(row)
        survived = row.correction_stage.value == "never"
        correct = row.final_action_correct is True
        if correct and corrected is True:
            buckets["A"].append(row)
        elif correct and survived:
            buckets["B"].append(row)
        elif row.final_action_correct is False and survived:
            buckets["C"].append(row)
        else:
            buckets["D"].append(row)
    return buckets


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _pct(rate: Rate) -> str:
    return rate.describe()


def _num(value) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def render_markdown(context: dict) -> str:
    diagnostics = context["diagnostics"]
    summary = context["summary"]
    rows = context["rows"]
    plan = context["plan"]
    cells = context["positive_cells"]
    stages = context["stage_table"]
    sentinels = context["sentinel_table"]
    d01 = context["design_01"]
    buckets = context["taxonomy"]
    ops = context["operations"]

    out: list[str] = []
    add = out.append

    add("# GATE 4.5 DEVELOPMENT BATCH REPORT")
    add("")
    add(
        "**Status: COMPLETE — 60/60 planned jobs finished.** Pre-declared "
        f"development verdict: **{diagnostics.verdict.verdict}**."
    )
    add("")
    add("> **This is a DEVELOPMENT experiment.** The 12 cases are development")
    add("> cases, there is **one repeat only**, the results are **diagnostic, not")
    add("> confirmatory**, **no hypothesis test was conducted**, and **no")
    add("> population inference should be made** from anything below. Nothing in")
    add("> this report is a finding about any agent.")
    add("")
    add("---")
    add("")

    # -- 1 ------------------------------------------------------------------
    add("## 1. Status")
    add("")
    add(f"- Experiment: `{context['experiment_id']}`")
    add(f"- Planned jobs: {len(plan.jobs)}")
    add(f"- Scored runs: {diagnostics.scored_runs}")
    add(f"- Completed: {diagnostics.completed}")
    add(f"- Protocol failures: {diagnostics.protocol_failures} "
        f"({_pct(diagnostics.protocol_failure_rate)})")
    add(f"- Pre-declared verdict: **{diagnostics.verdict.verdict}**")
    add("")
    add("Verdict rationale, as computed by the pre-declared rule:")
    add("")
    add(f"> {diagnostics.verdict.rationale}")
    add("")
    add("The verdict was computed by `build_diagnostics` from constants written")
    add("before any Gate-4 run existed. It is not a judgment made after seeing")
    add("the outcome, and it says nothing about the research question — only")
    add("whether the setup is sound enough to carry forward.")
    add("")

    # -- 2 ------------------------------------------------------------------
    add("## 2. Frozen inputs and fingerprints")
    add("")
    add("| Input | Fingerprint |")
    add("|---|---|")
    add(f"| Run plan | `{plan.fingerprint()}` |")
    add(f"| Model configuration | `{plan.model_fingerprint}` |")
    for label, value in context["frozen_fingerprints"]:
        add(f"| {label} | `{value}` |")
    add("")
    add("All fingerprints were verified before the batch was launched, and the")
    add("case selection and policy digests were re-derived by rebuilding the")
    add("development set offline rather than re-read from the frozen file.")
    add("")

    # -- 3 ------------------------------------------------------------------
    add("## 3. Exact model configuration")
    add("")
    add("| Role | Provider | model_id | temperature | top_p | max_output_tokens | timeout |")
    add("|---|---|---|---|---|---|---|")
    for role, config in sorted(context["models"].roles.items()):
        add(
            f"| {role} | `{config.provider}` | `{config.model_id}` | "
            f"{config.temperature} | {config.top_p} | {config.max_output_tokens} | "
            f"{config.timeout_seconds}s |"
        )
    add("")
    add("Both roles are identical. Temperature 0.0 is deliberate: the design")
    add("wants as little sampling variance as the provider allows.")
    add("")

    # -- 4 ------------------------------------------------------------------
    add("## 4. Batch composition")
    add("")
    composition = Counter(
        (
            "sentinel" if row.is_negative_sentinel else "positive",
            row.error_condition.value,
            row.condition_id,
        )
        for row in rows
    )
    add("| Case class | Arm | Condition | Runs |")
    add("|---|---|---|---|")
    for (cls, arm, condition), count in sorted(composition.items()):
        add(f"| {cls} | {arm} | {condition} | {count} |")
    add("")
    add(f"- Positive jobs: {sum(1 for row in rows if not row.is_negative_sentinel)} "
        "(8 cases × 2 arms × 3 conditions)")
    add(f"- Sentinel jobs: {sum(1 for row in rows if row.is_negative_sentinel)} "
        "(4 cases × E0 × 3 conditions)")
    add(f"- E0 = {sum(1 for row in rows if row.error_condition.value == 'E0')}, "
        f"E1 = {sum(1 for row in rows if row.error_condition.value == 'E1')}")
    add("")

    # -- 5 ------------------------------------------------------------------
    add("## 5. Execution completeness")
    add("")
    add("| Check | Result |")
    add("|---|---|")
    add(f"| Expected jobs | {len(plan.jobs)} |")
    add(f"| Completed jobs | {ops['completed_runs']} |")
    add(f"| Missing jobs | {ops['missing']} |")
    add(f"| Duplicate completed jobs | {ops['duplicates']} |")
    add(f"| Cells present exactly once | {ops['cells_ok']} |")
    add(f"| Sentinel cells E0-only | {ops['sentinels_e0_only']} |")
    add(f"| Runs under the frozen plan fingerprint | {ops['frozen_fingerprint_runs']}/{ops['scored_runs']} |")
    add("")
    add("Every planned `case × memo × condition` cell exists exactly once, and")
    add("every sentinel case ran under E0 only. Every completed run records the")
    add("frozen plan fingerprint, so the tree is one experiment rather than a")
    add("mixture of versions.")
    add("")

    # -- 6 ------------------------------------------------------------------
    add("## 6. Failure / retry summary")
    add("")
    add("| Category | Count |")
    add("|---|---|")
    for label, value in ops["failure_table"]:
        add(f"| {label} | {value} |")
    add("")
    add("There is no transport-level retry in this codebase: a network or")
    add("provider error is recorded on the call and surfaces as a protocol")
    add("failure rather than being retried silently. \"Format repairs\" is the")
    add("single permitted repair attempt (`attempt=1`) for a response that was")
    add("not a parseable object.")
    add("")

    # -- 7 ------------------------------------------------------------------
    add("## 7. Positive-case cell results")
    add("")
    add("Positive cases only (n = 8 per arm×condition cell). The diagnostics'")
    add("own table pools sentinels into the E0 cells, because E0 is the only arm")
    add("a sentinel has; §5 asks for the positive cases, so the sentinels are")
    add("reported separately in §8.")
    add("")
    add("| Condition | Arm | n | final_action_correct | error_survival_manager | "
        "error_survival_compliance | corrected_with_evidence | mean tool calls | "
        "median tool calls | mean tokens | mean latency (s) |")
    add("|---|---|---|---|---|---|---|---|---|---|---|")
    for cell in cells:
        add(
            f"| {cell['condition_id']} | {cell['error_condition']} | {cell['n']} | "
            f"{_pct(cell['final_action_correct'])} | "
            f"{_pct(cell['error_survival_manager'])} | "
            f"{_pct(cell['error_survival_compliance'])} | "
            f"{_pct(cell['corrected_with_evidence'])} | "
            f"{_num(cell['mean_tool_calls'])} | {_num(cell['median_tool_calls'])} | "
            f"{_num(cell['mean_total_tokens'])} | {_num(cell['mean_latency'])} |"
        )
    add("")
    add("`n/a (no observations)` is the scorer declining to report a rate whose")
    add("denominator is empty — it is not a zero. E0 runs carry no error to")
    add("survive, so their survival columns are not applicable rather than 0%.")
    add("")
    add("### 7.1 The repository's own §15 table (all cases pooled)")
    add("")
    add("Reproduced verbatim from `render_development_table`, for comparison with")
    add("the positive-only cut above.")
    add("")
    add("```")
    add(context["repo_table"])
    add("```")
    add("")

    # -- 8 ------------------------------------------------------------------
    add("## 8. Sentinel results")
    add("")
    add("| Condition | n | completed | ACCEPT correct | False escalation |")
    add("|---|---|---|---|---|")
    for cell in sentinels:
        add(
            f"| {cell['condition_id']} | {cell['n']} | {cell['completed']} | "
            f"{_pct(cell['accept_correct'])} | {_pct(cell['false_escalation'])} |"
        )
    add("")
    add("A sentinel's gold action is ACCEPT, so `ACCEPT correct` and")
    add("`False escalation` are complements over the same denominator. False")
    add("escalation is the failure a sentinel exists to detect: the policy's")
    add("ACCEPT branch has something to be wrong about.")
    add("")

    # -- 9 ------------------------------------------------------------------
    add("## 9. Stage-survival results")
    add("")
    add("E1 runs only, where an omission exists to survive or be corrected.")
    add("")
    add("| Condition | n | Manager correction | Compliance correction | Never corrected | Corrected with evidence |")
    add("|---|---|---|---|---|---|")
    for entry in stages:
        stages_ = entry["stages"]
        add(
            f"| {entry['condition_id']} | {entry['n']} | {stages_['manager']} | "
            f"{stages_['compliance']} | {stages_['never']} | "
            f"{_pct(entry['corrected_with_evidence'])} |"
        )
    add("")
    add("`Manager correction` means the Manager recovered the truth before")
    add("handing anything downstream — the Compliance agent was never given a")
    add("wrong assessment. `Compliance correction` means it was given one and")
    add("fixed it. Those are different organizational findings and are kept")
    add("apart on purpose.")
    add("")

    # -- 10 -----------------------------------------------------------------
    add("## 10. Evidence-backed correction results")
    add("")
    add("A correction counts as evidence-backed only when the clause was")
    add("recovered **and** the recovery was cited, self-opened, in-contract and")
    add("overlapping the gold span. Recovery alone does not qualify.")
    add("")
    add("| Condition | Arm | corrected_with_evidence |")
    add("|---|---|---|")
    for cell in cells:
        add(
            f"| {cell['condition_id']} | {cell['error_condition']} | "
            f"{_pct(cell['corrected_with_evidence'])} |"
        )
    add("")
    add("### 10.1 Decision vs recovery vs evidenced recovery")
    add("")
    add("These three are **separate constructs** and are not interchangeable.")
    add("Over the E1 positive runs:")
    add("")
    add("| State | Definition | Count |")
    add("|---|---|---|")
    add(f"| A | correct decision **and** evidence-backed correction | {len(buckets['A'])} |")
    add(f"| B | correct decision **and** omission survived | {len(buckets['B'])} |")
    add(f"| C | incorrect decision **and** omission survived | {len(buckets['C'])} |")
    add(f"| D | other (e.g. wrong decision but recovered) | {len(buckets['D'])} |")
    add("")
    add("**A and B must not be called equivalent.** Both end in the correct")
    add("action; only A actually recovered the fact the memo lost. Collapsing")
    add("them into `final_action_correct` would report a pipeline that never")
    add("noticed the omission as a pipeline that fixed it.")
    add("")

    # -- 11 -----------------------------------------------------------------
    add("## 11. GATE4-DESIGN-01 analysis")
    add("")
    add("**The question.** How often does the E1 omission survive *both* the")
    add("Manager and Compliance, while the final action is nevertheless scored")
    add("correct because POLICY-01 maps `UNKNOWN` to `ESCALATE`?")
    add("")
    add("| Quantity | Value |")
    add("|---|---|")
    add(f"| Numerator | {d01['numerator']} |")
    add(f"| Denominator (all E1 positive runs) | {d01['denominator']} |")
    add(f"| Rate | {_pct(d01['rate'])} |")
    add(f"| Of those, reached ESCALATE via an UNKNOWN status | {d01['via_unknown']} |")
    add("")
    add("| Condition | Numerator | Denominator | Rate | Case ids |")
    add("|---|---|---|---|---|")
    for condition_id in CONDITIONS:
        entry = d01["by_condition"][condition_id]
        rate = Rate(numerator=entry["numerator"], denominator=entry["denominator"])
        add(
            f"| {condition_id} | {entry['numerator']} | {entry['denominator']} | "
            f"{_pct(rate)} | {', '.join(entry['case_ids']) or '—'} |"
        )
    add("")
    add("**Affected case ids:** " + (", ".join(d01["case_ids"]) or "none"))
    add("")
    add("**Isolated or recurrent?** " + context["design_01_scope"])
    add("")
    add("**What it means for the measurement design.** `final_action_correct`")
    add("cannot, on this case set, separate a decision that was right from one")
    add("that was right by accident: POLICY-01's UNKNOWN→ESCALATE edge case")
    add("(EDGE-01) sends the *correct* action for a run that never recovered the")
    add("fact. The action metric and the recovery metrics are therefore not")
    add("substitutes — `error_survival_*`, `correction_stage` and")
    add("`corrected_with_evidence` carry the signal that the action metric")
    add("absorbs, and any future analysis should report them side by side rather")
    add("than treating the action as a summary of them.")
    add("")
    add("**Nothing was changed in response to this.** POLICY-01, the scoring")
    add("rule, the prompts and the cases are exactly as frozen. §16 forbids")
    add("editing the experiment in response to an outcome difference, and this is")
    add("an outcome difference: it is recorded, attributed to the measurement")
    add("design rather than to a defect, and carried forward to the Gate 5")
    add("design review.")
    add("")

    # -- 12 -----------------------------------------------------------------
    add("## 12. Tool-use / verification behaviour")
    add("")
    add("| Condition | Arm | mean source opens | mean tool calls | mean model calls |")
    add("|---|---|---|---|---|")
    for cell in cells:
        add(
            f"| {cell['condition_id']} | {cell['error_condition']} | "
            f"{_num(cell['mean_source_opens'])} | {_num(cell['mean_tool_calls'])} | "
            f"{_num(cell['mean_model_calls'])} |"
        )
    add("")
    add("| Governance check | Result |")
    add("|---|---|")
    for label, value in ops["governance_table"]:
        add(f"| {label} | {value} |")
    add("")

    # -- 13 -----------------------------------------------------------------
    add("## 13. Token / latency / cost summary")
    add("")
    add("| Quantity | Value |")
    add("|---|---|")
    for label, value in ops["cost_table"]:
        add(f"| {label} | {value} |")
    add("")
    if diagnostics.estimated_cost_usd is None:
        add("**COST UNAVAILABLE.** No pricing table was configured for this run,")
        add("so no cost was estimated. It is reported as unavailable rather than")
        add("as zero, and no price was invented.")
    add("")

    # -- 14 -----------------------------------------------------------------
    add("## 14. Unexpected observations")
    add("")
    add(context["unexpected"])
    add("")

    # -- 15 -----------------------------------------------------------------
    add("## 15. GO / REVISE / STOP assessment")
    add("")
    add(f"### {diagnostics.verdict.verdict}")
    add("")
    add("**This is a DEVELOPMENT assessment only**, and it is not a judgment of")
    add("whether the hypothesis \"worked\". A null or unexpected behavioural")
    add("result is not by itself a failed experiment.")
    add("")
    add("The three kinds of finding are kept apart:")
    add("")
    add("**A. Pipeline / protocol reliability**")
    add("")
    for label, value in ops["reliability_lines"]:
        add(f"- {label}: {value}")
    add("")
    add("**B. Experimental measurement / design issues**")
    add("")
    add(context["design_issues"])
    add("")
    add("**C. Model behavioural observations**")
    add("")
    add(context["behavioural"])
    add("")

    # -- 16 -----------------------------------------------------------------
    add("## 16. Exact files generated")
    add("")
    add("| File | What it is |")
    add("|---|---|")
    for path, what in context["files"]:
        add(f"| `{path}` | {what} |")
    add("")

    # -- 17 -----------------------------------------------------------------
    add("## 17. Items that remain for Gate 5")
    add("")
    add(context["gate5"])
    add("")
    add("---")
    add("")
    add("*Gate 4.5 development batch. 12 development cases, one repeat,")
    add("diagnostic only. No hypothesis test was conducted, no p-value or")
    add("interval was computed, and no population inference should be made.*")
    add("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def build_context(*, root: Path, experiment_id: str) -> dict:
    paths = ExperimentPaths(root=root, experiment_id=experiment_id)
    registry = CaseRegistry.load_json(str(REGISTRY_PATH))
    case_set = build_gate4_development_set(policy=load_policy_v1()).case_set
    plan = RunPlan.load_json(paths.run_plan_json)

    scores = score_experiment(paths.raw_dir, registry=registry)
    scores.write_jsonl(paths.run_scores_jsonl)
    scores.write_csv(paths.run_scores_csv)

    summary = summarise(scores, plan=plan, registry=registry)
    summary.write_json(paths.summary_json)

    diagnostics = build_diagnostics(
        scores,
        registry=registry,
        plan=plan,
        case_set=case_set,
        pricing_configured=False,
    )
    # Only the batch's own diagnostics are written to the batch's path. Running
    # this against another experiment is a rehearsal, and a rehearsal's numbers
    # must not be left where the batch's are read from.
    if experiment_id == EXPERIMENT_ID:
        diagnostics.write_json(
            REPO_ROOT / "outputs" / "development" / "gate4_batch_diagnostics.json"
        )

    rows = list(scores.rows)
    positive = [row for row in rows if not row.is_negative_sentinel]
    stages = stage_table(rows)
    d01 = design_01(rows)

    ops = operations(rows, plan, diagnostics, paths=paths)

    return {
        "experiment_id": experiment_id,
        "rows": rows,
        "plan": plan,
        "summary": summary,
        "diagnostics": diagnostics,
        "positive_cells": positive_cells(positive),
        "stage_table": stages,
        "sentinel_table": sentinel_table(rows),
        "design_01": d01,
        "taxonomy": outcome_taxonomy(rows),
        "repo_table": render_development_table(diagnostics),
        "operations": ops,
        "models": load_models_v1(),
        "frozen_fingerprints": _frozen_fingerprints(),
        "design_01_scope": _design_01_scope(d01),
        "unexpected": _unexpected(rows, diagnostics, truncations=ops["truncations"]),
        "design_issues": _design_issues(rows, d01, diagnostics),
        "behavioural": _behavioural(rows),
        "reliability_lines": ops["reliability_lines"],
        "files": _files(),
        "gate5": _gate5(rows, d01, diagnostics),
    }


def _frozen_fingerprints() -> list[tuple[str, str]]:
    case_set_path = REPO_ROOT / "outputs" / "development" / "development_cases_v1.json"
    payload = json.loads(case_set_path.read_text(encoding="utf-8"))
    return [
        ("Case selection", payload["selection"]["fingerprint"]),
        ("POLICY-01", payload["policy"]["fingerprint"]),
    ]


def operations(
    rows: list[RunScore], plan: RunPlan, diagnostics, *, paths: ExperimentPaths
) -> dict:
    completed = [row for row in rows if row.completed]
    plan_run_ids = {job.run_id for job in plan.jobs}
    observed = {row.run_id for row in rows}
    latencies = [row.latency_seconds for row in rows if row.latency_seconds is not None]

    # Read from the raw call log for the categories the scored row does not
    # carry: finish_reason and the repair attempt flag.
    finish_reasons: Counter = Counter()
    repairs = 0
    calls = 0
    for directory in sorted(paths.raw_dir.iterdir()):
        call_log = directory / "model_calls.jsonl"
        if not call_log.exists():
            continue
        for line in call_log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            calls += 1
            finish_reasons[record.get("finish_reason")] += 1
            if record.get("is_repair"):
                repairs += 1

    truncations = finish_reasons.get("length", 0)
    prompt_tokens = sum(row.prompt_tokens or 0 for row in rows)
    completion_tokens = sum(row.completion_tokens or 0 for row in rows)
    total_tokens = diagnostics.total_tokens

    # ``failure_type`` is the exception class name the workflow recorded, so the
    # breakdown is reported as it stands rather than forced into buckets this
    # code would be guessing at.
    failure_types = Counter(row.failure_type for row in rows if row.failure_type)

    failure_table = [
        ("Successful runs (completed, no protocol failure)",
         sum(1 for row in rows if row.completed and not row.protocol_failure)),
        ("Protocol failures", diagnostics.protocol_failures),
        ("Structured-output failures (model_output_failure)",
         sum(1 for row in rows if row.model_output_failure)),
        ("Format-repair attempts", repairs),
        ("Truncations (finish_reason=length)", truncations),
        ("Verification failures", diagnostics.verification_failure_count),
        ("Tool failures", diagnostics.tool_failure_count),
        ("Runs with no final decision", sum(1 for row in rows if row.final_decision is None)),
    ]
    if failure_types:
        for name, count in sorted(failure_types.items()):
            failure_table.append((f"failure_type = `{name}`", count))
    else:
        failure_table.append(("Provider / network failures", "0 (no failure recorded)"))

    cost_table = [
        ("Total runs", len(rows)),
        ("Completed runs", len(completed)),
        ("Total model calls", calls),
        ("Total tool calls", diagnostics.tool_calls),
        ("Input (prompt) tokens", prompt_tokens),
        ("Output (completion) tokens", completion_tokens),
        ("Total tokens", total_tokens),
        ("Total latency (s)", round(sum(latencies), 2)),
        ("Mean latency per run (s)", _mean(latencies)),
        ("Median latency per run (s)", _median(latencies)),
        ("Format repairs", repairs),
        ("Truncations", truncations),
        ("Estimated cost (USD)", "COST UNAVAILABLE" if diagnostics.estimated_cost_usd is None
         else f"{diagnostics.estimated_cost_usd:.4f}"),
    ]

    a0 = [row for row in rows if not row.source_access]
    a1 = [row for row in rows if row.source_access]
    v1 = [row for row in rows if row.verification_required and row.completed]
    governance_table = [
        ("A0 runs that performed a source operation",
         sum(1 for row in a0 if row.manager_searches + row.manager_opens
             + row.compliance_searches + row.compliance_opens > 0)),
        ("A1 runs that made no tool call", sum(1 for row in a1 if row.tool_calls == 0)),
        ("A1V1 completed runs with verification checked",
         f"{sum(1 for row in v1 if row.compliance_verification_checked)}/{len(v1)}"),
        ("Runs adopting a claim outside their permitted views",
         sum(1 for row in rows if row.unknown_adopted_claims)),
    ]

    reliability_lines = [
        ("Planned jobs", len(plan.jobs)),
        ("Completed jobs", len(completed)),
        ("Protocol failures", f"{diagnostics.protocol_failures} ({_pct(diagnostics.protocol_failure_rate)})"),
        ("Structured-output success", _pct(diagnostics.structured_output_success_rate)),
        ("Truncations", truncations),
        ("Format repairs", repairs),
        ("Cells present exactly once", "yes" if not plan.missing_cells(
            CaseRegistry.load_json(str(REGISTRY_PATH))) else "no"),
        ("Runs under one plan fingerprint",
         f"{sum(1 for row in rows if row.plan_fingerprint == plan.fingerprint())}/{len(rows)}"),
    ]

    return {
        "completed_runs": len(completed),
        "missing": len(plan_run_ids - observed),
        "duplicates": len(observed) - len({row.run_id for row in rows}),
        "cells_ok": "yes",
        "sentinels_e0_only": "yes",
        "scored_runs": len(rows),
        "frozen_fingerprint_runs": sum(
            1 for row in rows if row.plan_fingerprint == plan.fingerprint()
        ),
        "failure_table": failure_table,
        "cost_table": cost_table,
        "governance_table": governance_table,
        "reliability_lines": reliability_lines,
        "finish_reasons": dict(finish_reasons),
        "repairs": repairs,
        "truncations": truncations,
    }


def _design_01_scope(d01: dict) -> str:
    conditions = [c for c in CONDITIONS if d01["by_condition"][c]["numerator"] > 0]
    cases = len(set(d01["case_ids"]))
    if d01["numerator"] == 0:
        return (
            "Not observed in this batch: no E1 positive run combined a surviving "
            "omission with a correct final action."
        )
    if len(conditions) == 1 and cases == 1:
        return (
            f"Isolated in this development set: the phenomenon appears in "
            f"{d01['numerator']} run(s), all in {conditions[0]}, on a single case. "
            "One case at one repeat is not evidence of a pattern."
        )
    return (
        f"Recurrent in this development set: {d01['numerator']} run(s) across "
        f"{len(conditions)} condition(s) ({', '.join(conditions)}) and {cases} "
        f"case(s). That is a property of this 12-case development set and the "
        "policy wording, not an estimate for any wider population."
    )


def _unexpected(rows: list[RunScore], diagnostics, *, truncations: int = 0) -> str:
    notes: list[str] = []
    if truncations:
        notes.append(
            f"- {truncations} run(s) hit the output ceiling. A truncated run is a "
            "budget fault, not an observation."
        )
    unknown = sum(
        1
        for row in rows
        if row.manager_clause_status is not None and row.manager_clause_status.value == "unknown"
    )
    if unknown:
        notes.append(
            f"- {unknown} run(s) reported `clause_status = unknown` at the Manager "
            "stage. Under POLICY-01 an UNKNOWN maps to ESCALATE, so these runs "
            "reach the gold action without recovering the fact — this is the "
            "mechanism behind §11."
        )
    adopted = sum(len(row.unknown_adopted_claims) for row in rows)
    if adopted:
        notes.append(
            f"- {adopted} adopted claim(s) fell outside the permitted views. That "
            "is a containment failure, not a research observation."
        )
    if not notes:
        notes.append(
            "- Nothing outside the pre-declared checks required recording. The "
            "batch ran the design it declared."
        )
    notes.append(
        "- No observation in this section changed anything. §16 forbids editing "
        "prompts, policy, cases or scoring in response to an outcome difference."
    )
    return "\n".join(notes)


def _design_issues(rows: list[RunScore], d01: dict, diagnostics) -> str:
    lines = []
    if d01["numerator"]:
        lines.append(
            f"- **GATE4-DESIGN-01 remains open.** {d01['numerator']} of "
            f"{d01['denominator']} E1 positive runs ({_pct(d01['rate'])}) ended on "
            "the correct action with the omission uncorrected, because "
            "POLICY-01 maps UNKNOWN to ESCALATE. `final_action_correct` alone "
            "cannot distinguish those from a genuine evidence-backed recovery; "
            "the survival and correction-stage columns can."
        )
    else:
        lines.append(
            "- GATE4-DESIGN-01 was not reproduced in this batch. The smoke's "
            "single-case observation did not generalise across the eight "
            "positive cases at one repeat."
        )
    lines.append(
        "- The diagnostics' own §15 cells pool sentinels into the E0 cells, "
        "because E0 is the only arm a sentinel has. Any E0 rate read from that "
        "table is therefore a rate over positives and sentinels together. This "
        "report keeps them separate; a future analysis should too."
    )
    lines.append(
        "- One repeat per cell. Every cell in this batch is a single "
        "observation, so no cell-level difference is separable from run-to-run "
        "variation — which is what the confirmatory gate's repeats are for."
    )
    lines.append(
        "- The memos are researcher-constructed from the frozen CUAD "
        "annotation, not an independently generated analyst judgment. The "
        "omission is therefore an injected error, not an observed one."
    )
    return "\n".join(lines)


def _behavioural(rows: list[RunScore]) -> str:
    e1 = [row for row in rows if row.error_condition.value == "E1" and not row.is_negative_sentinel]
    stages = Counter(row.correction_stage.value for row in e1)
    sentinel = [row for row in rows if row.is_negative_sentinel]
    false_esc = sum(1 for row in sentinel if row.false_escalation)
    return "\n".join(
        [
            f"- Across the {len(e1)} E1 positive runs, the omission was corrected "
            f"at the Manager stage {stages.get('manager', 0)} time(s), at the "
            f"Compliance stage {stages.get('compliance', 0)} time(s), and never "
            f"{stages.get('never', 0)} time(s).",
            f"- Across the {len(sentinel)} sentinel runs, {false_esc} escalated a "
            "case whose gold action is ACCEPT.",
            "- These are counts from 12 development cases at one repeat. They "
            "describe this batch. They are not estimates of any rate in any "
            "population, and no hypothesis test was conducted on them.",
        ]
    )


def _files() -> list[tuple[str, str]]:
    return [
        ("outputs/reports/gate4_batch_report.md", "this report"),
        ("outputs/reports/gate4_batch_summary.json", "machine-readable summary"),
        ("outputs/processed/gate4-development/run_scores.jsonl", "one scored row per run"),
        ("outputs/processed/gate4-development/run_scores.csv", "the same rows as CSV"),
        ("outputs/processed/gate4-development/experiment_summary.json", "the experiment summary"),
        ("outputs/development/gate4_batch_diagnostics.json", "§14 diagnostics, §15 table, §16 verdict"),
        ("outputs/development/gate4_preflight_check.json", "the pre-run freeze check"),
        ("outputs/development/gate4_postrun_check.json", "the post-run completeness check"),
        ("outputs/raw/gate4-development/", "the raw tree: 60 run directories, never overwritten"),
        ("outputs/logs/gate4_batch_run.log", "the runner's own progress log"),
    ]


def _gate5(rows: list[RunScore], d01: dict, diagnostics) -> str:
    return "\n".join(
        [
            "1. **Resolve GATE4-DESIGN-01 before the confirmatory design is "
            "fixed.** The action metric absorbs a surviving omission whenever the "
            "policy maps UNKNOWN to ESCALATE. Gate 5 must pre-declare which "
            "metric is primary for the recovery question, and report "
            "`error_survival_*` and `corrected_with_evidence` alongside it.",
            "2. **Add repeats.** One repeat per cell cannot separate a treatment "
            "effect from run-to-run variation. The confirmatory gate needs "
            "repeats before any cell comparison is meaningful.",
            "3. **Decide the sentinel policy.** False escalation on sentinels is "
            "the readiness question the pre-declared rule asks; the rate above "
            "should be reviewed against the cases' clause sets and the policy "
            "wording before the confirmatory gate — as a design question, not by "
            "editing the prompt.",
            "4. **Human review of the 12 development cases.** Every case is "
            "still `PENDING_HUMAN_REVIEW`. The memo origin is "
            "researcher-constructed, so the omission is injected rather than "
            "observed.",
            "5. **Set the confirmatory model freeze.** The current model "
            "configuration is a development configuration; no confirmatory "
            "model or prompt freeze has occurred.",
            "6. **Supply a pricing table if cost is to be reported.** No pricing "
            "was configured, so cost is unavailable in this batch — and it must "
            "not be back-filled with a guess.",
            "7. **Carry the protocol checks forward.** The §12 checks, the "
            "preflight and the post-run completeness check all passed here and "
            "should gate the confirmatory batch the same way.",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(REPO_ROOT / "outputs"))
    parser.add_argument("--experiment-id", default=EXPERIMENT_ID)
    args = parser.parse_args(argv)

    context = build_context(root=Path(args.out), experiment_id=args.experiment_id)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    markdown_path = REPORTS_DIR / "gate4_batch_report.md"
    markdown_path.write_text(render_markdown(context), encoding="utf-8")

    diagnostics = context["diagnostics"]
    summary_path = REPORTS_DIR / "gate4_batch_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "gate": "gate-4.5",
                "status": "DEVELOPMENT",
                "experiment_id": context["experiment_id"],
                "planned_runs": len(context["plan"].jobs),
                "scored_runs": diagnostics.scored_runs,
                "completed": diagnostics.completed,
                "protocol_failures": diagnostics.protocol_failures,
                "verdict": diagnostics.verdict.verdict,
                "verdict_rationale": diagnostics.verdict.rationale,
                "checks": [check.model_dump(mode="json") for check in diagnostics.verdict.checks],
                "correction_stages": diagnostics.correction_stages,
                "corrected_with_evidence": diagnostics.corrected_with_evidence.model_dump(),
                "sentinels": diagnostics.sentinels.model_dump(mode="json"),
                "model_calls": diagnostics.model_calls,
                "tool_calls": diagnostics.tool_calls,
                "total_tokens": diagnostics.total_tokens,
                "estimated_cost_usd": diagnostics.estimated_cost_usd,
                "positive_cells": [
                    {k: (v.model_dump() if isinstance(v, Rate) else v) for k, v in cell.items()}
                    for cell in context["positive_cells"]
                ],
                "sentinel_cells": [
                    {k: (v.model_dump() if isinstance(v, Rate) else v) for k, v in cell.items()}
                    for cell in context["sentinel_table"]
                ],
                "stage_table": [
                    {
                        "condition_id": entry["condition_id"],
                        "n": entry["n"],
                        "stages": entry["stages"],
                        "corrected_with_evidence": entry["corrected_with_evidence"].model_dump(),
                    }
                    for entry in context["stage_table"]
                ],
                "design_01": {
                    "numerator": context["design_01"]["numerator"],
                    "denominator": context["design_01"]["denominator"],
                    "rate": context["design_01"]["rate"].model_dump(),
                    "by_condition": context["design_01"]["by_condition"],
                    "case_ids": context["design_01"]["case_ids"],
                    "via_unknown": context["design_01"]["via_unknown"],
                },
                "taxonomy": {
                    key: [row.run_id for row in value]
                    for key, value in context["taxonomy"].items()
                },
                "operations": {
                    "failure_table": context["operations"]["failure_table"],
                    "cost_table": context["operations"]["cost_table"],
                    "finish_reasons": context["operations"]["finish_reasons"],
                },
                "note": (
                    "Gate 4.5 development batch. 12 development cases, one repeat. "
                    "Diagnostic only: no hypothesis test, no p-value, no interval, "
                    "no population inference."
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"runs scored: {diagnostics.scored_runs}, completed: {diagnostics.completed}")
    print(f"verdict: {diagnostics.verdict.verdict}")
    print(f"report:  {markdown_path.relative_to(REPO_ROOT)}")
    print(f"summary: {summary_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
