"""Gate 4: how many output tokens the real prompts actually need.

Written because the Gate-4 smoke found the configured budget ambiguous. This
provider is a reasoning model -- it spends output tokens before emitting any
content, and ``max_output_tokens`` caps both together -- so a budget smaller
than its reasoning yields an *empty* response with ``finish_reason="length"``,
which downstream reads as a model that ignored the schema.

Two numbers decide whether ``max_output_tokens: 1024`` is enough:

* how large the rendered prompt is, which sets how much there is to reason
  about, and
* how large a *complete* answer is, which is the floor the budget must clear
  before any reasoning at all.

Both are measured here, offline, by rendering the real prompts against the real
development case and running the real workflow with a scripted transport. The
scripted transport answers with a valid object, so the run completes and the raw
call log records exactly what would have been sent. No provider is contacted and
no credential is read.

    .venv/Scripts/python.exe scripts/gate4_budget_probe.py

The answer is a *floor*, not a measurement of the live model's reasoning: this
script cannot see how many tokens a real call spends thinking, and it does not
pretend to. It exists to rule out the obvious failure -- a budget that cannot
even hold the answer -- and to print the prompt sizes so the operator can size
the budget with the numbers in front of them.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from pilot01.config import (  # noqa: E402
    load_conditions_v1,
    load_models_v1,
    load_workflow_v1,
)
from pilot01.experiment.batch import ExperimentRunner, ScriptedClientFactory  # noqa: E402
from pilot01.experiment.cases import CaseRegistry  # noqa: E402
from pilot01.experiment.layout import ExperimentPaths  # noqa: E402
from pilot01.experiment.runspec import build_run_plan  # noqa: E402
from pilot01.schemas import (  # noqa: E402
    ClauseStatus,
    ComplianceOutput,
    Decision,
    ManagerOutput,
    VerificationStatus,
)

DEFAULT_CASE_ID = "DEV-POS-COC-0496"
PROBE_EXPERIMENT_ID = "gate4-budget-probe"

# A complete, minimal, schema-valid answer for each role. Deliberately *terse*:
# this is the floor, and a floor measured with a verbose answer would overstate
# the budget a model needs to answer at all.
MANAGER_ANSWER = ManagerOutput(
    clause_status=ClauseStatus.ABSENT,
    decision=Decision.ACCEPT,
    rule_id="PROBE",
    verification_status=VerificationStatus.NOT_CHECKED,
    confidence=0.5,
    reason_summary="probe",
).model_dump_json()

COMPLIANCE_ANSWER = ComplianceOutput(
    clause_status=ClauseStatus.ABSENT,
    decision=Decision.ACCEPT,
    rule_id="PROBE",
    verification_status=VerificationStatus.NOT_CHECKED,
    confidence=0.5,
    reason_summary="probe",
).model_dump_json()

# What the Manager's *tool* turn looks like when source access is available: the
# loop asks for a search before answering, and the probe has to answer that turn
# too or the run never reaches the answer.
SEARCH_TURN = json.dumps(
    {"tool": "search_contract", "query": "change of control termination convenience"}
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--registry",
        default=str(REPO_ROOT / "outputs" / "development" / "cases_v1.json"),
    )
    parser.add_argument("--case", default=DEFAULT_CASE_ID)
    args = parser.parse_args(argv)

    registry = CaseRegistry.load_json(args.registry)
    conditions = load_conditions_v1()
    models = load_models_v1()
    workflow = load_workflow_v1()

    print("Gate 4 budget probe -- rendered prompt sizes, offline")
    print(f"  case: {args.case}")
    print(f"  configured max_output_tokens: "
          f"{models.roles['manager'].max_output_tokens} (manager), "
          f"{models.roles['compliance'].max_output_tokens} (compliance)")
    print()

    plan = build_run_plan(
        experiment_id=PROBE_EXPERIMENT_ID,
        registry=registry,
        conditions=conditions,
        repeat_count=1,
        seed=0,
        models=models,
        case_ids=[args.case],
    )
    # Every cell the planner produced is scripted and run, because the runner
    # checks that a plan covers its own grid -- the same check that turned the
    # §12 smoke from four runs into six. Here that is a convenience: one plan
    # measures every governance condition at once.
    script = {
        job.run_id: {
            "manager": _manager_script(job.source_access),
            "compliance": [COMPLIANCE_ANSWER],
        }
        for job in plan.jobs
    }

    with tempfile.TemporaryDirectory() as scratch:
        paths = ExperimentPaths(root=Path(scratch), experiment_id=PROBE_EXPERIMENT_ID)
        ExperimentRunner(
            plan=plan,
            registry=registry,
            conditions=conditions,
            workflow=workflow,
            models=models,
            client_factory=ScriptedClientFactory(script),
            paths=paths,
        ).run()

        rows: list[tuple[str, str, int, int]] = []
        for job in plan.jobs:
            for role, prompt_chars, answer_chars in _measure(paths, job.run_id):
                rows.append(
                    (
                        f"{job.error_condition.value} {job.condition_id}",
                        role,
                        prompt_chars,
                        answer_chars,
                    )
                )

    _report(rows)
    return 0


def _manager_script(source_access: bool) -> list[str]:
    if not source_access:
        return [MANAGER_ANSWER]
    return [SEARCH_TURN, MANAGER_ANSWER]


def _measure(paths: ExperimentPaths, run_id: str) -> list[tuple[str, int, int]]:
    """Prompt and answer sizes, per role, read off the run's own raw call log."""
    log_path = paths.run_dir(run_id) / "model_calls.jsonl"
    if not log_path.exists():
        return []
    out: list[tuple[str, int, int]] = []
    seen: dict[str, tuple[int, int]] = {}
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        role = record["role"]
        prompt_chars = len(record.get("rendered_input") or "")
        answer_chars = len(record.get("raw_response") or "")
        # The largest prompt and the largest answer for the role: the budget has
        # to clear the worst call, not the average one.
        previous = seen.get(role, (0, 0))
        seen[role] = (max(previous[0], prompt_chars), max(previous[1], answer_chars))
    for role, (prompt_chars, answer_chars) in sorted(seen.items()):
        out.append((role, prompt_chars, answer_chars))
    return out


def _report(rows: list[tuple[str, str, int, int]]) -> None:
    if not rows:
        print("no calls were recorded; nothing to measure")
        return
    print(f"  {'condition':<10} {'role':<12} {'prompt chars':>13} {'answer chars':>13}")
    for condition_id, role, prompt_chars, answer_chars in rows:
        print(f"  {condition_id:<10} {role:<12} {prompt_chars:>13,} {answer_chars:>13,}")

    widest_prompt = max(row[2] for row in rows)
    widest_answer = max(row[3] for row in rows)
    print()
    print(f"  widest rendered prompt: {widest_prompt:,} chars")
    print(f"  widest complete answer: {widest_answer:,} chars (a floor, not a budget)")
    print()
    print("  A character is not a token, and this probe cannot see how many tokens")
    print("  the live model spends reasoning before it answers. What it rules out is")
    print("  a budget too small to hold the answer at all. If the live smoke returns")
    print("  finish_reason='length' with empty content, raise max_output_tokens in")
    print("  config/models_v1.yaml -- that is a budget fault, not a prompt fault.")


if __name__ == "__main__":
    raise SystemExit(main())
